"""The whole generation, shared by the interface and the command line."""

import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .documents import DocumentStore
from .excel_builder import build_workbook, excel_filename
from .extract import detect_content_field, extract_dossier
from .i18n import t
from .model import Dossier, ExtractParams, Notice
from .odoo_client import OdooBackend
from .paths import new_output_folder
from .pdf_builder import Cancelled, Document, VolumeResult, build_volume, pdf_filename

Progress = Callable[[str, int | None, int | None], None]


def _silent(_message: str, _current: int | None = None, _total: int | None = None) -> None:
    pass


@dataclass
class GenerateOptions:
    output_dir: Path
    form_overrides: dict[str, str] = field(default_factory=dict)
    cache_dir: Path | None = None
    generated_on: date | None = None


@dataclass
class GenerationResult:
    folder: Path
    excel_path: Path
    volumes: list[VolumeResult]
    dossier: Dossier
    notices: list[Notice]

    @property
    def page_count(self) -> int:
        return sum(v.page_count for v in self.volumes)


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled


def _fetch_documents(
    store: DocumentStore,
    dossier: Dossier,
    notices: list[Notice],
    progress: Progress,
    cancel: threading.Event | None,
) -> dict[int, Document]:
    documents: dict[int, Document] = {}
    total = len(dossier.invoices)
    for done, invoice in enumerate(dossier.invoices, start=1):
        _check(cancel)
        documents[invoice.id], found = store.fetch(invoice)
        notices.extend(found)
        progress(t("step.downloading"), done, total)
    return documents


def generate(
    backend: OdooBackend,
    params: ExtractParams,
    options: GenerateOptions,
    progress: Progress = _silent,
    cancel: threading.Event | None = None,
) -> GenerationResult:
    """Read Odoo and write every output. A cancelled or failed run leaves no output behind."""
    dossier = extract_dossier(
        backend, params, options.form_overrides, progress=lambda m: progress(m, None, None)
    )
    notices = list(dossier.notices)
    content_field = detect_content_field(backend)

    with tempfile.TemporaryDirectory() as work:
        store = DocumentStore(backend, content_field, Path(work), options.cache_dir)
        documents = _fetch_documents(store, dossier, notices, progress, cancel)
        _check(cancel)

        folder = new_output_folder(options.output_dir, params.year, options.generated_on)
        try:
            volume = build_volume(
                dossier,
                dossier.invoices,
                documents,
                folder / pdf_filename(params.year),
                generated_on=options.generated_on,
                cancel=cancel,
                on_invoice=lambda d, n: progress(t("step.building_pdf"), d, n),
            )
            progress(t("step.building_excel"), None, None)
            remarks = {
                i.id: t("excel.remark.unreadable")
                for i in dossier.invoices
                if i.attachment is not None and documents[i.id].path is None
            }
            excel_path = folder / excel_filename(params.year)
            build_workbook(
                dossier,
                excel_path,
                pages=volume.first_pages,
                document_remarks=remarks,
                generated_on=options.generated_on,
            )
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    progress(t("step.done"), None, None)
    return GenerationResult(folder, excel_path, [volume], dossier, notices)
