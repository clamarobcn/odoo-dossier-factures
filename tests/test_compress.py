import base64
import hashlib
import io
from datetime import date

import pikepdf
import pypdfium2
import pytest
from openpyxl import load_workbook
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from dossier_factures.compress import CompressOptions, compress_pdf
from dossier_factures.documents import prepare_pdf
from dossier_factures.excel_builder import HEADER_ROW
from dossier_factures.model import ExtractParams, NoticeCode
from dossier_factures.pipeline import GenerateOptions, generate

YEAR = 2099
TODAY = date(2099, 12, 31)
OPTIONS = CompressOptions()
DOCUMENT_IDS = (1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)


def noisy_scan(size=(2479, 3507)) -> Image.Image:
    """A page that compresses as badly as a real scan, with a red mark near the top left."""
    base = Image.frombytes("L", (size[0] // 8, size[1] // 8), bytes(range(256)) * 20000)
    image = base.resize(size).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 700, 700), fill=(220, 0, 0))
    return image


def scan_pdf(path, page_size=A4, flipped=False, image=None, pages=1):
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pageCompression=1)
    for _ in range(pages):
        canvas.setPageSize(page_size)
        if flipped:
            canvas.saveState()
            canvas.transform(1, 0, 0, -1, 0, page_size[1])
            source = (image or noisy_scan()).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            canvas.drawImage(ImageReader(source), 0, 0, page_size[0], page_size[1])
            canvas.restoreState()
        else:
            canvas.drawImage(ImageReader(image or noisy_scan()), 0, 0, page_size[0], page_size[1])
        canvas.showPage()
    canvas.save()
    path.write_bytes(buffer.getvalue())
    return path


def render(path, index=0, scale=1.0):
    document = pypdfium2.PdfDocument(path)
    try:
        return document[index].render(scale=scale).to_pil().convert("RGB")
    finally:
        document.close()


def image_of(path, index=0):
    with pikepdf.open(path) as pdf:
        stream = next(iter(pdf.pages[index].get_images().values()))
        return int(stream.Width), int(stream.Height), str(stream.get("/Filter"))


def red_center(image):
    r, g, _ = image.split()[:3]
    mask = Image.composite(
        r.point(lambda v: 255 if v > 150 else 0),
        Image.new("L", image.size, 0),
        g.point(lambda v: 255 if v < 60 else 0),
    )
    box = mask.getbbox()
    assert box
    return (box[0] + box[2]) / 2 / image.width, (box[1] + box[3]) / 2 / image.height


def test_scan_is_downsampled_to_the_target_resolution_and_much_smaller(tmp_path):
    path = scan_pdf(tmp_path / "scan.pdf")
    result = compress_pdf(path, OPTIONS)
    assert result.images_changed == 1
    assert result.final_size < result.original_size * 0.5
    width, _height, image_filter = image_of(path)
    # A4 reduced to 92 %, at 150 dpi.
    assert width == pytest.approx(595.28 * 0.92 / 72 * 150, abs=3)
    assert image_filter == "/DCTDecode"


def test_page_declared_oversized_is_treated_as_its_real_size(tmp_path):
    """A 300 ppi A4 scan may declare 2479 x 3507 points, as if it were 72 ppi."""
    path = scan_pdf(tmp_path / "scan.pdf", page_size=(2479, 3507))
    compress_pdf(path, OPTIONS)
    width, _height, _filter = image_of(path)
    assert width == pytest.approx(595.28 * 0.92 / 72 * 150, abs=3)


def test_quality_and_resolution_are_configurable(tmp_path):
    low = scan_pdf(tmp_path / "low.pdf")
    high = scan_pdf(tmp_path / "high.pdf")
    compress_pdf(low, CompressOptions(dpi=100, quality=40))
    compress_pdf(high, CompressOptions(dpi=200, quality=85))
    assert low.stat().st_size < high.stat().st_size
    assert image_of(low)[0] < image_of(high)[0]


def test_appearance_is_kept_including_flipped_images(tmp_path):
    for flipped in (False, True):
        path = scan_pdf(tmp_path / f"scan{flipped}.pdf", flipped=flipped)
        before = red_center(render(path))
        compress_pdf(path, OPTIONS)
        after = red_center(render(path))
        assert after == pytest.approx(before, abs=0.01)


def test_flipped_page_keeps_its_transformation(tmp_path):
    path = scan_pdf(tmp_path / "scan.pdf", flipped=True)
    with pikepdf.open(path) as pdf:
        before = bytes(pdf.pages[0].Contents.read_bytes())
    compress_pdf(path, OPTIONS)
    with pikepdf.open(path) as pdf:
        assert bytes(pdf.pages[0].Contents.read_bytes()) == before


def test_light_text_pdf_is_not_touched(tmp_path):
    path = tmp_path / "text.pdf"
    canvas = Canvas(str(path))
    canvas.drawString(100, 500, "Factura digital")
    canvas.save()
    before = path.read_bytes()
    result = compress_pdf(path, OPTIONS)
    assert path.read_bytes() == before
    assert result.images_changed == 0


def test_small_logo_in_a_heavy_pdf_is_kept(tmp_path):
    logo = Image.new("RGB", (60, 60), (10, 20, 200))
    scan = noisy_scan()
    buffer = io.BytesIO()
    canvas = Canvas(buffer)
    canvas.drawImage(ImageReader(logo), 10, 10, 60, 60)
    canvas.drawImage(ImageReader(scan), 80, 80, 400, 560)
    canvas.save()
    path = tmp_path / "mixed.pdf"
    path.write_bytes(buffer.getvalue())
    result = compress_pdf(path, OPTIONS)
    assert result.images_changed == 1
    with pikepdf.open(path) as pdf:
        sizes = sorted(int(s.Width) for s in pdf.pages[0].get_images().values())
    assert sizes[0] == 60


def test_one_bit_scan_is_not_touched(tmp_path):
    path = tmp_path / "bw.pdf"
    noisy_scan().convert("1").save(path, "PDF", resolution=300)
    before = path.read_bytes()
    assert len(before) >= 150 * 1024
    with pikepdf.open(path) as pdf:
        stream = next(iter(pdf.pages[0].get_images().values()))
        assert int(stream.BitsPerComponent) == 1
    assert compress_pdf(path, OPTIONS).images_changed == 0
    assert path.read_bytes() == before


def test_all_pages_survive(tmp_path):
    path = scan_pdf(tmp_path / "scan.pdf", pages=3)
    compress_pdf(path, OPTIONS)
    with pikepdf.open(path) as pdf:
        assert len(pdf.pages) == 3


def valid_pdf() -> bytes:
    buffer = io.BytesIO()
    canvas = Canvas(buffer)
    canvas.drawString(100, 500, "ORIGINAL")
    canvas.save()
    return buffer.getvalue()


def test_good_pdf_needs_no_repair(tmp_path):
    path = tmp_path / "ok.pdf"
    path.write_bytes(valid_pdf())
    assert prepare_pdf(path) == "ok"


def test_pdf_without_cross_reference_table_is_repaired(tmp_path):
    content = valid_pdf()
    path = tmp_path / "cut.pdf"
    path.write_bytes(content[: content.rfind(b"xref")])
    assert prepare_pdf(path) == "repaired"
    with pikepdf.open(path) as pdf:
        assert len(pdf.pages) == 1
    assert prepare_pdf(path) == "ok"


def test_junk_before_the_header_is_handled(tmp_path):
    path = tmp_path / "junk.pdf"
    path.write_bytes(b"\r\n\r\nGARBAGE FROM A MAIL GATEWAY\r\n" + valid_pdf())
    assert prepare_pdf(path) in ("ok", "repaired")
    with pikepdf.open(path) as pdf:
        assert len(pdf.pages) == 1


def test_hopeless_files_are_reported(tmp_path):
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"not a pdf")
    assert prepare_pdf(path) == "unreadable"
    path.write_bytes(b"%PDF-1.7\nnothing useful here")
    assert prepare_pdf(path) == "unreadable"


def test_password_protected_file_is_reported(tmp_path):
    with pikepdf.open(io.BytesIO(valid_pdf())) as source:
        path = tmp_path / "secret.pdf"
        source.save(path, encryption=pikepdf.Encryption(user="x", owner="x"))
    assert prepare_pdf(path) == "unreadable"


def set_document(scenario, attachment_id, content):
    record = next(a for a in scenario["ir.attachment"] if a["id"] == attachment_id)
    record["raw"] = base64.b64encode(content).decode()
    record["checksum"] = hashlib.sha1(content).hexdigest()
    record["file_size"] = len(content)
    record["mimetype"] = "application/pdf"


@pytest.fixture
def documents_ready(scenario):
    for attachment_id in DOCUMENT_IDS:
        set_document(scenario, attachment_id, valid_pdf())
    return scenario


def test_end_to_end_compression_reports_sizes(backend, scenario, documents_ready, tmp_path):
    scan = scan_pdf(tmp_path / "scan.pdf").read_bytes()
    set_document(scenario, 1, scan)
    out = tmp_path / "out"
    result = generate(backend, ExtractParams(YEAR), GenerateOptions(out, generated_on=TODAY))
    assert result.final_bytes < result.original_bytes
    assert result.volumes[0].path.stat().st_size < len(scan)


def test_compression_can_be_turned_off(backend, scenario, documents_ready, tmp_path):
    scan = scan_pdf(tmp_path / "scan.pdf").read_bytes()
    set_document(scenario, 1, scan)
    out = tmp_path / "out"
    result = generate(
        backend, ExtractParams(YEAR), GenerateOptions(out, generated_on=TODAY, compress=None)
    )
    assert result.final_bytes == result.original_bytes
    assert result.volumes[0].path.stat().st_size > 100 * 1024


def test_repaired_document_is_kept_and_noted(backend, scenario, documents_ready, tmp_path):
    content = valid_pdf()
    set_document(scenario, 1, content[: content.rfind(b"xref")])
    result = generate(backend, ExtractParams(YEAR), GenerateOptions(tmp_path, generated_on=TODAY))
    assert NoticeCode.DOCUMENT_REPAIRED in {n.code for n in result.notices}
    assert NoticeCode.DOCUMENT_UNREADABLE not in {n.code for n in result.notices}
    item = next(i for i in result.dossier.invoices if i.name == "FC/2099/01/0001")
    assert result.volumes[0].source_pages[item.id] == 1


def quarters_result(backend, tmp_path):
    return generate(
        backend,
        ExtractParams(YEAR),
        GenerateOptions(tmp_path, generated_on=TODAY, split_quarters=True),
    )


def test_quarters_each_have_cover_index_and_own_numbering(backend, documents_ready, tmp_path):
    result = quarters_result(backend, tmp_path)
    names = [v.path.name for v in result.volumes]
    assert names == [f"Dossier de factures de proveïdors 2099 T{q}.pdf" for q in (1, 2, 3)]
    expected = {1: 2, 2: 8, 3: 3}
    for quarter, volume in zip((1, 2, 3), result.volumes, strict=True):
        document = pypdfium2.PdfDocument(volume.path)
        try:
            cover = document[0].get_textpage().get_text_range()
            assert f"Trimestre {quarter}" in cover
            assert "Índex" in document[1].get_textpage().get_text_range()
            assert f"Pàg. 1 de {len(document)}" in cover
        finally:
            document.close()
        assert len(volume.first_pages) == expected[quarter]
        assert min(volume.first_pages.values()) == 3


def test_quarter_volumes_keep_the_global_order_number(backend, documents_ready, tmp_path):
    result = quarters_result(backend, tmp_path)
    second = result.volumes[1]
    document = pypdfium2.PdfDocument(second.path)
    try:
        text = document[1].get_textpage().get_text_range()
    finally:
        document.close()
    assert "003" in text
    assert "001" not in text.split("\n")[3:]


def test_no_invoice_is_lost_or_repeated_between_quarters(backend, documents_ready, tmp_path):
    result = quarters_result(backend, tmp_path)
    seen = [k for v in result.volumes for k in v.first_pages]
    assert sorted(seen) == sorted(i.id for i in result.dossier.documented)


def test_single_excel_with_quarter_and_page_of_that_quarter(backend, documents_ready, tmp_path):
    result = quarters_result(backend, tmp_path)
    assert len(list(result.folder.glob("*.xlsx"))) == 1
    ws = load_workbook(result.excel_path).active
    names = [c.value for c in ws[HEADER_ROW]]
    pages = {k: v for volume in result.volumes for k, v in volume.first_pages.items()}
    item = next(i for i in result.dossier.invoices if i.name == "FC/2099/02/0002")
    row = next(
        r
        for r in range(HEADER_ROW + 1, ws.max_row)
        if ws.cell(row=r, column=names.index("Registre intern") + 1).value == item.name
    )
    assert ws.cell(row=row, column=names.index("Trimestre") + 1).value == "T2"
    assert ws.cell(row=row, column=names.index("Pàgina al dossier") + 1).value == pages[item.id]


def test_oversize_single_pdf_is_flagged_only_when_not_split(backend, documents_ready, tmp_path):
    options = GenerateOptions(tmp_path / "a", generated_on=TODAY, split_threshold_mb=0)
    assert generate(backend, ExtractParams(YEAR), options).oversize
    options = GenerateOptions(tmp_path / "b", generated_on=TODAY, split_threshold_mb=100)
    assert not generate(backend, ExtractParams(YEAR), options).oversize
    options = GenerateOptions(
        tmp_path / "c", generated_on=TODAY, split_threshold_mb=0, split_quarters=True
    )
    assert not generate(backend, ExtractParams(YEAR), options).oversize
