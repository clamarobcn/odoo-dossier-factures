"""Read everything the dossier needs from Odoo."""

from .attachments import detect_content_field, fetch_attachment_content
from .invoices import extract_dossier, list_companies, sort_key
from .lookups import (
    ConnectionInfo,
    check_connection,
    check_required_fields,
    list_analytic_accounts,
    list_suppliers,
    list_years,
)

__all__ = [
    "ConnectionInfo",
    "check_connection",
    "check_required_fields",
    "detect_content_field",
    "extract_dossier",
    "fetch_attachment_content",
    "list_analytic_accounts",
    "list_companies",
    "list_suppliers",
    "list_years",
    "sort_key",
]
