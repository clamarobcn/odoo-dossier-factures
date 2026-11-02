"""Presentation of amounts and dates shared by the outputs."""

from datetime import date
from decimal import Decimal

from .i18n import t
from .model import Dossier, Payment


def format_amount(amount: Decimal) -> str:
    """Return ``1.234,56``, the convention in Catalan."""
    return f"{amount:,.2f}".replace(",", "\0").replace(".", ",").replace("\0", ".")


def format_date(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def payment_form_label(payment: Payment) -> str:
    return " – ".join(part for part in (payment.form, payment.method) if part)


def describe_filters(dossier: Dossier) -> str:
    parts = []
    if dossier.analytic_account_name:
        parts.append(t("filters.project", name=dossier.analytic_account_name))
    if dossier.supplier_name:
        parts.append(t("filters.supplier", name=dossier.supplier_name))
    if dossier.params.include_receipts:
        parts.append(t("filters.receipts"))
    return " · ".join(parts) or t("filters.none")
