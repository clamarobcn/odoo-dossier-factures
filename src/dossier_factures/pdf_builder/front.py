"""Cover and index of a volume."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from reportlab.lib.colors import HexColor, white
from reportlab.pdfgen.canvas import Canvas

from ..formatting import describe_filters, format_amount, format_date, payment_form_label
from ..i18n import t
from ..model import Dossier, Invoice
from .layout import LANDSCAPE, PORTRAIT
from .overlay import BAND_LINE, INK, MUTED, WARNING_FILL, draw_footer
from .text import FONT, FONT_BOLD, wrap

SIZE = 6.5
LEADING = 8.0
PADDING = 2.0
MARGIN = 24.0
HEADER_HEIGHT = 20.0
TITLE_SPACE = 30.0
FOOTER_SPACE = 24.0
LINK = HexColor("#1F4E9E")
RULE = HexColor("#D0D5DD")

RIGHT_ALIGNED = {"untaxed", "tax", "total", "amount_paid", "imputed", "percent", "page"}
BASE_WIDTHS = {
    "order": 24,
    "internal": 64,
    "ref": 66,
    "invoice_date": 40,
    "supplier": 120,
    "vat": 50,
    "untaxed": 46,
    "tax": 40,
    "total": 46,
    "payment_date": 40,
    "payment_form": 100,
    "amount_paid": 46,
    "payment_state": 52,
    "page": 24,
}
PROJECT_WIDTHS = {"percent": 28, "imputed": 44}
PROJECT_REDUCTIONS = {"supplier": 42, "payment_form": 22, "ref": 8}


@dataclass
class LinkArea:
    """A clickable rectangle on an index page, leading to an invoice."""

    index_page: int
    rect: tuple[float, float, float, float]
    invoice_id: int


@dataclass
class _Row:
    cells: dict[str, list[str]]
    height: float
    invoice_id: int | None = None
    highlighted: bool = False
    bold: bool = False


@dataclass
class IndexLayout:
    columns: dict[str, float]
    pages: list[list[_Row]] = field(default_factory=list)


def _columns(has_project: bool) -> dict[str, float]:
    widths = dict(BASE_WIDTHS)
    if has_project:
        for key, reduction in PROJECT_REDUCTIONS.items():
            widths[key] -= reduction
        page = widths.pop("page")
        widths.update(PROJECT_WIDTHS)
        widths["page"] = page
    available = LANDSCAPE[0] - 2 * MARGIN
    factor = available / sum(widths.values())
    return {key: width * factor for key, width in widths.items()}


def _invoice_row(
    invoice: Invoice, first_page: int, columns: dict[str, float], highlighted: bool
) -> _Row:
    def fit(key: str, text: str) -> list[str]:
        return wrap(text, FONT, SIZE, columns[key] - 2 * PADDING)

    dates: list[str] = []
    forms: list[str] = []
    paid: list[str] = []
    for payment in invoice.payments:
        lines = fit("payment_form", payment_form_label(payment))
        forms.extend(lines)
        dates.extend([format_date(payment.date), *[""] * (len(lines) - 1)])
        paid.extend([format_amount(payment.amount), *[""] * (len(lines) - 1)])

    cells = {
        "order": [f"{invoice.order_number:03d}"],
        "internal": fit("internal", invoice.name),
        "ref": fit("ref", invoice.ref),
        "invoice_date": [format_date(invoice.invoice_date)],
        "supplier": fit("supplier", invoice.partner_name),
        "vat": fit("vat", invoice.partner_vat),
        "untaxed": [format_amount(invoice.untaxed)],
        "tax": [format_amount(invoice.tax)],
        "total": [format_amount(invoice.total)],
        "payment_date": dates,
        "payment_form": forms,
        "amount_paid": paid,
        "payment_state": fit("payment_state", t(f"payment_state.{invoice.payment_state}")),
        "page": [str(first_page)],
    }
    if "imputed" in columns and invoice.imputation:
        cells["percent"] = [f"{invoice.imputation.percent:.2f}".replace(".", ",")]
        cells["imputed"] = [format_amount(invoice.imputation.amount)]
    height = max(len(lines) for lines in cells.values()) * LEADING + 2 * PADDING
    return _Row(cells, height, invoice.id, highlighted)


def _total_row(invoices: list[Invoice], columns: dict[str, float]) -> _Row:
    def add(values) -> str:
        return format_amount(sum(values, Decimal("0.00")))

    cells = {
        "internal": [t("pdf.index.total")],
        "untaxed": [add(i.untaxed for i in invoices)],
        "tax": [add(i.tax for i in invoices)],
        "total": [add(i.total for i in invoices)],
        "amount_paid": [add(i.amount_paid for i in invoices)],
    }
    if "imputed" in columns:
        cells["imputed"] = [add(i.imputation.amount for i in invoices if i.imputation)]
    return _Row(cells, LEADING + 2 * PADDING, bold=True)


def layout_index(
    invoices: list[Invoice],
    first_pages: dict[int, int],
    flagged: set[int],
    has_project: bool,
) -> IndexLayout:
    """Distribute the rows on pages. Their height does not depend on the page numbers."""
    columns = _columns(has_project)
    available = LANDSCAPE[1] - 2 * MARGIN - TITLE_SPACE - HEADER_HEIGHT - FOOTER_SPACE + MARGIN
    rows = [
        _invoice_row(i, first_pages.get(i.id, 0), columns, i.attachment is None or i.id in flagged)
        for i in invoices
    ]
    rows.append(_total_row(invoices, columns))
    layout = IndexLayout(columns)
    page: list[_Row] = []
    used = 0.0
    for row in rows:
        if page and used + row.height > available:
            layout.pages.append(page)
            page, used = [], 0.0
        page.append(row)
        used += row.height
    layout.pages.append(page)
    return layout


def _draw_cell(
    canvas: Canvas, key: str, lines: list[str], x: float, width: float, top: float, font: str
) -> None:
    canvas.setFont(font, SIZE)
    y = top - LEADING + 1
    for line in lines:
        if key in RIGHT_ALIGNED:
            canvas.drawRightString(x + width - 2 * PADDING, y, line)
        else:
            canvas.drawString(x + PADDING, y, line)
        y -= LEADING


def draw_index(
    canvas: Canvas,
    layout: IndexLayout,
    title: str,
    footer: str,
    first_number: int,
    total_pages: int,
) -> list[LinkArea]:
    links: list[LinkArea] = []
    width, height = LANDSCAPE
    for page_number, rows in enumerate(layout.pages):
        canvas.setPageSize(LANDSCAPE)
        canvas.setFillColor(BAND_LINE)
        canvas.setFont(FONT_BOLD, 12)
        canvas.drawString(MARGIN, height - MARGIN - 8, t("pdf.index.title"))
        canvas.setFillColor(MUTED)
        canvas.setFont(FONT, 8)
        canvas.drawRightString(width - MARGIN, height - MARGIN - 8, title)

        top = height - MARGIN - TITLE_SPACE + 8
        canvas.setFillColor(BAND_LINE)
        canvas.rect(
            MARGIN, top - HEADER_HEIGHT, width - 2 * MARGIN, HEADER_HEIGHT, fill=1, stroke=0
        )
        canvas.setFillColor(white)
        x = MARGIN
        for key, column_width in layout.columns.items():
            label = wrap(t(f"pdf.index.column.{key}"), FONT_BOLD, SIZE, column_width - 2 * PADDING)
            _draw_cell(canvas, key, label, x, column_width, top - 3, FONT_BOLD)
            x += column_width
        top -= HEADER_HEIGHT

        for row in rows:
            if row.highlighted:
                canvas.setFillColor(WARNING_FILL)
                canvas.rect(
                    MARGIN, top - row.height, width - 2 * MARGIN, row.height, fill=1, stroke=0
                )
            x = MARGIN
            font = FONT_BOLD if row.bold else FONT
            for key, column_width in layout.columns.items():
                canvas.setFillColor(LINK if key == "page" else INK)
                _draw_cell(
                    canvas, key, row.cells.get(key, []), x, column_width, top - PADDING, font
                )
                x += column_width
            canvas.setStrokeColor(BAND_LINE if row.bold else RULE)
            canvas.setLineWidth(0.8 if row.bold else 0.3)
            canvas.line(MARGIN, top - row.height, width - MARGIN, top - row.height)
            if row.invoice_id is not None:
                rect = (MARGIN, top - row.height, width - MARGIN, top)
                links.append(LinkArea(page_number, rect, row.invoice_id))
            top -= row.height

        draw_footer(canvas, LANDSCAPE, footer, first_number + page_number, total_pages)
        canvas.showPage()
    return links


def draw_cover(
    canvas: Canvas,
    dossier: Dossier,
    invoices: list[Invoice],
    volume_label: str,
    generated_on: date,
    footer: str,
    total_pages: int,
) -> None:
    width, height = PORTRAIT
    canvas.setPageSize(PORTRAIT)
    left = 70.0
    inner = width - 2 * left
    y = height - 170

    canvas.setFillColor(BAND_LINE)
    canvas.rect(left, y + 34, inner, 3, fill=1, stroke=0)
    canvas.setFont(FONT_BOLD, 20)
    for line in wrap(dossier.company.name, FONT_BOLD, 20, inner):
        canvas.drawString(left, y, line)
        y -= 25
    if dossier.company.vat:
        canvas.setFillColor(MUTED)
        canvas.setFont(FONT, 11)
        canvas.drawString(left, y, t("pdf.vat", vat=dossier.company.vat))
        y -= 18

    y -= 40
    canvas.setFillColor(INK)
    canvas.setFont(FONT_BOLD, 16)
    canvas.drawString(left, y, t("pdf.title"))
    y -= 24
    canvas.setFont(FONT, 14)
    subtitle = t("pdf.fiscal_year", year=dossier.params.year)
    if volume_label:
        subtitle = f"{subtitle} · {volume_label}"
    canvas.drawString(left, y, subtitle)
    y -= 56

    count_key = (
        "pdf.cover.count_with_receipts" if dossier.params.include_receipts else "pdf.cover.count"
    )
    total = sum((i.total for i in invoices), Decimal("0.00"))
    entries = [
        (t(count_key), str(len(invoices))),
        (t("pdf.cover.total"), f"{format_amount(total)} €"),
        (t("pdf.cover.filters"), describe_filters(dossier)),
        (t("pdf.cover.generated"), format_date(generated_on)),
    ]
    label_width = 150.0
    for label, value in entries:
        canvas.setFillColor(MUTED)
        canvas.setFont(FONT, 10)
        canvas.drawString(left, y, label)
        canvas.setFillColor(INK)
        canvas.setFont(FONT_BOLD, 10)
        lines = wrap(value, FONT_BOLD, 10, inner - label_width)
        for line in lines:
            canvas.drawString(left + label_width, y, line)
            y -= 14
        y -= 8

    draw_footer(canvas, PORTRAIT, footer, 1, total_pages)
    canvas.showPage()
