"""Choose the document of each invoice and download its content."""

import base64
import hashlib
from collections import defaultdict
from typing import Any

from ..model import AttachmentRef, Notice, NoticeCode, attachment_kind
from ..odoo_client import AttachmentIntegrityError, MissingFieldsError, OdooBackend, chunks

ATTACHMENT_MODEL = "ir.attachment"
ATTACHMENT_FIELDS = ["name", "mimetype", "file_size", "checksum", "res_id"]
# Online 19.3 removed "datas"; older versions have it and it is always base64.
CONTENT_FIELDS = ("datas", "raw")


def _ref(record: dict[str, Any]) -> AttachmentRef:
    return AttachmentRef(
        id=record["id"],
        name=record["name"] or "",
        mimetype=record["mimetype"] or "",
        size=record["file_size"] or 0,
        checksum=record["checksum"] or "",
    )


def _first_of_kind(records: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    for record in records:
        if attachment_kind(record["mimetype"], record["name"]) == kind:
            return record
    return None


def select_attachments(
    backend: OdooBackend, moves: list[dict[str, Any]]
) -> tuple[dict[int, AttachmentRef | None], list[Notice]]:
    """Pick the main attachment, else the first PDF, else the first image."""
    move_ids = [m["id"] for m in moves]
    by_move: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for batch in chunks(move_ids):
        for record in backend.search_read_all(
            ATTACHMENT_MODEL,
            [("res_model", "=", "account.move"), ("res_id", "in", list(batch))],
            ATTACHMENT_FIELDS,
        ):
            by_move[record["res_id"]].append(record)

    selected: dict[int, AttachmentRef | None] = {}
    notices: list[Notice] = []
    for move in moves:
        records = by_move.get(move["id"], [])
        main_id = move["message_main_attachment_id"][0] if move["message_main_attachment_id"] else 0
        main = next((r for r in records if r["id"] == main_id), None)
        if main is None and main_id:
            found = backend.read(ATTACHMENT_MODEL, [main_id], ATTACHMENT_FIELDS)
            main = found[0] if found else None
        if main is not None and attachment_kind(main["mimetype"], main["name"]) is None:
            main = None
        chosen = main or _first_of_kind(records, "pdf") or _first_of_kind(records, "image")
        selected[move["id"]] = _ref(chosen) if chosen else None
        if chosen is None:
            notices.append(Notice(NoticeCode.MISSING_ATTACHMENT, move["name"]))
            continue
        others = [
            r["name"]
            for r in records
            if r["id"] != chosen["id"] and attachment_kind(r["mimetype"], r["name"])
        ]
        if others:
            notices.append(
                Notice(NoticeCode.OTHER_ATTACHMENTS, move["name"], {"count": len(others)})
            )
    return selected, notices


def detect_content_field(backend: OdooBackend) -> str:
    fields = backend.fields_get(ATTACHMENT_MODEL)
    for name in CONTENT_FIELDS:
        if name in fields:
            return name
    raise MissingFieldsError({ATTACHMENT_MODEL: list(CONTENT_FIELDS)})


def fetch_attachment_content(
    backend: OdooBackend, attachment: AttachmentRef, content_field: str
) -> bytes:
    """Download one attachment and verify it against the server checksum."""
    records = backend.call(ATTACHMENT_MODEL, "read", ids=[attachment.id], fields=[content_field])
    encoded = records[0].get(content_field) if records else None
    if not encoded:
        raise AttachmentIntegrityError(attachment.name)
    content = base64.b64decode(encoded)
    if attachment.checksum and hashlib.sha1(content).hexdigest() != attachment.checksum:
        raise AttachmentIntegrityError(attachment.name)
    return content
