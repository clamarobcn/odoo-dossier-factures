"""Payments applied to each invoice, read from the reconciled accounting lines.

Validated against a production database (see docs/validacio-odoo.md):

* ``matched_payment_ids`` is not reliable: it also returns cancelled payments and
  payments that were never reconciled with the invoice.
* ``account.partial.reconcile`` gives the amount applied to each invoice, which matters
  when one payment covers several invoices, and also covers bank statement lines and
  credit notes that have no ``account.payment``.
* ``invoice_payments_widget`` is display-oriented and localized, so it only cross-checks.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from ..i18n import t
from ..model import Notice, NoticeCode, Payment, PaymentKind, money
from ..odoo_client import OdooBackend, chunks

SETTLEMENT_ACCOUNT_TYPES = ["liability_payable", "asset_receivable"]
INACTIVE_PAYMENT_STATES = {"draft", "canceled", "rejected"}
JOURNAL_FORM_KEYS = {"bank": "bank", "cash": "cash", "credit": "card"}
CROSS_CHECK_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class _Applied:
    move_id: int
    partial_id: int
    amount: Decimal
    counterpart: dict[str, Any]


def payment_form(journal_name: str, journal_type: str, overrides: dict[str, str]) -> str:
    """Label for the payment form: journal overrides win over the journal type."""
    if journal_name in overrides:
        return overrides[journal_name]
    key = JOURNAL_FORM_KEYS.get(journal_type)
    return t(f"payment_form.{key}") if key else journal_name


def _id(value: Any) -> int | None:
    """Return the id of a many2one value, which Odoo sends as ``[id, name]`` or ``False``."""
    return value[0] if value else None


def _reconciled_lines(backend: OdooBackend, move_ids: list[int]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for batch in chunks(move_ids):
        lines.extend(
            backend.search_read_all(
                "account.move.line",
                [("move_id", "in", list(batch)), ("account_type", "in", SETTLEMENT_ACCOUNT_TYPES)],
                ["move_id", "matched_debit_ids", "matched_credit_ids"],
            )
        )
    return lines


def _applied_amounts(backend: OdooBackend, lines: list[dict[str, Any]]) -> list[_Applied]:
    partial_ids = [p for ln in lines for p in ln["matched_debit_ids"] + ln["matched_credit_ids"]]
    partials = {
        p["id"]: p
        for p in backend.read(
            "account.partial.reconcile",
            partial_ids,
            ["amount", "debit_move_id", "credit_move_id", "exchange_move_id"],
        )
    }
    other_ids = [
        _id(side) for p in partials.values() for side in (p["debit_move_id"], p["credit_move_id"])
    ]
    counterparts = {
        ln["id"]: ln
        for ln in backend.read(
            "account.move.line",
            other_ids,
            ["move_id", "date", "journal_id", "payment_id", "statement_line_id"],
        )
    }
    exchange_moves = {_id(p["exchange_move_id"]) for p in partials.values()} - {None}

    applied: list[_Applied] = []
    for line in lines:
        for partial_id in line["matched_debit_ids"] + line["matched_credit_ids"]:
            partial = partials[partial_id]
            debit, credit = _id(partial["debit_move_id"]), _id(partial["credit_move_id"])
            counterpart = counterparts[credit if debit == line["id"] else debit]
            # Exchange differences are accounting adjustments, not money that moved.
            if _id(counterpart["move_id"]) in exchange_moves:
                continue
            applied.append(
                _Applied(_id(line["move_id"]), partial_id, money(partial["amount"]), counterpart)
            )
    return applied


def _cross_check(move: dict[str, Any], applied: list[_Applied]) -> Notice | None:
    widget = move.get("invoice_payments_widget")
    content = widget.get("content") if isinstance(widget, dict) else None
    if content is None:
        return None
    from_widget = sum(
        (money(c.get("amount")) for c in content if not c.get("is_exchange")), Decimal("0.00")
    )
    from_lines = sum((a.amount for a in applied), Decimal("0.00"))
    if abs(from_widget - from_lines) <= CROSS_CHECK_TOLERANCE:
        return None
    return Notice(
        NoticeCode.PAYMENT_MISMATCH,
        move["name"],
        {"lines": str(from_lines), "widget": str(from_widget)},
    )


def _unreconciled(
    move: dict[str, Any], applied: list[_Applied], linked: dict[int, dict[str, Any]]
) -> list[Notice]:
    used = {_id(a.counterpart["payment_id"]) for a in applied}
    notices = []
    for payment_id in move.get("matched_payment_ids") or []:
        payment = linked.get(payment_id)
        if not payment or payment_id in used or payment["state"] in INACTIVE_PAYMENT_STATES:
            continue
        notices.append(
            Notice(
                NoticeCode.UNRECONCILED_PAYMENT,
                move["name"],
                {
                    "payment": payment["name"],
                    "date": payment["date"],
                    "amount": str(money(payment["amount"])),
                },
            )
        )
    return notices


def fetch_payments(
    backend: OdooBackend, moves: list[dict[str, Any]], form_overrides: dict[str, str] | None = None
) -> tuple[dict[int, list[Payment]], list[Notice]]:
    """Return the payments of each move and the notices found while reading them."""
    overrides = form_overrides or {}
    applied = _applied_amounts(backend, _reconciled_lines(backend, [m["id"] for m in moves]))

    payment_ids = {_id(a.counterpart["payment_id"]) for a in applied} - {None}
    linked_ids = {p for m in moves for p in m.get("matched_payment_ids") or []}
    payments = {
        p["id"]: p
        for p in backend.read(
            "account.payment",
            list(payment_ids | linked_ids),
            ["name", "date", "amount", "state", "payment_method_line_id"],
        )
    }
    # The many2one label of a method line also repeats the journal, so read its plain name.
    methods = {
        m["id"]: m["name"]
        for m in backend.read(
            "account.payment.method.line",
            [
                _id(payments[i]["payment_method_line_id"])
                for i in payment_ids
                if payments[i]["payment_method_line_id"]
            ],
            ["name"],
        )
    }
    statement_lines = {
        s["id"]: s
        for s in backend.read(
            "account.bank.statement.line",
            [
                _id(a.counterpart["statement_line_id"])
                for a in applied
                if a.counterpart["statement_line_id"]
            ],
            ["date", "payment_ref"],
        )
    }
    journals = {
        j["id"]: j
        for j in backend.read(
            "account.journal", [_id(a.counterpart["journal_id"]) for a in applied], ["name", "type"]
        )
    }
    entries = {
        m["id"]: m
        for m in backend.read(
            "account.move",
            [
                _id(a.counterpart["move_id"])
                for a in applied
                if not a.counterpart["payment_id"] and not a.counterpart["statement_line_id"]
            ],
            ["name", "move_type"],
        )
    }

    by_move: dict[int, list[_Applied]] = defaultdict(list)
    for item in applied:
        by_move[item.move_id].append(item)

    result: dict[int, list[Payment]] = {}
    notices: list[Notice] = []
    for move in moves:
        items = by_move.get(move["id"], [])
        sign = -1 if move["move_type"] == "in_refund" else 1
        built = [
            _build(item, sign, payments, methods, statement_lines, journals, entries, overrides)
            for item in items
        ]
        result[move["id"]] = sorted(built, key=lambda p: (p.date, p.reference))
        if notice := _cross_check(move, items):
            notices.append(notice)
        notices.extend(_unreconciled(move, items, payments))
    return result, notices


def _build(
    item: _Applied,
    sign: int,
    payments: dict[int, dict[str, Any]],
    methods: dict[int, str],
    statement_lines: dict[int, dict[str, Any]],
    journals: dict[int, dict[str, Any]],
    entries: dict[int, dict[str, Any]],
    overrides: dict[str, str],
) -> Payment:
    line = item.counterpart
    journal = journals[_id(line["journal_id"])]
    form = payment_form(journal["name"], journal["type"], overrides)
    when, method, kind, reference = line["date"], "", PaymentKind.OTHER, ""

    if payment_id := _id(line["payment_id"]):
        payment = payments[payment_id]
        when, kind, reference = payment["date"], PaymentKind.PAYMENT, payment["name"]
        method = methods.get(_id(payment["payment_method_line_id"]), "")
    elif statement_id := _id(line["statement_line_id"]):
        statement = statement_lines[statement_id]
        when, kind = statement["date"], PaymentKind.BANK_STATEMENT
        reference = statement["payment_ref"] or ""
        method = t("payment_method.bank_statement")
    else:
        entry = entries[_id(line["move_id"])]
        reference = entry["name"]
        if entry["move_type"] in {"in_refund", "in_invoice"}:
            kind, form = PaymentKind.CREDIT_NOTE, t("payment_form.credit_note")

    return Payment(
        date=date.fromisoformat(when),
        journal_name=journal["name"],
        journal_type=journal["type"],
        form=form,
        method=method,
        amount=item.amount * sign,
        kind=kind,
        reference=reference,
    )
