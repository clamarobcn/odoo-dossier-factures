"""The dossier as a PDF."""

from ..i18n import t
from .assemble import Cancelled, Document, VolumeResult, build_volume


def pdf_filename(year: int) -> str:
    return t("output.pdf_filename", year=year)


__all__ = ["Cancelled", "Document", "VolumeResult", "build_volume", "pdf_filename"]
