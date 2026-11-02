"""Expenses booked as plain journal entries: payroll, bank fees and the like.

They usually have no document. They go in the register, and in the PDF only if they do have
one.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from ..formatting import format_amount
from ..i18n import t
from ..model import (
    Company,
    ExtractParams,
    Invoice,
    Notice,
    NoticeCode,
    Payment,
    PaymentKind,
    ProjectImputation,
    money,
)
from ..odoo_client import OdooBackend, chunks
from .analytics import line_percent
from .attachments import select_attachments
from .payments import fetch_payments

ENTRY_TYPE = "entry"
EXPENSE_ACCOUNT_TYPES = ["expense", "expense_depreciation", "expense_direct_cost"]
LIQUIDITY_ACCOUNT_TYPE = "asset_cash"
# Spanish chart of accounts: group 64 is personnel. Their references usually name the employee.
PERSONNEL_ACCOUNT_PREFIX = "64"


def _account_label(account: list) -> str:
    """Return the name of an account whose label is ``<code> <name>``."""
    code, _, name = account[1].partition(" ")
    return name.strip() or code


def _concept(move: dict[str, Any], lines: list[dict[str, Any]], show_names: bool) -> str:
    personnel = any(ln["account_id"][1].startswith(PERSONNEL_ACCOUNT_PREFIX) for ln in lines)
    if personnel and not show_names:
        return t("expense.personnel")
    if move["partner_id"]:
        return move["partner_id"][1]
    return move["ref"] or _account_label(lines[0]["account_id"])


def _liquidity_payments(backend: OdooBackend, ids: list[int]) -> dict[int, list[Payment]]:
    """Money that left a bank or cash account in the entry itself."""
    payments: dict[int, list[Payment]] = defaultdict(list)
    for batch in chunks(ids):
        for line in backend.search_read_all(
            "account.move.line",
            [("move_id", "in", list(batch)), ("account_type", "=", LIQUIDITY_ACCOUNT_TYPE)],
            ["move_id", "account_id", "balance", "date"],
        ):
            label = _account_label(line["account_id"])
            payments[line["move_id"][0]].append(
                Payment(
                    date=date.fromisoformat(line["date"]),
                    journal_name=label,
                    journal_type=LIQUIDITY_ACCOUNT_TYPE,
                    form=label,
                    method="",
                    amount=-money(line["balance"]),
                    kind=PaymentKind.OTHER,
                )
            )
    return payments


def _imputation(
    lines: list[dict[str, Any]], account_id: int, total: Decimal
) -> ProjectImputation | None:
    charged = sum(
        (
            Decimal(str(ln["balance"]))
            * line_percent(ln["analytic_distribution"], account_id)
            / 100
            for ln in lines
        ),
        Decimal(0),
    )
    if not charged or not total:
        return None
    return ProjectImputation(
        percent=(charged / total * 100).quantize(Decimal("0.01")), amount=money(charged)
    )


def _duplicates(invoices: list[Invoice], accounts: dict[int, tuple]) -> list[Notice]:
    """Same accounts and amount in the same month: possibly booked twice."""
    groups: dict[tuple, list[Invoice]] = defaultdict(list)
    for invoice in invoices:
        month = invoice.invoice_date.strftime("%Y-%m")
        groups[(accounts[invoice.id], invoice.total, month)].append(invoice)
    return [
        Notice(
            NoticeCode.POSSIBLE_DUPLICATE,
            group[0].name,
            {
                "names": ", ".join(i.name for i in group),
                "amount": format_amount(group[0].total),
                "month": group[0].invoice_date.strftime("%m/%Y"),
            },
        )
        for group in groups.values()
        if len(group) > 1
    ]


def fetch_other_expenses(
    backend: OdooBackend,
    params: ExtractParams,
    company: Company,
    form_overrides: dict[str, str] | None = None,
) -> tuple[list[Invoice], list[Notice]]:
    """Return the entries of the year charged to expense accounts, with their notices."""
    lines = backend.search_read_all(
        "account.move.line",
        [
            ("parent_state", "=", "posted"),
            ("move_type", "=", ENTRY_TYPE),
            ("account_type", "in", EXPENSE_ACCOUNT_TYPES),
            ("date", ">=", f"{params.year}-01-01"),
            ("date", "<=", f"{params.year}-12-31"),
            ("company_id", "=", company.id),
        ],
        ["move_id", "balance", "account_id", "analytic_distribution"],
    )
    by_move: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        by_move[line["move_id"][0]].append(line)

    moves = backend.read(
        "account.move",
        list(by_move),
        ["name", "ref", "date", "partner_id", "message_main_attachment_id"],
    )
    moves = [{**m, "move_type": ENTRY_TYPE} for m in moves]

    imputations: dict[int, ProjectImputation] = {}
    if params.analytic_account_id is not None:
        for move in moves:
            total = sum((Decimal(str(ln["balance"])) for ln in by_move[move["id"]]), Decimal(0))
            found = _imputation(by_move[move["id"]], params.analytic_account_id, total)
            if found:
                imputations[move["id"]] = found
        moves = [m for m in moves if m["id"] in imputations]

    ids = [m["id"] for m in moves]
    liquidity = _liquidity_payments(backend, ids)
    reconciled, _ = fetch_payments(backend, moves, form_overrides)
    attachments, _ = select_attachments(backend, moves)

    invoices: list[Invoice] = []
    accounts: dict[int, tuple] = {}
    for move in moves:
        move_lines = by_move[move["id"]]
        total = money(sum(Decimal(str(ln["balance"])) for ln in move_lines))
        if not total:
            continue
        payments = sorted(
            [*liquidity[move["id"]], *reconciled[move["id"]]], key=lambda p: (p.date, p.reference)
        )
        invoices.append(
            Invoice(
                id=move["id"],
                name=move["name"],
                ref="",
                partner_id=move["partner_id"][0] if move["partner_id"] else None,
                partner_name=_concept(move, move_lines, params.show_personnel_names),
                partner_vat="",
                invoice_date=date.fromisoformat(move["date"]),
                untaxed=total,
                tax=Decimal("0.00"),
                total=total,
                payment_state="",
                move_type=ENTRY_TYPE,
                payments=payments,
                attachment=attachments[move["id"]],
                imputation=imputations.get(move["id"]),
            )
        )
        accounts[move["id"]] = tuple(sorted(ln["account_id"][0] for ln in move_lines))

    notices = _duplicates(invoices, accounts)
    without_document = [i for i in invoices if i.attachment is None]
    if without_document:
        total = sum((i.total for i in without_document), Decimal("0.00"))
        notices.append(
            Notice(
                NoticeCode.OTHER_EXPENSES,
                "",
                {"count": len(without_document), "total": format_amount(total)},
            )
        )
    return invoices, notices
