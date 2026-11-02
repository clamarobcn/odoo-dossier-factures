"""A filled-in synthetic scenario, shared by the interface tests and the screenshots."""

import base64
import hashlib
import io

from PIL import Image
from reportlab.pdfgen.canvas import Canvas


def _pdf() -> bytes:
    buffer = io.BytesIO()
    canvas = Canvas(buffer)
    canvas.drawString(100, 500, "FACTURA D'EXEMPLE")
    canvas.save()
    return buffer.getvalue()


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (600, 800), (200, 200, 200)).save(buffer, "JPEG")
    return buffer.getvalue()


def fill_documents(scenario: dict) -> dict:
    for record in scenario["ir.attachment"]:
        content = _jpeg() if record["mimetype"] == "image/jpeg" else _pdf()
        record["raw"] = base64.b64encode(content).decode()
        record["checksum"] = hashlib.sha1(content).hexdigest()
        record["file_size"] = len(content)
    return scenario
