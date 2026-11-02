"""Assemble a volume: cover, index and every invoice page with its band and footer."""

import io
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, OutlineItem, Rectangle
from reportlab.pdfgen.canvas import Canvas

from ..formatting import format_date
from ..i18n import t
from ..model import Dossier, Invoice
from .front import draw_cover, draw_index, layout_index
from .layout import PORTRAIT, content_rect, output_size
from .overlay import draw_band, draw_footer, draw_warning
from .text import outline_title, register_fonts

# Sources are read into memory below this size, so a long dossier does not hold hundreds of
# files open at once: Windows limits how many a process may have.
IN_MEMORY_LIMIT = 20 * 1024 * 1024


class Cancelled(Exception):
    """The user cancelled the generation."""


@dataclass
class Document:
    """The document of an invoice: a readable PDF, or the reason why there is none."""

    path: Path | None = None
    problem: str = ""
    detail: str = ""


@dataclass
class VolumeResult:
    path: Path
    page_count: int
    first_pages: dict[int, int]
    source_pages: dict[int, int]


@dataclass
class _PlannedPage:
    invoice: Invoice
    number: int
    of: int
    size: tuple[float, float]


def _form_size(form) -> tuple[float, float]:
    """Size of a page as seen on screen, once its rotation and scale are applied."""
    box = [float(v) for v in form.BBox]
    a, b, c, d = (float(form.Matrix[i]) for i in range(4)) if "/Matrix" in form else (1, 0, 0, 1)
    width, height = box[2] - box[0], box[3] - box[1]
    return abs(a * width + c * height), abs(b * width + d * height)


def _page_sizes(path: Path) -> list[tuple[float, float]]:
    with pikepdf.open(path) as pdf:
        return [_form_size(page.as_form_xobject()) for page in pdf.pages]


def _open_source(path: Path) -> pikepdf.Pdf:
    if path.stat().st_size < IN_MEMORY_LIMIT:
        pdf = pikepdf.open(io.BytesIO(path.read_bytes()))
    else:
        pdf = pikepdf.open(path)
    try:
        # Stamps, comments and form values only exist as annotations, which a form XObject
        # would lose, so they become part of the page first.
        pdf.generate_appearance_streams()
        pdf.flatten_annotations("all")
    except pikepdf.PdfError:
        pass
    return pdf


def _place(page: pikepdf.Page, form, rect: Rectangle) -> bytes:
    name = page.add_resource(form, Name.XObject, prefix="Fx")
    return page.calc_form_xobject_placement(form, name, rect, allow_shrink=True, allow_expand=False)


def _write_front(
    path: Path,
    dossier: Dossier,
    invoices: list[Invoice],
    layout,
    volume_label: str,
    generated_on: date,
    footer: str,
    total_pages: int,
):
    canvas = Canvas(str(path), pageCompression=1)
    draw_cover(canvas, dossier, invoices, volume_label, generated_on, footer, total_pages)
    title = f"{dossier.company.name} · {t('pdf.fiscal_year', year=dossier.params.year)}"
    if volume_label:
        title = f"{title} · {volume_label}"
    links = draw_index(canvas, layout, title, footer, 2, total_pages)
    canvas.save()
    return links


def _write_overlays(
    path: Path,
    plan: list[_PlannedPage],
    documents: dict[int, Document],
    footer: str,
    first_number: int,
    total_pages: int,
) -> None:
    canvas = Canvas(str(path), pageCompression=1)
    for offset, planned in enumerate(plan):
        canvas.setPageSize(planned.size)
        draw_band(canvas, planned.size, planned.invoice, planned.number, planned.of)
        draw_footer(canvas, planned.size, footer, first_number + offset, total_pages)
        document = documents[planned.invoice.id]
        if document.path is None:
            draw_warning(canvas, planned.size, document.problem, document.detail)
        canvas.showPage()
    canvas.save()


def build_volume(
    dossier: Dossier,
    invoices: list[Invoice],
    documents: dict[int, Document],
    path: Path,
    volume_label: str = "",
    generated_on: date | None = None,
    cancel: threading.Event | None = None,
    on_invoice=lambda _done, _total: None,
) -> VolumeResult:
    """Write one PDF. Page numbers are global within this file."""
    register_fonts()
    generated_on = generated_on or date.today()
    path = Path(path)
    has_project = dossier.params.analytic_account_id is not None
    flagged = {i.id for i in invoices if documents[i.id].path is None}

    plan: list[_PlannedPage] = []
    source_pages: dict[int, int] = {}
    per_invoice: list[tuple[Invoice, int]] = []
    for invoice in invoices:
        document = documents[invoice.id]
        sizes = _page_sizes(document.path) if document.path else [PORTRAIT]
        source_pages[invoice.id] = len(sizes) if document.path else 0
        per_invoice.append((invoice, len(sizes)))
        plan.extend(
            _PlannedPage(invoice, n, len(sizes), output_size(*size))
            for n, size in enumerate(sizes, start=1)
        )

    # First pass: the length of the index decides where the invoices start.
    index_pages = len(layout_index(invoices, {}, flagged, has_project).pages)
    first_invoice_page = 1 + index_pages + 1
    first_pages: dict[int, int] = {}
    position = first_invoice_page
    for invoice, count in per_invoice:
        first_pages[invoice.id] = position
        position += count
    total_pages = position - 1

    footer = f"{dossier.company.name} · {t('pdf.fiscal_year', year=dossier.params.year)}"
    if volume_label:
        footer = f"{footer} · {volume_label}"

    # Second pass: the same layout, now with the real page numbers.
    layout = layout_index(invoices, first_pages, flagged, has_project)

    with tempfile.TemporaryDirectory(dir=path.parent) as work:
        front_path = Path(work) / "front.pdf"
        overlay_path = Path(work) / "overlays.pdf"
        links = _write_front(
            front_path, dossier, invoices, layout, volume_label, generated_on, footer, total_pages
        )
        _write_overlays(overlay_path, plan, documents, footer, first_invoice_page, total_pages)

        output = pikepdf.new()
        sources: list[pikepdf.Pdf] = []
        with pikepdf.open(front_path) as front, pikepdf.open(overlay_path) as overlays:
            output.pages.extend(front.pages)
            overlay_index = 0
            for done, (invoice, _count) in enumerate(per_invoice, start=1):
                if cancel is not None and cancel.is_set():
                    raise Cancelled
                document = documents[invoice.id]
                source = _open_source(document.path) if document.path else None
                if source is not None:
                    sources.append(source)
                    originals = list(source.pages)
                else:
                    originals = [None]
                for original in originals:
                    planned = plan[overlay_index]
                    page = output.add_blank_page(page_size=planned.size)
                    parts = []
                    if original is not None:
                        form = output.copy_foreign(original.as_form_xobject())
                        parts.append(_place(page, form, Rectangle(*content_rect(planned.size))))
                    overlay = output.copy_foreign(overlays.pages[overlay_index].as_form_xobject())
                    whole = Rectangle(0, 0, *planned.size)
                    parts.append(_place(page, overlay, whole))
                    page.Contents = output.make_stream(b"\n".join(parts))
                    overlay_index += 1
                on_invoice(done, len(per_invoice))

            _add_links(output, links, first_pages)
            _add_outline(output, per_invoice, first_pages)
            _add_metadata(output, dossier, volume_label, generated_on)

            temporary = Path(work) / "dossier.pdf"
            output.save(
                temporary,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                compress_streams=True,
            )
        for source in sources:
            source.close()
        os.replace(temporary, path)

    return VolumeResult(path, total_pages, first_pages, source_pages)


def _add_links(output: pikepdf.Pdf, links, first_pages: dict[int, int]) -> None:
    by_page: dict[int, list] = {}
    for link in links:
        target = output.pages[first_pages[link.invoice_id] - 1]
        annotation = output.make_indirect(
            Dictionary(
                Type=Name.Annot,
                Subtype=Name.Link,
                Rect=Array(list(link.rect)),
                Border=Array([0, 0, 0]),
                Dest=Array([target.obj, Name.Fit]),
            )
        )
        by_page.setdefault(link.index_page, []).append(annotation)
    for index_page, annotations in by_page.items():
        output.pages[1 + index_page].Annots = Array(annotations)


def _add_outline(output: pikepdf.Pdf, per_invoice, first_pages: dict[int, int]) -> None:
    with output.open_outline() as outline:
        outline.root.append(OutlineItem(t("pdf.index.title"), 1))
        for invoice, _count in per_invoice:
            outline.root.append(OutlineItem(outline_title(invoice), first_pages[invoice.id] - 1))
    output.Root.PageMode = Name.UseOutlines


def _add_metadata(
    output: pikepdf.Pdf, dossier: Dossier, volume_label: str, generated_on: date
) -> None:
    title = f"{t('pdf.title')} {dossier.params.year}"
    if volume_label:
        title = f"{title} {volume_label}"
    with output.open_metadata(set_pikepdf_as_editor=False) as metadata:
        metadata["dc:title"] = title
        metadata["dc:creator"] = [dossier.company.name]
        metadata["dc:description"] = format_date(generated_on)
        metadata["xmp:CreatorTool"] = "DossierFactures"
    output.Root.Lang = pikepdf.String("ca")
