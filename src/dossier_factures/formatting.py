"""Presentation of amounts and dates shared by the outputs."""

from datetime import date
from decimal import Decimal


def format_amount(amount: Decimal) -> str:
    """Return ``1.234,56``, the convention in Catalan."""
    return f"{amount:,.2f}".replace(",", "\0").replace(".", ",").replace("\0", ".")


def format_date(value: date) -> str:
    return value.strftime("%d/%m/%Y")
