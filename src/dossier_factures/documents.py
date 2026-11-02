"""Download each invoice's document and leave it ready as a readable PDF."""

import io
import logging
import shutil
from pathlib import Path

import pikepdf
from PIL import Image, ImageOps, UnidentifiedImageError

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


def is_readable_pdf(path: Path) -> bool:
    try:
        with pikepdf.open(path) as pdf:
            return len(pdf.pages) > 0
    except (pikepdf.PdfError, pikepdf.PasswordError, OSError, ValueError):
        return False


class DocumentStore:
    def __init__(
        self,
        backend: OdooBackend,
        content_field: str,
        work_dir: Path,
        cache_dir: Path | None = None,
    ) -> None:
        self.backend = backend
        self.content_field = content_field
        self.work_dir = Path(work_dir)
        self.cache_dir = Path(cache_dir) if cache_dir else None

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
        if not is_readable_pdf(target):
            return self._unreadable(invoice, attachment)
        return Document(path=target), []

    @staticmethod
    def _unreadable(invoice: Invoice, attachment: AttachmentRef) -> tuple[Document, list[Notice]]:
        notice = Notice(NoticeCode.DOCUMENT_UNREADABLE, invoice.name, {"name": attachment.name})
        return Document(problem=t("pdf.warning.unreadable"), detail=attachment.name), [notice]
