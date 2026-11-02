"""Fonts, text fitting and the wording of the header band."""

import os

import reportlab
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from ..formatting import format_amount, format_date
from ..i18n import t
from ..model import Invoice

FONT = "DossierSans"
FONT_BOLD = "DossierSans-Bold"
ELLIPSIS = "…"


def register_fonts() -> None:
    """Embed Bitstream Vera, which ships with reportlab and covers Catalan and the euro sign."""
    if FONT in pdfmetrics.getRegisteredFontNames():
        return
    folder = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
    pdfmetrics.registerFont(TTFont(FONT, os.path.join(folder, "Vera.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, os.path.join(folder, "VeraBd.ttf")))


def truncate(text: str, font: str, size: float, width: float) -> str:
    if pdfmetrics.stringWidth(text, font, size) <= width:
        return text
    while text and pdfmetrics.stringWidth(text + ELLIPSIS, font, size) > width:
        text = text[:-1]
    return text.rstrip() + ELLIPSIS


def fit(text: str, font: str, size: float, minimum: float, width: float) -> tuple[str, float]:
    """Shrink text to fit, and cut it only when the smallest size is not enough."""
    while size > minimum and pdfmetrics.stringWidth(text, font, size) > width:
        size -= 0.25
    return truncate(text, font, size, width), size


def wrap(text: str, font: str, size: float, width: float) -> list[str]:
    """Split into lines; a single word wider than the column is cut."""
    lines = simpleSplit(text or "", font, size, width) or [""]
    return [truncate(line, font, size, width) for line in lines]


def _document_label(invoice: Invoice) -> str:
    if invoice.is_other_expense:
        return ""
    key = "receipt" if invoice.is_receipt else "refund" if invoice.is_refund else "invoice"
    return t(f"pdf.band.{key}", ref=invoice.ref).strip()


def payment_summary(invoice: Invoice) -> str:
    """Return ``Pagada 12/03/2026 – Banc``, listing every payment of the invoice."""
    state = t(f"payment_state.{invoice.payment_state}") if invoice.payment_state else ""
    if not invoice.payments:
        return state
    details = "; ".join(f"{format_date(p.date)} – {p.form}" for p in invoice.payments)
    return f"{state} {details}".strip()


def order_label(invoice: Invoice) -> str:
    return t("pdf.band.order", number=f"{invoice.order_number:03d}")


def band_lines(invoice: Invoice) -> tuple[str, str]:
    supplier = invoice.partner_name or t("pdf.band.no_supplier")
    first = " · ".join((order_label(invoice), invoice.name, supplier))
    parts = (
        _document_label(invoice),
        format_date(invoice.invoice_date),
        f"{format_amount(invoice.total)} €",
        payment_summary(invoice),
    )
    second = " · ".join(part for part in parts if part)
    return first, second


def outline_title(invoice: Invoice) -> str:
    supplier = invoice.partner_name or t("pdf.band.no_supplier")
    return f"{invoice.order_number:03d} – {invoice.name} – {supplier}"
