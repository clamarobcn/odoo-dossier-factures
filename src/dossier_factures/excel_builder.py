"""The invoice register as an Excel workbook.

The workbook goes to third parties, so it only carries what justifies the invoices.
Internal review notices stay in the on-screen summary.
"""

import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .formatting import describe_filters, format_date, payment_form_label
from .i18n import t
from .model import Dossier, Invoice, Payment

HEADER_ROW = 6
FIRST_DATA_ROW = HEADER_ROW + 1
DATE_FORMAT = "dd/mm/yyyy"
PERCENT_FORMAT = "0.00%"
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
FONT_NAME = "Calibri"
HEADER_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=10)
BODY_FONT = Font(name=FONT_NAME, size=10)
REPEATED_FONT = Font(name=FONT_NAME, size=10, color="808080")
TOTAL_FONT = Font(name=FONT_NAME, bold=True, size=10)
MARKED_FILL = PatternFill("solid", fgColor="FFF2CC")
THIN = Side(style="thin", color="BFBFBF")
CELL_BORDER = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
TOTAL_BORDER = Border(top=Side(style="medium"), bottom=Side(style="medium"))
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")
CENTER = Alignment(horizontal="center", vertical="top")
HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center", wrap_text=True)
SYMBOLS = {"EUR": "€"}


@dataclass(frozen=True)
class _Column:
    key: str
    width: float


BASE_COLUMNS = [
    _Column("order", 7),
    _Column("internal", 17),
    _Column("ref", 18),
    _Column("invoice_date", 11),
    _Column("supplier", 30),
    _Column("vat", 12),
    _Column("untaxed", 13),
    _Column("tax", 12),
    _Column("total", 13),
    _Column("payment_date", 11),
    _Column("payment_form", 24),
    _Column("amount_paid", 13),
    _Column("payment_state", 13),
    _Column("quarter", 8.5),
    _Column("page", 8),
]
PROJECT_COLUMNS = [_Column("percent", 10), _Column("imputed", 13)]
REMARKS_COLUMN = _Column("remarks", 15)


def excel_filename(year: int) -> str:
    return t("output.excel_filename", year=year)


def _clean(text: str) -> str:
    return ILLEGAL_CHARACTERS_RE.sub("", text or "")


def _money_format(currency: str) -> str:
    symbol = SYMBOLS.get(currency, currency)
    return f'#,##0.00\\ "{symbol}";[Red]-#,##0.00\\ "{symbol}"'


def _remarks(invoice: Invoice, document_remarks: dict[int, str]) -> str:
    parts = []
    if invoice.is_refund:
        parts.append(t("excel.remark.refund"))
    if invoice.is_receipt:
        parts.append(t("excel.remark.receipt"))
    if invoice.attachment is None:
        parts.append(t("excel.remark.no_document"))
    if invoice.id in document_remarks:
        parts.append(document_remarks[invoice.id])
    return "; ".join(parts)


class _Sheet:
    def __init__(self, ws: Worksheet, dossier: Dossier, columns: list[_Column]) -> None:
        self.ws = ws
        self.money_format = _money_format(dossier.company.currency)
        self.index = {c.key: position for position, c in enumerate(columns, start=1)}
        self.columns = columns

    def letter(self, key: str) -> str:
        return get_column_letter(self.index[key])

    def text(self, row: int, key: str, value: str, font: Font = BODY_FONT) -> None:
        # Forced to text: a supplier reference starting with "=" must not become a formula.
        cell = self.ws.cell(row=row, column=self.index[key])
        cell.value = _clean(value)
        cell.data_type = "s"
        cell.font = font
        cell.alignment = WRAP

    def number(self, row: int, key: str, value: int, font: Font = BODY_FONT) -> None:
        cell = self.ws.cell(row=row, column=self.index[key], value=value)
        cell.font = font
        cell.alignment = CENTER

    def amount(self, row: int, key: str, value: Decimal) -> None:
        cell = self.ws.cell(row=row, column=self.index[key], value=value)
        cell.number_format = self.money_format
        cell.font = BODY_FONT
        cell.alignment = TOP

    def date(self, row: int, key: str, value: date, font: Font = BODY_FONT) -> None:
        cell = self.ws.cell(row=row, column=self.index[key], value=value)
        cell.number_format = DATE_FORMAT
        cell.font = font
        cell.alignment = CENTER


def _write_title(ws: Worksheet, dossier: Dossier, generated_on: date) -> None:
    company = dossier.company
    entity = _clean(company.name)
    if company.vat:
        entity += f" · {t('excel.vat', vat=_clean(company.vat))}"
    lines = [
        (entity, Font(name=FONT_NAME, bold=True, size=14)),
        (
            f"{t('excel.title')} · {t('excel.fiscal_year', year=dossier.params.year)}",
            Font(name=FONT_NAME, bold=True, size=11),
        ),
        (_clean(describe_filters(dossier)), BODY_FONT),
        (
            t("excel.generated", date=format_date(generated_on)),
            Font(name=FONT_NAME, size=10, color="595959"),
        ),
    ]
    for row, (text, font) in enumerate(lines, start=1):
        cell = ws.cell(row=row, column=1)
        cell.value = text
        cell.data_type = "s"
        cell.font = font


def _write_header(sheet: _Sheet) -> None:
    for column in sheet.columns:
        cell = sheet.ws.cell(
            row=HEADER_ROW, column=sheet.index[column.key], value=t(f"excel.column.{column.key}")
        )
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGNMENT
        cell.border = CELL_BORDER
        sheet.ws.column_dimensions[get_column_letter(sheet.index[column.key])].width = column.width
    sheet.ws.row_dimensions[HEADER_ROW].height = 42


def _write_identity(
    sheet: _Sheet, row: int, invoice: Invoice, page: int | None, font: Font
) -> None:
    sheet.number(row, "order", invoice.order_number, font)
    sheet.text(row, "internal", invoice.name, font)
    sheet.text(row, "ref", invoice.ref, font)
    sheet.date(row, "invoice_date", invoice.invoice_date, font)
    sheet.text(row, "supplier", invoice.partner_name, font)
    sheet.text(row, "vat", invoice.partner_vat, font)
    sheet.text(row, "payment_state", t(f"payment_state.{invoice.payment_state}"), font)
    sheet.text(row, "quarter", t("excel.quarter", quarter=invoice.quarter), font)
    if page is not None:
        sheet.number(row, "page", page, font)


def _write_payment(sheet: _Sheet, row: int, payment: Payment) -> None:
    sheet.date(row, "payment_date", payment.date)
    sheet.text(row, "payment_form", payment_form_label(payment))
    sheet.amount(row, "amount_paid", payment.amount)


def _write_invoice(
    sheet: _Sheet,
    row: int,
    invoice: Invoice,
    page: int | None,
    has_project: bool,
    document_remarks: dict[int, str],
) -> int:
    """Write an invoice and return the next free row.

    Each extra payment takes a row of its own, so "Import pagat" stays numeric. Those rows
    repeat the identifying columns in grey, which keeps them when filtering by supplier,
    and leave the invoice amounts empty, so that nothing is counted twice.
    """
    _write_identity(sheet, row, invoice, page, BODY_FONT)
    sheet.amount(row, "untaxed", invoice.untaxed)
    sheet.amount(row, "tax", invoice.tax)
    sheet.amount(row, "total", invoice.total)
    if has_project and invoice.imputation:
        percent = sheet.ws.cell(
            row=row, column=sheet.index["percent"], value=invoice.imputation.percent / 100
        )
        percent.number_format = PERCENT_FORMAT
        percent.font = BODY_FONT
        percent.alignment = TOP
        sheet.amount(row, "imputed", invoice.imputation.amount)
    remarks = _remarks(invoice, document_remarks)
    sheet.text(row, "remarks", remarks)

    if invoice.payments:
        _write_payment(sheet, row, invoice.payments[0])
    for payment in invoice.payments[1:]:
        row += 1
        _write_identity(sheet, row, invoice, page, REPEATED_FONT)
        _write_payment(sheet, row, payment)
    return row + 1


def _write_totals(sheet: _Sheet, row: int, dossier: Dossier, has_project: bool) -> dict[str, str]:
    """Write the totals and return each formula cell with its value, for ``_store_results``."""
    last = row - 1
    values = {
        "untaxed": dossier.total_untaxed,
        "tax": dossier.total_tax,
        "total": dossier.total,
        "amount_paid": dossier.total_paid,
    }
    if has_project:
        values["imputed"] = sum(
            (i.imputation.amount for i in dossier.invoices if i.imputation), Decimal("0.00")
        )
    results: dict[str, str] = {}
    for key, value in values.items():
        letter = sheet.letter(key)
        # SUBTOTAL ignores rows hidden by a filter, so the total follows what is shown.
        cell = sheet.ws.cell(
            row=row,
            column=sheet.index[key],
            value=f"=SUBTOTAL(109,{letter}{FIRST_DATA_ROW}:{letter}{last})",
        )
        cell.number_format = sheet.money_format
        results[cell.coordinate] = str(value)
    sheet.text(row, "internal", t("excel.total"))
    count_key = (
        "excel.invoice_count_with_receipts"
        if dossier.params.include_receipts
        else "excel.invoice_count"
    )
    sheet.text(row, "supplier", t(count_key, count=len(dossier.invoices)))
    for position in range(1, len(sheet.columns) + 1):
        cell = sheet.ws.cell(row=row, column=position)
        cell.font = TOTAL_FONT
        cell.border = TOTAL_BORDER
    return results


def _setup_page(ws: Worksheet, last_row: int, column_count: int, dossier: Dossier) -> None:
    ws.freeze_panes = ws.cell(row=FIRST_DATA_ROW, column=3)
    last_column = get_column_letter(column_count)
    ws.auto_filter.ref = f"A{HEADER_ROW}:{last_column}{last_row - 1}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f"{HEADER_ROW}:{HEADER_ROW}"
    ws.print_area = f"A1:{last_column}{last_row}"
    ws.print_options.horizontalCentered = True
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.page_margins.top = 0.5
    ws.page_margins.bottom = 0.6
    ws.oddFooter.left.text = _clean(dossier.company.name).replace("&", "&&")
    ws.oddFooter.left.size = 8
    ws.oddFooter.right.text = t("excel.page_footer")
    ws.oddFooter.right.size = 8


def _store_results(path: Path, results: dict[str, str]) -> None:
    """Save the value of each formula next to it.

    openpyxl leaves formula results empty, and viewers that do not calculate would show a
    blank total: Excel's Protected View for a file received by email, mobile previews...
    """
    sheet_name = "xl/worksheets/sheet1.xml"
    with zipfile.ZipFile(path) as source:
        items = [(info, source.read(info.filename)) for info in source.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for info, data in items:
            if info.filename == sheet_name:
                xml = data.decode("utf-8")
                for coordinate, value in results.items():
                    xml, count = re.subn(
                        rf'(<c r="{coordinate}"[^>]*><f>[^<]*</f>)<v\s*/?>(?:</v>)?',
                        rf"\g<1><v>{value}</v>",
                        xml,
                    )
                    if count != 1:
                        raise RuntimeError(f"Formula cell not found: {coordinate}")
                data = xml.encode("utf-8")
            target.writestr(info, data)


def build_workbook(
    dossier: Dossier,
    path: Path,
    pages: dict[int, int] | None = None,
    document_remarks: dict[int, str] | None = None,
    generated_on: date | None = None,
) -> None:
    """Write the register.

    ``pages`` maps invoice id to its first page in the dossier; with one PDF per quarter,
    that page counts within the quarter's PDF. ``document_remarks`` adds problems found in
    a document, such as a file that could not be read.
    """
    pages = pages or {}
    document_remarks = document_remarks or {}
    generated_on = generated_on or date.today()
    has_project = dossier.params.analytic_account_id is not None
    columns = [*BASE_COLUMNS, *(PROJECT_COLUMNS if has_project else []), REMARKS_COLUMN]

    workbook = Workbook()
    ws = workbook.active
    ws.title = t("excel.sheet", year=dossier.params.year)
    workbook.properties.creator = "DossierFactures"
    workbook.properties.title = f"{t('excel.title')} {dossier.params.year}"
    workbook.calculation.fullCalcOnLoad = True

    sheet = _Sheet(ws, dossier, columns)
    _write_title(ws, dossier, generated_on)
    _write_header(sheet)
    row = FIRST_DATA_ROW
    for invoice in dossier.invoices:
        first = row
        row = _write_invoice(
            sheet, row, invoice, pages.get(invoice.id), has_project, document_remarks
        )
        needs_review = invoice.attachment is None or invoice.id in document_remarks
        for current in range(first, row):
            for position in range(1, len(columns) + 1):
                cell = ws.cell(row=current, column=position)
                cell.border = CELL_BORDER
                if needs_review:
                    cell.fill = MARKED_FILL
    results = _write_totals(sheet, row, dossier, has_project)
    _setup_page(ws, row, len(columns), dossier)

    # Written next to the target and swapped in, so a failure never leaves half a file.
    path = Path(path)
    with tempfile.TemporaryDirectory(dir=path.parent) as work:
        temporary = Path(work) / path.name
        workbook.save(temporary)
        _store_results(temporary, results)
        os.replace(temporary, path)
