"""Values for the interface drop-downs, and the connection check."""

import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..model import Company
from ..odoo_client import MissingFieldsError, OdooBackend
from .attachments import ATTACHMENT_FIELDS, detect_content_field
from .invoices import MOVE_FIELDS, base_domain, list_companies, year_domain

REQUIRED_FIELDS = {
    "account.move": [*MOVE_FIELDS, "date"],
    "account.move.line": [
        "account_id",
        "account_type",
        "analytic_distribution",
        "balance",
        "date",
        "display_type",
        "journal_id",
        "matched_credit_ids",
        "matched_debit_ids",
        "move_id",
        "move_type",
        "parent_state",
        "payment_id",
        "price_total",
        "statement_line_id",
    ],
    "account.partial.reconcile": ["amount", "credit_move_id", "debit_move_id", "exchange_move_id"],
    "account.payment": ["amount", "date", "name", "payment_method_line_id", "state"],
    "account.payment.method.line": ["name"],
    "account.bank.statement.line": ["date", "payment_ref"],
    "account.journal": ["name", "type"],
    "ir.attachment": [*ATTACHMENT_FIELDS, "res_model"],
    "account.analytic.account": ["code", "name", "plan_id"],
    "res.partner": ["name", "vat"],
    "res.company": ["currency_id", "name", "vat"],
}


@dataclass(frozen=True)
class ConnectionInfo:
    version: str
    user_name: str
    user_login: str
    companies: list[Company]
    invoice_count: int
    content_field: str


def check_required_fields(backend: OdooBackend) -> None:
    """Fail early and clearly when an Odoo upgrade has changed what the application reads."""
    missing: dict[str, list[str]] = {}
    for model, fields in REQUIRED_FIELDS.items():
        available = backend.fields_get(model)
        absent = [f for f in fields if f not in available]
        if absent:
            missing[model] = absent
    if missing:
        raise MissingFieldsError(missing)


def check_connection(backend: OdooBackend) -> ConnectionInfo:
    version = backend.server_version().get("version", "")
    uid = backend.context_get().get("uid")
    users = backend.read("res.users", [uid], ["name", "login"]) if uid else []
    check_required_fields(backend)
    return ConnectionInfo(
        version=version,
        user_name=users[0]["name"] if users else "",
        user_login=users[0]["login"] if users else "",
        companies=list_companies(backend),
        invoice_count=backend.search_count("account.move", base_domain()),
        content_field=detect_content_field(backend),
    )


def list_years(backend: OdooBackend) -> list[int]:
    """From the current year back to the oldest invoice, most recent first."""
    current = date.today().year
    oldest = backend.search_read(
        "account.move", base_domain(), ["invoice_date"], order="invoice_date asc", limit=1
    )
    first = int(oldest[0]["invoice_date"][:4]) if oldest else current
    return list(range(max(first, current), min(first, current) - 1, -1))


def list_analytic_accounts(backend: OdooBackend) -> list[dict[str, Any]]:
    return [
        {
            "id": a["id"],
            "name": f"[{a['code']}] {a['name']}" if a["code"] else a["name"],
            "plan": a["plan_id"][1] if a["plan_id"] else "",
        }
        for a in backend.search_read_all(
            "account.analytic.account", [], ["name", "code", "plan_id"], order="name asc, id asc"
        )
    ]


def _alphabetical(text: str) -> str:
    """Sort key that ignores case and accents, so that "Àlfa" comes before "Beta"."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def list_suppliers(backend: OdooBackend, year: int) -> list[dict[str, Any]]:
    """Suppliers that actually have invoices that year."""
    moves = backend.search_read_all(
        "account.move", base_domain() + year_domain(year), ["partner_id"]
    )
    partners = {m["partner_id"][0]: m["partner_id"][1] for m in moves if m["partner_id"]}
    return [
        {"id": pid, "name": name}
        for pid, name in sorted(partners.items(), key=lambda kv: _alphabetical(kv[1]))
    ]
