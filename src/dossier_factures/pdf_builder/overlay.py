"""What is drawn around each invoice page: header band, footer and warning pages."""

from reportlab.lib.colors import HexColor
from reportlab.pdfgen.canvas import Canvas

from ..i18n import t
from ..model import Invoice
from .layout import BAND_HEIGHT, BAND_MARGIN, BAND_TOP_GAP, FOOTER_BASELINE
from .text import FONT, FONT_BOLD, band_lines, fit, wrap

BAND_FILL = HexColor("#E8ECF4")
BAND_LINE = HexColor("#1F3864")
INK = HexColor("#1A1A1A")
MUTED = HexColor("#595959")
WARNING = HexColor("#9C5700")
WARNING_FILL = HexColor("#FFF2CC")


def draw_footer(canvas: Canvas, size: tuple[float, float], left: str, current: int, total: int):
    width, _ = size
    canvas.setFillColor(MUTED)
    text, font_size = fit(left, FONT, 6.5, 6.5, width * 0.7)
    canvas.setFont(FONT, font_size)
    canvas.drawString(BAND_MARGIN, FOOTER_BASELINE, text)
    canvas.drawRightString(
        width - BAND_MARGIN, FOOTER_BASELINE, t("pdf.footer.page", current=current, total=total)
    )


def draw_band(
    canvas: Canvas, size: tuple[float, float], invoice: Invoice, page: int, page_count: int
) -> None:
    width, height = size
    top = height - BAND_TOP_GAP
    bottom = top - BAND_HEIGHT
    canvas.setFillColor(BAND_FILL)
    canvas.setStrokeColor(BAND_LINE)
    canvas.setLineWidth(0.6)
    canvas.rect(BAND_MARGIN, bottom, width - 2 * BAND_MARGIN, BAND_HEIGHT, fill=1, stroke=1)

    first, second = band_lines(invoice)
    pages = t("pdf.band.page", current=page, total=page_count)
    inner = width - 2 * BAND_MARGIN - 12
    canvas.setFillColor(INK)
    canvas.setFont(FONT, 7.5)
    canvas.drawRightString(width - BAND_MARGIN - 6, top - 14, pages)

    text, font_size = fit(first, FONT_BOLD, 8.5, 6.0, inner - 50)
    canvas.setFont(FONT_BOLD, font_size)
    canvas.drawString(BAND_MARGIN + 6, top - 14, text)

    text, font_size = fit(second, FONT, 7.5, 5.5, inner)
    canvas.setFont(FONT, font_size)
    canvas.drawString(BAND_MARGIN + 6, top - 27, text)


def draw_warning(canvas: Canvas, size: tuple[float, float], message: str, detail: str = "") -> None:
    width, height = size
    box_width = width * 0.7
    left = (width - box_width) / 2
    lines = wrap(message, FONT_BOLD, 14, box_width - 40)
    detail_lines = wrap(detail, FONT, 9, box_width - 40) if detail else []
    box_height = 40 + 18 * len(lines) + 12 * len(detail_lines)
    bottom = (height - box_height) / 2
    canvas.setFillColor(WARNING_FILL)
    canvas.setStrokeColor(WARNING)
    canvas.rect(left, bottom, box_width, box_height, fill=1, stroke=1)
    canvas.setFillColor(WARNING)
    canvas.setFont(FONT_BOLD, 14)
    y = bottom + box_height - 28
    for line in lines:
        canvas.drawCentredString(width / 2, y, line)
        y -= 18
    canvas.setFont(FONT, 9)
    for line in detail_lines:
        canvas.drawCentredString(width / 2, y, line)
        y -= 12
