"""Check the workbook with a real spreadsheet engine.

Runs where LibreOffice is installed. CI sets REQUIRE_LIBREOFFICE so that it cannot be
skipped there without anyone noticing.
"""

import csv
import os
import shutil
import subprocess
from decimal import Decimal

import pikepdf
import pytest
from openpyxl import load_workbook

from dossier_factures.excel_builder import HEADER_ROW, build_workbook
from dossier_factures.extract import extract_dossier
from dossier_factures.model import ExtractParams

SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")

if not SOFFICE and os.environ.get("REQUIRE_LIBREOFFICE"):
    raise RuntimeError("LibreOffice is required here but was not found")

pytestmark = pytest.mark.skipif(not SOFFICE, reason="LibreOffice is not installed")


def convert(source, target_format, outdir):
    subprocess.run(
        [
            SOFFICE,
            f"-env:UserInstallation=file://{outdir}/profile",
            "--headless",
            "--convert-to",
            target_format,
            "--outdir",
            str(outdir),
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )


@pytest.fixture
def dossier(backend):
    return extract_dossier(backend, ExtractParams(2099))


def test_formulas_calculate_the_same_totals_without_stored_results(dossier, tmp_path):
    path = tmp_path / "registre.xlsx"
    build_workbook(dossier, path)
    # Saving through openpyxl drops the stored results, so the engine has to calculate.
    stripped = tmp_path / "stripped.xlsx"
    load_workbook(path).save(stripped)
    convert(stripped, "csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,false", tmp_path)

    with open(tmp_path / "stripped.csv", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    header = rows[HEADER_ROW - 1]
    totals = rows[-1]

    def total(column):
        text = totals[header.index(column)].replace("€", "").replace("\xa0", "").strip()
        return Decimal(text.replace(",", ""))

    assert total("Total") == dossier.total
    assert total("Base imposable") == dossier.total_untaxed
    assert total("IVA") == dossier.total_tax
    assert total("Import pagat") == dossier.total_paid


def test_prints_on_one_a4_landscape_page_wide(dossier, tmp_path):
    path = tmp_path / "registre.xlsx"
    build_workbook(dossier, path)
    convert(path, "pdf", tmp_path)
    with pikepdf.open(tmp_path / "registre.pdf") as pdf:
        # The fixture is short: columns spilling onto extra pages would show up here.
        assert len(pdf.pages) == 1
        box = [float(v) for v in pdf.pages[0].mediabox]
    assert box[2] - box[0] == pytest.approx(842, abs=2)
    assert box[3] - box[1] == pytest.approx(595, abs=2)
