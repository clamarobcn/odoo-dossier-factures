"""Recompress scanned images. Light PDFs, with text and vector graphics, are left alone."""

import io
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import pikepdf
from pikepdf import Name, PdfImage
from PIL import Image

from .pdf_builder.assemble import form_size
from .pdf_builder.layout import content_rect, output_size

logger = logging.getLogger(__name__)

DEFAULT_DPI = 150
DEFAULT_QUALITY = 70
# Below this average per page there is nothing worth recompressing.
LIGHT_BYTES_PER_PAGE = 150 * 1024
# Images this small are logos and icons.
MIN_IMAGE_BYTES = 20 * 1024
# A small excess over the target is not worth another generation of JPEG loss.
DOWNSAMPLE_TOLERANCE = 1.1
# Below this saving the original image is better kept.
MIN_SAVING = 0.9


@dataclass(frozen=True)
class CompressOptions:
    dpi: int = DEFAULT_DPI
    quality: int = DEFAULT_QUALITY


@dataclass(frozen=True)
class CompressResult:
    original_size: int
    final_size: int
    images_changed: int


def _placed_widths(page: pikepdf.Page) -> dict[str, float]:
    """Width in points at which each image is drawn on the page, before any scaling."""
    widths: dict[str, float] = {}
    stack: list[tuple[float, float, float, float, float, float]] = []
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for operands, operator in pikepdf.parse_content_stream(page, "q Q cm Do"):
        name = str(operator)
        if name == "q":
            stack.append(ctm)
        elif name == "Q":
            if stack:
                ctm = stack.pop()
        elif name == "cm":
            a, b, c, d, e, f = (float(v) for v in operands)
            A, B, C, D, E, F = ctm
            ctm = (
                a * A + b * C,
                a * B + b * D,
                c * A + d * C,
                c * B + d * D,
                e * A + f * C + E,
                e * B + f * D + F,
            )
        elif name == "Do":
            image = str(operands[0])
            width = math.hypot(ctm[0], ctm[1])
            # Drawn more than once: the largest use decides the resolution it needs.
            widths[image] = max(widths.get(image, 0.0), width)
    return widths


def _page_scale(page: pikepdf.Page) -> float:
    """Reduction this page will get in the dossier, where every page becomes A4."""
    width, height = form_size(page.as_form_xobject())
    left, bottom, right, top = content_rect(output_size(width, height))
    return min((right - left) / width, (top - bottom) / height, 1.0)


def _recompress_image(
    image: pikepdf.Stream, placed_width: float, scale: float, options: CompressOptions
) -> bool:
    if image.get(Name.ImageMask) or image.get(Name.BitsPerComponent) == 1:
        return False
    if Name.Decode in image:
        return False
    original_size = len(image.read_raw_bytes())
    if original_size < MIN_IMAGE_BYTES:
        return False
    pil = PdfImage(image).as_pil_image()
    if pil.mode == "1":
        return False

    effective_dpi = pil.width / (placed_width * scale / 72)
    if effective_dpi > options.dpi * DOWNSAMPLE_TOLERANCE:
        factor = options.dpi / effective_dpi
        size = (max(1, round(pil.width * factor)), max(1, round(pil.height * factor)))
        pil = pil.resize(size, Image.Resampling.LANCZOS)
    if pil.mode not in ("RGB", "L"):
        pil = pil.convert("RGB")

    buffer = io.BytesIO()
    pil.save(buffer, "JPEG", quality=options.quality, optimize=True)
    data = buffer.getvalue()
    if len(data) >= original_size * MIN_SAVING:
        return False

    image.write(data, filter=Name.DCTDecode)
    image.Width, image.Height = pil.width, pil.height
    image.ColorSpace = Name.DeviceRGB if pil.mode == "RGB" else Name.DeviceGray
    image.BitsPerComponent = 8
    if Name.DecodeParms in image:
        del image.DecodeParms
    return True


def compress_pdf(path: Path, options: CompressOptions) -> CompressResult:
    """Recompress the images of a PDF in place, only if that makes it smaller.

    Images are replaced where they are, leaving the page transformations untouched: scanners
    may store an image flipped and rely on those transformations to show it right.
    """
    path = Path(path)
    original_size = path.stat().st_size
    with pikepdf.open(path) as pdf:
        if original_size / max(len(pdf.pages), 1) < LIGHT_BYTES_PER_PAGE:
            return CompressResult(original_size, original_size, 0)
        changed = 0
        done: set[tuple[int, int]] = set()
        for page in pdf.pages:
            xobjects = page.obj.get(Name.Resources, {}).get(Name.XObject, {})
            scale = _page_scale(page)
            for name, width in _placed_widths(page).items():
                image = xobjects.get(name)
                if image is None or image.get(Name.Subtype) != Name.Image or not width:
                    continue
                if image.objgen in done:
                    continue
                done.add(image.objgen)
                try:
                    changed += _recompress_image(image, width, scale, options)
                except (pikepdf.PdfError, NotImplementedError, OSError, ValueError) as error:
                    logger.info("Image kept as it was: %s", type(error).__name__)
        if not changed:
            return CompressResult(original_size, original_size, 0)
        temporary = path.with_suffix(".tmp")
        pdf.save(
            temporary,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            compress_streams=True,
        )
    final_size = temporary.stat().st_size
    if final_size >= original_size:
        temporary.unlink()
        return CompressResult(original_size, original_size, 0)
    temporary.replace(path)
    return CompressResult(original_size, final_size, changed)
