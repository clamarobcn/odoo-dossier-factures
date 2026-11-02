import os
import re
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from dossier_factures.excel_builder import HEADER_ROW, build_workbook, excel_filename
from dossier_factures.extract import extract_dossier
from dossier_factures.model import ExtractParams
from dossier_factures.paths import new_output_folder

YEAR = 2099
TODAY = date(2099, 12, 31)
COLUMNS = [
    "Núm. d'ordre",
    "Registre intern",
    "Núm. factura proveïdor",
    "Data factura",
    "Proveïdor",
    "NIF",
    "Base imposable",
    "IVA",
    "Total",
    "Data de pagament",
    "Forma de pagament",
    "Import pagat",
    "Estat del pagament",
    "Trimestre",
    "Pàgina al dossier",
    "Observacions",
]


@pytest.fixture
def dossier(backend):
    return extract_dossier(backend, ExtractParams(YEAR, include_other_expenses=False))


def build(dossier, tmp_path, **kwargs):
    path = tmp_path / excel_filename(dossier.params.year)
    build_workbook(dossier, path, generated_on=TODAY, **kwargs)
    return path


class Register:
    """Read a generated workbook back by column name."""

    def __init__(self, path, data_only=False):
        self.ws = load_workbook(path, data_only=data_only).active
        self.names = [c.value for c in self.ws[HEADER_ROW]]
        self.last = self.ws.max_row

    def value(self, row, column):
        return self.ws.cell(row=row, column=self.names.index(column) + 1).value

    def rows_of(self, internal):
        return [
            r
            for r in range(HEADER_ROW + 1, self.last)
            if self.value(r, "Registre intern") == internal
        ]

    def column(self, column):
        return [self.value(r, column) for r in range(HEADER_ROW + 1, self.last)]


def test_filename_keeps_the_agreed_name():
    assert excel_filename(2026) == "Registre de factures de proveïdors 2026.xlsx"


def test_columns_are_the_agreed_ones_in_order(dossier, tmp_path):
    assert Register(build(dossier, tmp_path)).names == COLUMNS


def test_title_block_shows_entity_year_filters_and_date(dossier, tmp_path):
    ws = Register(build(dossier, tmp_path)).ws
    assert ws["A1"].value == "Entitat d'Exemple · NIF G00000000"
    assert ws["A2"].value == "Registre de factures de proveïdors · Exercici 2099"
    assert ws["A3"].value == "Inclou rebuts de compra"
    assert ws["A4"].value == "Generat el 31/12/2099"


def test_filters_are_described(backend, tmp_path):
    dossier = extract_dossier(backend, ExtractParams(YEAR, analytic_account_id=7, supplier_id=1))
    assert (
        Register(build(dossier, tmp_path)).ws["A3"].value
        == "Projecte: Projecte Set · Proveïdor: Proveïdor Àlfa · Inclou rebuts de compra"
    )


def test_every_invoice_appears_once_with_amounts(dossier, tmp_path):
    register = Register(build(dossier, tmp_path))
    numbered = [n for n in register.column("Núm. d'ordre")]
    assert sorted(set(numbered)) == list(range(1, 14))
    amounts = [v for v in register.column("Total") if v is not None]
    assert len(amounts) == 13
    assert sum(Decimal(str(v)) for v in amounts) == dossier.total


def test_dates_are_real_dates_and_amounts_real_numbers(dossier, tmp_path):
    register = Register(build(dossier, tmp_path))
    row = register.rows_of("FC/2099/01/0001")[0]
    assert register.value(row, "Data factura").date() == date(2099, 1, 15)
    assert register.value(row, "Data de pagament").date() == date(2099, 1, 20)
    assert register.value(row, "Total") == 121
    assert register.value(row, "Import pagat") == 121
    assert register.value(row, "Forma de pagament") == "Banc – Transferència"
    assert register.value(row, "Estat del pagament") == "Pagada"
    assert register.value(row, "Trimestre") == "T1"


def test_money_format_is_euros(dossier, tmp_path):
    register = Register(build(dossier, tmp_path))
    cell = register.ws.cell(row=HEADER_ROW + 1, column=COLUMNS.index("Total") + 1)
    assert "€" in cell.number_format
    assert "[Red]" in cell.number_format


def test_several_payments_use_extra_rows_without_counting_the_invoice_twice(dossier, tmp_path):
    register = Register(build(dossier, tmp_path))
    rows = register.rows_of("FC/2099/01/0002")
    assert len(rows) == 2
    assert [register.value(r, "Import pagat") for r in rows] == [71, 50]
    assert [register.value(r, "Forma de pagament") for r in rows] == [
        "Efectiu – Manual",
        "Banc – Transferència",
    ]
    assert register.value(rows[0], "Total") == 121
    assert register.value(rows[1], "Total") is None
    assert register.value(rows[1], "Base imposable") is None
    assert register.value(rows[1], "Proveïdor") == "Proveïdor Àlfa"
    assert register.value(rows[1], "Núm. d'ordre") == register.value(rows[0], "Núm. d'ordre")


def test_refunds_are_negative_and_marked(dossier, tmp_path):
    register = Register(build(dossier, tmp_path))
    row = register.rows_of("RFC/2099/03/0001")[0]
    assert register.value(row, "Total") == -40
    assert register.value(row, "Import pagat") == -40
    assert "Rectificativa" in register.value(row, "Observacions")


def test_in_payment_label(dossier, tmp_path):
    register = Register(build(dossier, tmp_path))
    row = register.rows_of("FC/2099/04/0002")[0]
    assert register.value(row, "Estat del pagament") == "En procés de pagament"


def test_unpaid_invoice_has_empty_payment_columns(dossier, tmp_path):
    register = Register(build(dossier, tmp_path))
    row = register.rows_of("FC/2099/04/0001")[0]
    assert register.value(row, "Data de pagament") is None
    assert register.value(row, "Import pagat") is None
    assert register.value(row, "Estat del pagament") == "No pagada"


def test_missing_document_is_marked_and_highlighted(dossier, tmp_path):
    register = Register(build(dossier, tmp_path))
    row = register.rows_of("FC/2099/03/0001")[0]
    assert register.value(row, "Observacions") == "Sense document adjunt"
    cell = register.ws.cell(row=row, column=1)
    assert cell.fill.fgColor.rgb.endswith("FFF2CC")


def test_document_problem_is_marked(dossier, tmp_path):
    invoice = dossier.invoices[0]
    path = build(dossier, tmp_path, document_remarks={invoice.id: "Document il·legible"})
    register = Register(path)
    assert (
        register.value(register.rows_of(invoice.name)[0], "Observacions") == "Document il·legible"
    )


def test_internal_review_notices_are_not_in_the_workbook(dossier, tmp_path):
    register = Register(build(dossier, tmp_path))
    text = " ".join(
        str(c.value) for row in register.ws.iter_rows() for c in row if isinstance(c.value, str)
    )
    assert "PAG/7" not in text
    assert "conciliar" not in text


def test_page_numbers(dossier, tmp_path):
    invoice = dossier.invoices[1]
    register = Register(build(dossier, tmp_path, pages={invoice.id: 42}))
    assert {register.value(r, "Pàgina al dossier") for r in register.rows_of(invoice.name)} == {42}


def test_totals_are_formulas_over_the_data_rows(dossier, tmp_path):
    register = Register(build(dossier, tmp_path))
    for column in ("Base imposable", "IVA", "Total", "Import pagat"):
        formula = register.value(register.last, column)
        letter, first, last = re.fullmatch(
            r"=SUBTOTAL\(109,([A-Z]+)(\d+):\1(\d+)\)", formula
        ).groups()
        assert int(first) == HEADER_ROW + 1
        assert int(last) == register.last - 1
        assert (
            letter == register.ws.cell(row=1, column=register.names.index(column) + 1).column_letter
        )


def test_totals_also_have_stored_results(dossier, tmp_path):
    register = Register(build(dossier, tmp_path), data_only=True)
    assert Decimal(str(register.value(register.last, "Total"))) == dossier.total
    assert Decimal(str(register.value(register.last, "Import pagat"))) == dossier.total_paid
    assert Decimal(str(register.value(register.last, "Base imposable"))) == dossier.total_untaxed
    assert Decimal(str(register.value(register.last, "IVA"))) == dossier.total_tax


def test_stored_results_match_what_the_formula_would_calculate(dossier, tmp_path):
    path = build(dossier, tmp_path)
    data = Register(path, data_only=True)
    for column in ("Base imposable", "IVA", "Total", "Import pagat"):
        values = [Decimal(str(v)) for v in data.column(column) if v is not None]
        assert sum(values) == Decimal(str(data.value(data.last, column)))


def test_totals_row_counts_invoices(dossier, tmp_path):
    register = Register(build(dossier, tmp_path))
    assert register.value(register.last, "Registre intern") == "Total"
    assert register.value(register.last, "Proveïdor") == "13 factures i rebuts"


def test_print_and_freeze_setup(dossier, tmp_path):
    register = Register(build(dossier, tmp_path))
    ws = register.ws
    assert ws.freeze_panes == f"C{HEADER_ROW + 1}"
    assert ws.page_setup.orientation == "landscape"
    assert int(ws.page_setup.paperSize) == int(ws.PAPERSIZE_A4)
    assert ws.page_setup.fitToWidth == 1
    assert ws.page_setup.fitToHeight == 0
    assert ws.sheet_properties.pageSetUpPr.fitToPage is True
    assert ws.print_title_rows == f"${HEADER_ROW}:${HEADER_ROW}"
    assert ws.auto_filter.ref.endswith(str(register.last - 1))


def test_project_columns_only_appear_with_a_project_filter(backend, dossier, tmp_path):
    assert "% imputat al projecte" not in Register(build(dossier, tmp_path)).names
    filtered = extract_dossier(
        backend, ExtractParams(YEAR, analytic_account_id=7, include_other_expenses=False)
    )
    register = Register(build(filtered, tmp_path))
    row = register.rows_of("FC/2099/02/0001")[0]
    assert register.value(row, "% imputat al projecte") == pytest.approx(0.6667)
    assert register.value(row, "Import imputat al projecte") == 200
    assert register.names[-1] == "Observacions"
    results = Register(tmp_path / excel_filename(YEAR), data_only=True)
    assert results.value(results.last, "Import imputat al projecte") == 200


def test_text_that_looks_like_a_formula_stays_text(dossier, tmp_path):
    dossier.invoices[0].ref = "=HYPERLINK(1)"
    register = Register(build(dossier, tmp_path))
    cell = register.ws.cell(row=register.rows_of(dossier.invoices[0].name)[0], column=3)
    assert cell.value == "=HYPERLINK(1)"
    assert cell.data_type != "f"


def test_control_characters_do_not_break_the_file(dossier, tmp_path):
    dossier.invoices[0].partner_name = "Nom\x0b amb\x01 control"
    register = Register(build(dossier, tmp_path))
    assert (
        register.value(register.rows_of(dossier.invoices[0].name)[0], "Proveïdor")
        == "Nom amb control"
    )


def test_ampersand_in_entity_name_survives_the_print_footer(dossier, tmp_path):
    dossier.company = type(dossier.company)(1, "Art & Oficis", "", "EUR")
    assert "&&" in Register(build(dossier, tmp_path)).ws.oddFooter.left.text


def test_empty_dossier_still_produces_a_valid_file(backend, tmp_path):
    empty = extract_dossier(backend, ExtractParams(1990, include_other_expenses=False))
    register = Register(build(empty, tmp_path), data_only=True)
    assert register.value(register.last, "Proveïdor") == "0 factures i rebuts"


def test_failure_leaves_no_partial_file(dossier, tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("dossier_factures.excel_builder._store_results", fail)
    with pytest.raises(OSError):
        build(dossier, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_output_folder_is_named_and_never_reused(tmp_path):
    first = new_output_folder(tmp_path, 2026, date(2026, 11, 2))
    second = new_output_folder(tmp_path, 2026, date(2026, 11, 2))
    assert first.name == "2026 - generat 2026-11-02"
    assert second.name == "2026 - generat 2026-11-02 (2)"
    assert first.is_dir()
    assert second.is_dir()


def test_only_invoices_is_described_as_no_filter(backend, tmp_path):
    dossier = extract_dossier(
        backend, ExtractParams(YEAR, include_receipts=False, include_other_expenses=False)
    )
    register = Register(build(dossier, tmp_path))
    assert register.ws["A3"].value == "Sense filtres"
    assert register.value(register.last, "Proveïdor") == "12 factures"


def test_receipts_are_marked_and_described(dossier, tmp_path):
    register = Register(build(dossier, tmp_path))
    row = register.rows_of("FC/2099/06/0001")[0]
    assert register.value(row, "Observacions") == "Rebut de compra"
    assert register.value(row, "Proveïdor") in ("", None)
    assert register.ws["A3"].value == "Inclou rebuts de compra"
    assert register.value(register.last, "Proveïdor") == "13 factures i rebuts"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_file_is_readable_by_other_users_like_any_saved_document(dossier, tmp_path):
    assert build(dossier, tmp_path).stat().st_mode & 0o044 == 0o044


@pytest.fixture
def full(backend):
    return extract_dossier(backend, ExtractParams(YEAR))


def test_other_expenses_have_a_section_with_subtotals(full, tmp_path):
    register = Register(build(full, tmp_path), data_only=True)
    labels = [
        register.value(r, "Registre intern") for r in range(HEADER_ROW + 1, register.last + 1)
    ]
    assert "Subtotal factures i rebuts" in labels
    assert "Altres despeses, sense document al dossier" in labels
    assert "Subtotal altres despeses" in labels
    assert labels[-1] == "Total"

    def value(label, column):
        return Decimal(str(register.value(HEADER_ROW + 1 + labels.index(label), column)))

    documents = sum(i.total for i in full.documented)
    assert value("Subtotal factures i rebuts", "Total") == documents
    assert value("Subtotal altres despeses", "Total") == Decimal("1282.00")
    assert value("Total", "Total") == full.total == documents + Decimal("1282.00")
    assert register.value(register.last, "Proveïdor") == "13 factures i rebuts · 4 despeses"


def test_grand_total_formula_covers_everything(full, tmp_path):
    register = Register(build(full, tmp_path))
    assert register.value(register.last, "Total").startswith("=SUBTOTAL(109,")
    assert f"{HEADER_ROW + 1}:" in register.value(register.last, "Total")


def test_undocumented_expense_row(full, tmp_path):
    register = Register(build(full, tmp_path))
    row = register.rows_of("VAR/2099/02/0001")[0]
    assert register.value(row, "Proveïdor") == "Comissió de manteniment"
    assert register.value(row, "Núm. factura proveïdor") in ("", None)
    assert register.value(row, "Observacions") == "Sense document"
    assert register.value(row, "Pàgina al dossier") is None
    assert register.value(row, "Estat del pagament") in ("", None)
    assert register.value(row, "Import pagat") == 12
    assert register.value(row, "Forma de pagament") == "Banc Exemple"
    assert register.value(row, "Trimestre") == "T1"
    assert not register.ws.cell(row=row, column=1).fill.fgColor.rgb.endswith("FFF2CC")


def test_personnel_names_are_not_in_the_workbook_by_default(full, tmp_path):
    ws = Register(build(full, tmp_path)).ws
    text = " ".join(str(c.value) for row in ws.iter_rows() for c in row if isinstance(c.value, str))
    assert "PERSONA EXEMPLE" not in text
