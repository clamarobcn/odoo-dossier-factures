"""What the person sees at the end of a run or when something fails."""

import logging
import os
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from .config import config_path
from .formatting import format_amount
from .i18n import t
from .model import Notice, NoticeCode
from .odoo_client import OdooError
from .pdf_builder import Cancelled
from .pipeline import GenerationResult, NothingToGenerate

logger = logging.getLogger(__name__)

# The ones that can make the justification incomplete come first.
NOTICE_ORDER = [
    NoticeCode.DOCUMENT_UNREADABLE,
    NoticeCode.MISSING_ATTACHMENT,
    NoticeCode.NO_PAYMENT,
    NoticeCode.PAYMENT_MISMATCH,
    NoticeCode.POSSIBLE_DUPLICATE,
    NoticeCode.UNRECONCILED_PAYMENT,
    NoticeCode.DOCUMENT_REPAIRED,
    NoticeCode.MISSING_SUPPLIER,
    NoticeCode.MISSING_VAT,
    NoticeCode.FOREIGN_CURRENCY,
    NoticeCode.OTHER_ATTACHMENTS,
    NoticeCode.RECEIPTS_EXCLUDED,
    NoticeCode.OTHER_EXPENSES,
]


def describe_error(error: BaseException) -> str:
    """A message for a non-technical person. Details go to the log, never a traceback here."""
    if isinstance(error, OdooError):
        return t(error.i18n_key)
    if isinstance(error, Cancelled):
        return t("error.cancelled")
    if isinstance(error, NothingToGenerate):
        return t("error.nothing_to_generate")
    if isinstance(error, OSError):
        return t("error.output_folder")
    return t("error.unexpected")


def notice_text(notice: Notice) -> str:
    message = t(f"notice.{notice.code}", **notice.params)
    return f"{notice.invoice_name}: {message}" if notice.invoice_name else message


def _megabytes(path: Path) -> str:
    return f"{path.stat().st_size / 1024 / 1024:.1f}".replace(".", ",")


def summary_lines(result: GenerationResult) -> list[str]:
    dossier = result.dossier
    documented = dossier.documented
    others = dossier.undocumented_expenses
    total = sum((i.total for i in documented), Decimal("0.00"))
    lines = [
        t("summary.documents", count=len(documented), total=format_amount(total)),
    ]
    if others:
        other_total = sum((i.total for i in others), Decimal("0.00"))
        lines.append(t("summary.others", count=len(others), total=format_amount(other_total)))
        lines.append(t("summary.grand_total", total=format_amount(dossier.total)))
    lines.append(t("summary.pages", count=result.page_count))
    lines.append("")
    lines.append(t("summary.files"))
    for volume in result.volumes:
        pages = t("summary.file_pages", count=volume.page_count)
        lines.append(f"  {volume.path.name} ({pages}, {_megabytes(volume.path)} MB)")
    lines.append(f"  {result.excel_path.name}")
    lines.append("")
    lines.extend(notice_lines(result.notices))
    return lines


def notice_lines(notices: list[Notice]) -> list[str]:
    if not notices:
        return [t("summary.no_notices")]
    groups: dict[NoticeCode, list[Notice]] = defaultdict(list)
    for notice in notices:
        groups[notice.code].append(notice)
    lines = [t("summary.notices", count=len(notices))]
    for code in NOTICE_ORDER:
        if code not in groups:
            continue
        lines.append("")
        lines.append(f"{t(f'notice_title.{code}')} ({len(groups[code])})")
        lines.extend(f"  • {notice_text(n)}" for n in groups[code])
    return lines


def default_log_path() -> Path:
    return config_path().parent / "dossier.log"


def setup_file_log(path: Path | None = None) -> None:
    """Keep a technical log for support. The API key is never logged."""
    path = path or default_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1024 * 1024:
            os.replace(path, path.with_suffix(".log.1"))
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)
