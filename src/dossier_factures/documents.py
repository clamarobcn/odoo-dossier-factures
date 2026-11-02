"""Download each invoice's document and leave it ready as a readable PDF."""

import io
import logging
import shutil
from pathlib import Path

import pikepdf
from PIL import Image, ImageOps, UnidentifiedImageError

from .compress import CompressOptions, CompressResult, compress_pdf
from .extract import fetch_attachment_content
from .i18n import t
from .model import AttachmentRef, Invoice, Notice, NoticeCode
from .odoo_client import AttachmentIntegrityError, OdooBackend
from .pdf_builder import Document

logger = logging.getLogger(__name__)

A4_INCHES = (8.27, 11.69)
IMAGE_JPEG_QUALITY = 85
DOWNLOAD_ATTEMPTS = 2


def image_to_pdf(content: bytes, target: Path) -> None:
    """Turn a JPG or PNG into a one-page PDF that fits on A4."""
    with Image.open(io.BytesIO(content)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    resolution = max(image.width / A4_INCHES[0], image.height / A4_INCHES[1], 72.0)
    image.save(target, "PDF", resolution=resolution, quality=IMAGE_JPEG_QUALITY)


def _strip_junk_around_pdf(content: bytes) -> bytes | None:
    """Keep only what lies between the PDF header and the last end-of-file mark."""
    start = content.find(b"%PDF-")
    if start < 0:
        return None
    end = content.rfind(b"%%EOF")
    return content[start : end + 5] if end > start else content[start:]


def _pages_are_readable(pdf: pikepdf.Pdf) -> bool:
    if len(pdf.pages) == 0:
        return False
    for page in pdf.pages:
        page.as_form_xobject()
    return True


def prepare_pdf(path: Path) -> str:
    """Make sure a PDF can be used, repairing it if needed.

    Returns ``"ok"``, ``"repaired"`` or ``"unreadable"``. qpdf rebuilds what it can on opening
    and reports it as warnings; saving again leaves a clean file for the following steps.
    """
    errors = (pikepdf.PdfError, pikepdf.PasswordError, OSError, ValueError, RuntimeError)
    try:
        with pikepdf.open(path) as pdf:
            if not _pages_are_readable(pdf):
                return "unreadable"
            if not pdf.get_warnings():
                return "ok"
            repaired = path.with_suffix(".repaired")
            pdf.save(repaired)
        repaired.replace(path)
        return "repaired"
    except pikepdf.PasswordError:
        return "unreadable"
    except errors:
        pass

    stripped = _strip_junk_around_pdf(path.read_bytes())
    if stripped is None:
        return "unreadable"
    try:
        with pikepdf.open(io.BytesIO(stripped)) as pdf:
            if not _pages_are_readable(pdf):
                return "unreadable"
            pdf.save(path)
        return "repaired"
    except errors:
        return "unreadable"


class DocumentStore:
    def __init__(
        self,
        backend: OdooBackend,
        content_field: str,
        work_dir: Path,
        cache_dir: Path | None = None,
        compress: CompressOptions | None = None,
    ) -> None:
        self.backend = backend
        self.content_field = content_field
        self.work_dir = Path(work_dir)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.compress = compress
        self.original_bytes = 0
        self.final_bytes = 0

    def _download(self, attachment: AttachmentRef) -> bytes:
        cached = None
        if self.cache_dir and attachment.checksum:
            cached = self.cache_dir / attachment.checksum
            if cached.exists():
                return cached.read_bytes()
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                content = fetch_attachment_content(self.backend, attachment, self.content_field)
                break
            except AttachmentIntegrityError:
                logger.info("Integrity check failed on attempt %d", attempt)
                if attempt == DOWNLOAD_ATTEMPTS:
                    raise
        if cached:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = cached.with_suffix(".part")
            temporary.write_bytes(content)
            shutil.move(temporary, cached)
        return content

    def fetch(self, invoice: Invoice) -> tuple[Document, list[Notice]]:
        attachment = invoice.attachment
        if attachment is None:
            return Document(problem=t("pdf.warning.no_document")), []
        target = self.work_dir / f"{attachment.id}.pdf"
        try:
            content = self._download(attachment)
            if attachment.kind == "image":
                image_to_pdf(content, target)
            else:
                target.write_bytes(content)
        except (AttachmentIntegrityError, UnidentifiedImageError, OSError) as error:
            logger.warning("Document %s failed: %s", attachment.id, type(error).__name__)
            return self._unreadable(invoice, attachment)
        state = prepare_pdf(target)
        if state == "unreadable":
            return self._unreadable(invoice, attachment)
        notices = []
        if state == "repaired":
            notices.append(
                Notice(NoticeCode.DOCUMENT_REPAIRED, invoice.name, {"name": attachment.name})
            )
        self._account(self._compressed(target))
        return Document(path=target), notices

    def _compressed(self, target: Path) -> CompressResult:
        if self.compress is None:
            size = target.stat().st_size
            return CompressResult(size, size, 0)
        try:
            return compress_pdf(target, self.compress)
        except (pikepdf.PdfError, OSError, ValueError) as error:
            # Compression only saves space: the document is still good without it.
            logger.warning("Compression skipped: %s", type(error).__name__)
            size = target.stat().st_size
            return CompressResult(size, size, 0)

    def _account(self, result: CompressResult) -> None:
        self.original_bytes += result.original_size
        self.final_bytes += result.final_size

    @staticmethod
    def _unreadable(invoice: Invoice, attachment: AttachmentRef) -> tuple[Document, list[Notice]]:
        notice = Notice(NoticeCode.DOCUMENT_UNREADABLE, invoice.name, {"name": attachment.name})
        return Document(problem=t("pdf.warning.unreadable"), detail=attachment.name), [notice]
