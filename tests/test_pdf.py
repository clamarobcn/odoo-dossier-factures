import base64
import hashlib
import io
import threading
from datetime import date
from decimal import Decimal

import pikepdf
import pypdfium2
import pytest
from openpyxl import load_workbook
from PIL import Image
from reportlab.lib.colors import black, red
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen.canvas import Canvas

from dossier_factures.excel_builder import HEADER_ROW
from dossier_factures.extract import extract_dossier
from dossier_factures.model import ExtractParams, NoticeCode
from dossier_factures.odoo_client import NetworkError
from dossier_factures.pdf_builder import Cancelled, Document, build_volume, pdf_filename
from dossier_factures.pdf_builder.front import layout_index
from dossier_factures.pdf_builder.layout import content_rect
from dossier_factures.pdf_builder.text import band_lines, fit, payment_summary, register_fonts
from dossier_factures.pipeline import GenerateOptions, generate

YEAR = 2099
TODAY = date(2099, 12, 31)
DOCUMENT_IDS = (1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)


def pdf_bytes(pages=((595, 842),), text="ORIGINAL", draw=None) -> bytes:
    buffer = io.BytesIO()
    canvas = Canvas(buffer)
    for number, size in enumerate(pages, start=1):
        canvas.setPageSize(size)
        if draw:
            draw(canvas, size)
        else:
            canvas.drawString(100, 500, f"{text} {number}")
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def set_document(scenario, attachment_id, content, mimetype="application/pdf", name=None):
    record = next(a for a in scenario["ir.attachment"] if a["id"] == attachment_id)
    record["raw"] = base64.b64encode(content).decode()
    record["checksum"] = hashlib.sha1(content).hexdigest()
    record["file_size"] = len(content)
    record["mimetype"] = mimetype
    if name:
        record["name"] = name


def jpeg_bytes(size=(600, 800), color=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.fixture
def documents_ready(scenario):
    for attachment_id in DOCUMENT_IDS:
        set_document(scenario, attachment_id, pdf_bytes(text=f"DOC{attachment_id}"))
    set_document(scenario, 1, pdf_bytes(pages=[(595, 842)] * 3, text="DOC1"))
    set_document(scenario, 2, jpeg_bytes(), "image/jpeg")
    return scenario


@pytest.fixture
def result(backend, documents_ready, tmp_path):
    return generate(backend, ExtractParams(YEAR), GenerateOptions(tmp_path, generated_on=TODAY))


def page_text(path, index) -> str:
    document = pypdfium2.PdfDocument(path)
    try:
        return document[index].get_textpage().get_text_range()
    finally:
        document.close()


def all_text(path) -> list[str]:
    with pikepdf.open(path) as pdf:
        count = len(pdf.pages)
    return [page_text(path, i) for i in range(count)]


def render(path, index, scale=1.0) -> Image.Image:
    document = pypdfium2.PdfDocument(path)
    try:
        return document[index].render(scale=scale).to_pil().convert("RGB")
    finally:
        document.close()


def first_pages(result):
    return result.volumes[0].first_pages


def pdf_path(result):
    return result.volumes[0].path


def invoice(result, name):
    return next(i for i in result.dossier.invoices if i.name == name)


def test_filename_keeps_the_agreed_name():
    assert pdf_filename(2026) == "Dossier de factures de proveïdors 2026.pdf"


def test_no_page_is_lost(result):
    # 13 documents: one has 3 pages, one is missing and takes a warning page.
    with pikepdf.open(pdf_path(result)) as pdf:
        assert len(pdf.pages) == 1 + 1 + 3 + 11 + 1
    assert result.page_count == 17
    source_pages = result.volumes[0].source_pages
    assert sum(source_pages.values()) == 14


def test_output_goes_in_a_dated_folder(result, tmp_path):
    assert result.folder.parent == tmp_path
    assert result.folder.name == "2099 - generat 2099-12-31"
    assert pdf_path(result).parent == result.folder
    assert result.excel_path.parent == result.folder


def test_every_page_is_a4(result):
    with pikepdf.open(pdf_path(result)) as pdf:
        for page in pdf.pages:
            box = [float(v) for v in page.mediabox]
            size = tuple(sorted((box[2] - box[0], box[3] - box[1])))
            assert size == pytest.approx((595.28, 841.89), abs=0.01)


def test_cover_shows_the_required_data(result):
    text = page_text(pdf_path(result), 0)
    for expected in (
        "Entitat d'Exemple",
        "NIF G00000000",
        "Dossier de factures de proveïdors",
        "Exercici 2099",
        "Nombre de factures i rebuts",
        "13",
        "1.662,50 €",
        "Inclou rebuts de compra",
        "31/12/2099",
    ):
        assert expected in text, expected


def test_band_is_on_every_page_of_an_invoice(result):
    item = invoice(result, "FC/2099/01/0001")
    first = first_pages(result)[item.id] - 1
    for offset in range(3):
        text = page_text(pdf_path(result), first + offset)
        assert "Núm. 001" in text
        assert "FC/2099/01/0001" in text
        assert "Proveïdor Àlfa" in text
        assert "Fra. R-101" in text
        assert "15/01/2099" in text
        assert "121,00 €" in text
        assert "Pagada 20/01/2099 – Banc" in text
        assert f"pàg. {offset + 1}/3" in text
        assert f"DOC1 {offset + 1}" in text


def test_footer_has_the_global_page_number(result):
    for index, text in enumerate(all_text(pdf_path(result))):
        assert f"Pàg. {index + 1} de 17" in text


def test_band_describes_refunds_receipts_and_several_payments(result):
    assert band_lines(invoice(result, "RFC/2099/03/0001"))[1].startswith("Rectificativa R-106")
    assert "-40,00 €" in band_lines(invoice(result, "RFC/2099/03/0001"))[1]
    receipt = band_lines(invoice(result, "FC/2099/06/0001"))
    assert receipt[1].startswith("Rebut TIQUET-1")
    assert "(sense proveïdor)" in receipt[0]
    assert (
        payment_summary(invoice(result, "FC/2099/01/0002"))
        == "Pagada 20/02/2099 – Efectiu; 05/03/2099 – Banc"
    )
    assert payment_summary(invoice(result, "FC/2099/04/0002")).startswith("En procés de pagament")
    assert payment_summary(invoice(result, "FC/2099/04/0001")) == "No pagada"


def test_index_links_go_to_the_first_page_of_each_invoice(result):
    with pikepdf.open(pdf_path(result)) as pdf:
        annotations = pdf.pages[1].Annots
        assert len(annotations) == 13
        order = {pdf.pages[i].obj.objgen: i for i in range(len(pdf.pages))}
        targets = [order[a.Dest[0].objgen] for a in annotations]
    expected = [first_pages(result)[i.id] - 1 for i in result.dossier.documented]
    assert targets == expected
    for number, target in enumerate(targets, start=1):
        assert f"Núm. {number:03d}" in page_text(pdf_path(result), target)


def test_index_shows_the_register_and_the_right_pages(result):
    text = page_text(pdf_path(result), 1)
    assert "FC/2099/01/0001" in text
    assert "1.662,50" in text
    item = invoice(result, "FC/2099/01/0002")
    assert str(first_pages(result)[item.id]) in text


def test_outline_has_one_entry_per_invoice_at_its_page(result):
    with pikepdf.open(pdf_path(result)) as pdf, pdf.open_outline() as outline:
        items = list(outline.root)
        titles = [item.title for item in items]
        assert titles[0] == "Índex"
        assert len(items) == 14
        assert titles[1] == "001 – FC/2099/01/0001 – Proveïdor Àlfa"
        order = {pdf.pages[i].obj.objgen: i for i in range(len(pdf.pages))}
        pages = [order[item.destination[0].objgen] for item in items[1:]]
    assert pages == [first_pages(result)[i.id] - 1 for i in result.dossier.documented]


def test_excel_pages_match_the_pdf(result):
    ws = load_workbook(result.excel_path).active
    names = [c.value for c in ws[HEADER_ROW]]
    checked = 0
    for row in range(HEADER_ROW + 1, ws.max_row):
        internal = ws.cell(row=row, column=names.index("Registre intern") + 1).value
        page = ws.cell(row=row, column=names.index("Pàgina al dossier") + 1).value
        order = ws.cell(row=row, column=names.index("Núm. d'ordre") + 1).value
        if order is None:
            continue
        item = invoice(result, internal)
        assert page == first_pages(result).get(item.id)
        checked += 1
    assert checked > 13


def test_missing_document_gets_a_warning_page_and_is_marked(result):
    item = invoice(result, "FC/2099/03/0001")
    text = page_text(pdf_path(result), first_pages(result)[item.id] - 1)
    assert "Factura sense document adjunt" in text
    assert "Núm. 005" in text
    assert {n.code for n in result.notices if n.invoice_name == item.name} >= {
        NoticeCode.MISSING_ATTACHMENT
    }


def test_image_becomes_a_page(result):
    item = invoice(result, "FC/2099/01/0002")
    page = render(pdf_path(result), first_pages(result)[item.id] - 1)
    r, g, b = page.getpixel((page.width // 2, page.height // 2))
    assert r > 150
    assert g < 80
    assert b < 80


def test_png_with_transparency_becomes_a_page(backend, scenario, documents_ready, tmp_path):
    buffer = io.BytesIO()
    Image.new("RGBA", (300, 300), (0, 0, 255, 128)).save(buffer, "PNG")
    set_document(scenario, 2, buffer.getvalue(), "image/png", "foto.png")
    result = generate(backend, ExtractParams(YEAR), GenerateOptions(tmp_path, generated_on=TODAY))
    assert result.volumes[0].source_pages[invoice(result, "FC/2099/01/0002").id] == 1


def _generate_with(backend, tmp_path):
    return generate(backend, ExtractParams(YEAR), GenerateOptions(tmp_path, generated_on=TODAY))


def _dark_box(image, threshold=15):
    """Bounding box of pure black, which leaves out the dark ink of the band and the footer."""
    grey = image.convert("L").point(lambda v: 255 if v < threshold else 0)
    return grey.getbbox()


def test_original_is_reduced_and_never_covered_by_band_or_footer(
    backend, scenario, documents_ready, tmp_path
):
    def full_black(canvas, size):
        canvas.setFillColor(black)
        canvas.rect(0, 0, size[0], size[1], fill=1, stroke=0)

    set_document(scenario, 1, pdf_bytes(draw=full_black, pages=[A4]))
    result = _generate_with(backend, tmp_path)
    index = first_pages(result)[invoice(result, "FC/2099/01/0001").id] - 1
    image = render(pdf_path(result), index)
    left, top, right, bottom = _dark_box(image)
    rect = content_rect(A4)
    assert left == pytest.approx(rect[0], abs=2)
    assert right == pytest.approx(rect[2], abs=2)
    assert top >= A4[1] - rect[3] - 2
    assert bottom <= A4[1] - rect[1] + 2
    assert (right - left) / A4[0] == pytest.approx(0.92, abs=0.01)
    # The band sits above the original: its background shows there, not black.
    assert image.getpixel((image.width // 2, 20))[0] > 200


def test_oversized_scan_is_normalized_to_a4(backend, scenario, documents_ready, tmp_path):
    def full_black(canvas, size):
        canvas.setFillColor(black)
        canvas.rect(0, 0, size[0], size[1], fill=1, stroke=0)

    set_document(scenario, 1, pdf_bytes(draw=full_black, pages=[(2479, 3507)]))
    result = _generate_with(backend, tmp_path)
    index = first_pages(result)[invoice(result, "FC/2099/01/0001").id] - 1
    image = render(pdf_path(result), index)
    assert image.size == pytest.approx((595, 842), abs=2)
    left, _top, right, _bottom = _dark_box(image)
    assert (right - left) / A4[0] == pytest.approx(0.92, abs=0.015)


def test_small_page_is_not_enlarged(backend, scenario, documents_ready, tmp_path):
    def full_black(canvas, size):
        canvas.setFillColor(black)
        canvas.rect(0, 0, size[0], size[1], fill=1, stroke=0)

    set_document(scenario, 1, pdf_bytes(draw=full_black, pages=[(200, 300)]))
    result = _generate_with(backend, tmp_path)
    index = first_pages(result)[invoice(result, "FC/2099/01/0001").id] - 1
    left, top, right, bottom = _dark_box(render(pdf_path(result), index))
    assert right - left == pytest.approx(200, abs=3)
    assert bottom - top == pytest.approx(300, abs=3)


def test_landscape_page_stays_landscape(backend, scenario, documents_ready, tmp_path):
    set_document(scenario, 1, pdf_bytes(pages=[landscape(A4)], text="APAISAT"))
    result = _generate_with(backend, tmp_path)
    index = first_pages(result)[invoice(result, "FC/2099/01/0001").id] - 1
    with pikepdf.open(pdf_path(result)) as pdf:
        box = [float(v) for v in pdf.pages[index].mediabox]
    assert box[2] > box[3]
    assert "APAISAT" in page_text(pdf_path(result), index)


def _mark_position(image):
    red_only = image.split()[0].point(lambda v: 255 if v > 150 else 0)
    green = image.split()[1].point(lambda v: 255 if v < 80 else 0)
    combined = Image.composite(red_only, Image.new("L", image.size, 0), green)
    box = combined.getbbox()
    assert box, "mark not found"
    return ((box[0] + box[2]) / 2 / image.width, (box[1] + box[3]) / 2 / image.height)


def _in_content(position, size):
    left, bottom, right, top = content_rect(size)
    return (
        (position[0] * size[0] - left) / (right - left),
        (position[1] * size[1] - (size[1] - top)) / (top - bottom),
    )


def _red_mark(canvas, size):
    canvas.setFillColor(red)
    canvas.rect(40, size[1] - 100, 60, 60, fill=1, stroke=0)


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_page_keeps_its_appearance(backend, scenario, documents_ready, tmp_path, rotation):
    with pikepdf.open(io.BytesIO(pdf_bytes(draw=_red_mark))) as source:
        source.pages[0].Rotate = rotation
        buffer = io.BytesIO()
        source.save(buffer)
    content = buffer.getvalue()
    set_document(scenario, 1, content)
    original_path = tmp_path / "original.pdf"
    original_path.write_bytes(content)
    expected = _mark_position(render(original_path, 0))

    result = _generate_with(backend, tmp_path)
    index = first_pages(result)[invoice(result, "FC/2099/01/0001").id] - 1
    size = landscape(A4) if rotation in (90, 270) else A4
    with pikepdf.open(pdf_path(result)) as pdf:
        box = [float(v) for v in pdf.pages[index].mediabox]
    assert (box[2] > box[3]) == (size[0] > size[1])
    found = _in_content(_mark_position(render(pdf_path(result), index)), size)
    assert found[0] == pytest.approx(expected[0], abs=0.03)
    assert found[1] == pytest.approx(expected[1], abs=0.03)


def test_image_stored_upside_down_stays_right(backend, scenario, documents_ready, tmp_path):
    """Scanners may store the image flipped and correct it with the page transformation."""

    def flipped(canvas, size):
        canvas.saveState()
        canvas.transform(1, 0, 0, -1, 0, size[1])
        canvas.setFillColor(red)
        canvas.rect(40, 40, 60, 60, fill=1, stroke=0)
        canvas.restoreState()

    content = pdf_bytes(draw=flipped)
    set_document(scenario, 1, content)
    original_path = tmp_path / "original.pdf"
    original_path.write_bytes(content)
    expected = _mark_position(render(original_path, 0))
    assert expected[1] < 0.2

    result = _generate_with(backend, tmp_path)
    index = first_pages(result)[invoice(result, "FC/2099/01/0001").id] - 1
    found = _in_content(_mark_position(render(pdf_path(result), index)), A4)
    assert found[1] == pytest.approx(expected[1], abs=0.03)


def test_damaged_file_gets_a_warning_page_and_marks(backend, scenario, documents_ready, tmp_path):
    set_document(scenario, 1, b"this is not a pdf at all", name="malmes.pdf")
    result = _generate_with(backend, tmp_path)
    item = invoice(result, "FC/2099/01/0001")
    text = page_text(pdf_path(result), first_pages(result)[item.id] - 1)
    assert "No s'ha pogut llegir el document adjunt" in text
    assert "malmes.pdf" in text
    assert NoticeCode.DOCUMENT_UNREADABLE in {n.code for n in result.notices}
    ws = load_workbook(result.excel_path).active
    names = [c.value for c in ws[HEADER_ROW]]
    row = next(
        r
        for r in range(HEADER_ROW + 1, ws.max_row)
        if ws.cell(row=r, column=names.index("Registre intern") + 1).value == item.name
    )
    assert ws.cell(row=row, column=names.index("Observacions") + 1).value == "Document il·legible"


def test_password_protected_file_gets_a_warning_page(backend, scenario, documents_ready, tmp_path):
    with pikepdf.open(io.BytesIO(pdf_bytes())) as source:
        buffer = io.BytesIO()
        source.save(buffer, encryption=pikepdf.Encryption(user="secret", owner="secret"))
    set_document(scenario, 1, buffer.getvalue())
    result = _generate_with(backend, tmp_path)
    assert NoticeCode.DOCUMENT_UNREADABLE in {n.code for n in result.notices}


def test_owner_restricted_file_is_included(backend, scenario, documents_ready, tmp_path):
    with pikepdf.open(io.BytesIO(pdf_bytes(text="RESTRINGIT"))) as source:
        buffer = io.BytesIO()
        source.save(buffer, encryption=pikepdf.Encryption(user="", owner="secret"))
    set_document(scenario, 1, buffer.getvalue())
    result = _generate_with(backend, tmp_path)
    index = first_pages(result)[invoice(result, "FC/2099/01/0001").id] - 1
    assert "RESTRINGIT" in page_text(pdf_path(result), index)


def test_document_with_annotations_keeps_them(backend, scenario, documents_ready, tmp_path):
    with pikepdf.open(io.BytesIO(pdf_bytes(text="AMBNOTA"))) as source:
        appearance = source.make_stream(b"1 0 0 rg 0 0 100 100 re f")
        appearance.Type = pikepdf.Name.XObject
        appearance.Subtype = pikepdf.Name.Form
        appearance.BBox = pikepdf.Array([0, 0, 100, 100])
        stamp = source.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name.Stamp,
                Rect=pikepdf.Array([200, 200, 300, 300]),
                F=4,
                AP=pikepdf.Dictionary(N=appearance),
            )
        )
        source.pages[0].Annots = pikepdf.Array([stamp])
        buffer = io.BytesIO()
        source.save(buffer)
    set_document(scenario, 1, buffer.getvalue())
    result = _generate_with(backend, tmp_path)
    index = first_pages(result)[invoice(result, "FC/2099/01/0001").id] - 1
    image = render(pdf_path(result), index)
    assert _mark_position(image)


def test_project_filter_adds_columns_to_the_index(backend, documents_ready, tmp_path):
    result = generate(
        backend,
        ExtractParams(YEAR, analytic_account_id=7, include_other_expenses=False),
        GenerateOptions(tmp_path, generated_on=TODAY),
    )
    text = page_text(pdf_path(result), 1)
    assert "Imputat" in text
    assert "66,67" in text
    assert "200,00" in text
    assert "Projecte: Projecte Set" in page_text(pdf_path(result), 0)


def test_cancelling_before_downloading_leaves_nothing(backend, documents_ready, tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        generate(backend, ExtractParams(YEAR), GenerateOptions(tmp_path), cancel=cancel)
    assert list(tmp_path.iterdir()) == []


def test_cancelling_while_assembling_leaves_nothing(backend, documents_ready, tmp_path):
    cancel = threading.Event()

    def progress(message, current=None, total=None):
        if message.startswith("Muntant"):
            cancel.set()

    with pytest.raises(Cancelled):
        generate(backend, ExtractParams(YEAR), GenerateOptions(tmp_path), progress, cancel)
    assert list(tmp_path.iterdir()) == []


def test_network_failure_while_downloading_leaves_nothing(backend, documents_ready, tmp_path):
    original = backend._execute

    def failing(model, method, kwargs):
        if model == "ir.attachment" and "raw" in kwargs.get("fields", []):
            raise NetworkError("down")
        return original(model, method, kwargs)

    backend._execute = failing
    with pytest.raises(NetworkError):
        generate(backend, ExtractParams(YEAR), GenerateOptions(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_cache_avoids_downloading_again(backend, documents_ready, tmp_path):
    cache = tmp_path / "cache"
    options = GenerateOptions(tmp_path / "a", cache_dir=cache)
    generate(backend, ExtractParams(YEAR), options)
    backend.calls.clear()
    generate(backend, ExtractParams(YEAR), GenerateOptions(tmp_path / "b", cache_dir=cache))
    assert not [c for c in backend.calls if "raw" in c[2].get("fields", [])]


def test_documents_are_downloaded_one_by_one_and_only_with_read_methods(result, backend):
    downloads = [c for c in backend.calls if "raw" in c[2].get("fields", [])]
    assert len(downloads) == 12 + 1 - 1
    assert all(len(c[2]["ids"]) == 1 for c in downloads)
    assert {c[1] for c in backend.calls} <= {"search_read", "read", "fields_get", "context_get"}


def test_progress_reports_each_document(backend, documents_ready, tmp_path):
    seen = []

    def progress(message, current=None, total=None):
        seen.append((message, current, total))

    generate(backend, ExtractParams(YEAR), GenerateOptions(tmp_path), progress)
    downloads = [s for s in seen if s[0].startswith("Descarregant")]
    assert downloads[-1][1:] == (13, 13)
    assert seen[-1][0] == "Fet."


def test_long_text_shrinks_and_is_cut_only_as_a_last_resort():
    register_fonts()
    text, size = fit("Nom curt", "DossierSans", 8.5, 6.0, 500)
    assert (text, size) == ("Nom curt", 8.5)
    text, size = fit("x" * 70, "DossierSans", 8.5, 6.0, 300)
    assert size < 8.5
    assert not text.endswith("…")
    text, size = fit("x" * 500, "DossierSans", 8.5, 6.0, 300)
    assert size == 6.0
    assert text.endswith("…")


def test_long_index_spreads_over_pages_with_totals_at_the_end(backend, documents_ready):
    dossier = extract_dossier(backend, ExtractParams(YEAR))
    many = [dossier.documented[0]] * 300
    layout = layout_index(many, {}, set(), False)
    assert len(layout.pages) > 3
    assert layout.pages[-1][-1].cells["internal"] == ["Total"]
    assert layout.pages[-1][-1].cells["total"] == ["36.300,00"]
    assert sum(len(p) for p in layout.pages) == 301


def test_long_index_keeps_the_page_numbers_consistent(backend, documents_ready, tmp_path):
    dossier = extract_dossier(backend, ExtractParams(YEAR))
    base = dossier.invoices[0]
    copies = []
    for number in range(1, 121):
        copy = type(base)(**{**base.__dict__, "id": number, "order_number": number})
        copy.attachment = None
        copy.total = Decimal("1.00")
        copies.append(copy)
    documents = {c.id: Document(problem="Falta") for c in copies}
    result = build_volume(dossier, copies, documents, tmp_path / "llarg.pdf", generated_on=TODAY)
    with pikepdf.open(result.path) as pdf:
        assert len(pdf.pages) == result.page_count
        index_pages = result.page_count - 121
        assert index_pages >= 2
        linked = sum(len(pdf.pages[1 + i].Annots) for i in range(index_pages))
    assert linked == 120
    assert f"Núm. {120:03d}" in page_text(result.path, result.page_count - 1)
    assert result.first_pages[1] == 2 + index_pages


def test_undocumented_expenses_stay_out_of_the_pdf_but_in_the_excel(result):
    text = "\n".join(all_text(pdf_path(result)))
    assert "NOM/2099/01/0001" not in text
    assert "Despesa de personal" not in text
    assert {i.name for i in result.dossier.undocumented_expenses} >= {"NOM/2099/01/0001"}
    registered = {
        c.value
        for row in load_workbook(result.excel_path).active.iter_rows(min_row=HEADER_ROW + 1)
        for c in row
        if isinstance(c.value, str)
    }
    assert "NOM/2099/01/0001" in registered


def test_cover_counts_the_pdf_and_points_to_the_excel(result):
    text = page_text(pdf_path(result), 0)
    assert "1.662,50 €" in text
    assert "Altres despeses sense document" in text
    assert "4 (1.282,00 €), només al registre Excel" in text


def test_entry_with_a_document_gets_its_page(backend, scenario, documents_ready, tmp_path):
    move = next(m for m in scenario["account.move"] if m["id"] == 203)
    move["message_main_attachment_id"] = [1, "factura.pdf"]
    result = _generate_with(backend, tmp_path)
    item = invoice(result, "VAR/2099/02/0001")
    text = page_text(pdf_path(result), first_pages(result)[item.id] - 1)
    assert "VAR/2099/02/0001" in text
    assert "Comissió de manteniment" in text
