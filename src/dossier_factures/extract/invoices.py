"""Read the supplier invoices of a fiscal year and assemble the dossier data."""

import re
from collections.abc import Callable
from datetime import date
from typing import Any

from ..formatting import format_amount
from ..i18n import t
from ..model import Company, Dossier, ExtractParams, Invoice, Notice, NoticeCode, money
from ..odoo_client import OdooBackend
from .analytics import compute_imputations
from .attachments import select_attachments
from .payments import fetch_payments

MOVE_TYPES = ["in_invoice", "in_refund"]
RECEIPT_TYPE = "in_receipt"
MOVE_FIELDS = [
    "amount_tax_signed",
    "amount_total",
    "amount_total_signed",
    "amount_untaxed_signed",
    "company_id",
    "currency_id",
    "invoice_date",
    "invoice_payments_widget",
    "matched_payment_ids",
    "message_main_attachment_id",
    "move_type",
    "name",
    "partner_id",
    "payment_state",
    "ref",
]


def base_domain(include_receipts: bool = True) -> list:
    types = [*MOVE_TYPES, RECEIPT_TYPE] if include_receipts else MOVE_TYPES
    return [("move_type", "in", types), ("state", "=", "posted")]


def year_domain(year: int) -> list:
    return [("invoice_date", ">=", f"{year}-01-01"), ("invoice_date", "<=", f"{year}-12-31")]


def sort_key(name: str) -> list[int | str]:
    """Natural order, so that .../999 comes before .../1000."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name or "")]


def list_companies(backend: OdooBackend) -> list[Company]:
    return [
        Company(c["id"], c["name"], c["vat"] or "", c["currency_id"][1] if c["currency_id"] else "")
        for c in backend.search_read_all("res.company", [], ["name", "vat", "currency_id"])
    ]


def _resolve_company(backend: OdooBackend, company_id: int | None) -> Company:
    companies = list_companies(backend)
    if company_id is not None:
        return next(c for c in companies if c.id == company_id)
    if len(companies) == 1:
        return companies[0]
    uid = backend.context_get()["uid"]
    current = backend.read("res.users", [uid], ["company_id"])[0]["company_id"][0]
    return next(c for c in companies if c.id == current)


def _to_invoice(move: dict[str, Any], partner: dict[str, Any]) -> Invoice:
    # Purchase receipts may have no supplier, so the partner can be empty.
    sign = -1 if move["move_type"] == "in_refund" else 1
    return Invoice(
        id=move["id"],
        name=move["name"],
        ref=move["ref"] or "",
        partner_id=move["partner_id"][0] if move["partner_id"] else None,
        partner_name=partner.get("name") or "",
        partner_vat=partner.get("vat") or "",
        invoice_date=date.fromisoformat(move["invoice_date"]),
        # Signed fields are in company currency; the sign depends on the move type.
        untaxed=money(abs(move["amount_untaxed_signed"])) * sign,
        tax=money(abs(move["amount_tax_signed"])) * sign,
        total=money(abs(move["amount_total_signed"])) * sign,
        payment_state=move["payment_state"],
        move_type=move["move_type"],
    )


def _filter_label(backend: OdooBackend, model: str, record_id: int | None) -> str:
    if record_id is None:
        return ""
    records = backend.read(model, [record_id], ["name"])
    return records[0]["name"] if records else str(record_id)


def _excluded_receipts(backend: OdooBackend, scope: list) -> list[Notice]:
    """Purchase receipts share the numbering of the invoices, and their absence looks like a gap."""
    receipts = backend.search_read_all(
        "account.move",
        [("move_type", "=", RECEIPT_TYPE), ("state", "=", "posted"), *scope],
        ["amount_total_signed"],
    )
    if not receipts:
        return []
    total = sum((money(abs(r["amount_total_signed"])) for r in receipts), money(0))
    return [
        Notice(
            NoticeCode.RECEIPTS_EXCLUDED,
            "",
            {"count": len(receipts), "total": format_amount(total)},
        )
    ]


def extract_dossier(
    backend: OdooBackend,
    params: ExtractParams,
    form_overrides: dict[str, str] | None = None,
    progress: Callable[[str], None] = lambda _message: None,
) -> Dossier:
    """Read everything the outputs need, except the content of the attachments."""
    company = _resolve_company(backend, params.company_id)
    backend.default_context["allowed_company_ids"] = [company.id]

    progress(t("step.reading_invoices"))
    scope = [*year_domain(params.year), ("company_id", "=", company.id)]
    if params.supplier_id is not None:
        scope.append(("partner_id", "=", params.supplier_id))
    domain = [*base_domain(params.include_receipts), *scope]
    moves = backend.search_read_all("account.move", domain, MOVE_FIELDS)

    imputations = {}
    if params.analytic_account_id is not None:
        imputations = compute_imputations(backend, moves, params.analytic_account_id)
        moves = [m for m in moves if m["id"] in imputations]

    moves.sort(key=lambda m: (sort_key(m["name"]), m["id"]))

    progress(t("step.reading_payments"))
    payments, notices = fetch_payments(backend, moves, form_overrides)

    progress(t("step.reading_attachments"))
    attachments, attachment_notices = select_attachments(backend, moves)
    notices.extend(attachment_notices)

    partners = {
        p["id"]: p
        for p in backend.read(
            "res.partner", [m["partner_id"][0] for m in moves if m["partner_id"]], ["name", "vat"]
        )
    }

    invoices: list[Invoice] = []
    for number, move in enumerate(moves, start=1):
        partner = partners[move["partner_id"][0]] if move["partner_id"] else {}
        invoice = _to_invoice(move, partner)
        invoice.order_number = number
        invoice.payments = payments[move["id"]]
        invoice.attachment = attachments[move["id"]]
        invoice.imputation = imputations.get(move["id"])
        invoices.append(invoice)
        if invoice.partner_id is None:
            notices.append(Notice(NoticeCode.MISSING_SUPPLIER, invoice.name))
        elif not invoice.partner_vat:
            notices.append(
                Notice(NoticeCode.MISSING_VAT, invoice.name, {"supplier": invoice.partner_name})
            )
        if not invoice.payments:
            notices.append(Notice(NoticeCode.NO_PAYMENT, invoice.name))
        if move["currency_id"][1] != company.currency:
            notices.append(
                Notice(
                    NoticeCode.FOREIGN_CURRENCY, invoice.name, {"currency": move["currency_id"][1]}
                )
            )

    if not params.include_receipts and params.analytic_account_id is None:
        notices.extend(_excluded_receipts(backend, scope))

    return Dossier(
        company=company,
        params=params,
        invoices=invoices,
        notices=notices,
        analytic_account_name=_filter_label(
            backend, "account.analytic.account", params.analytic_account_id
        ),
        supplier_name=_filter_label(backend, "res.partner", params.supplier_id),
    )
