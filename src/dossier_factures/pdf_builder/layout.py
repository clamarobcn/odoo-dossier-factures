"""Page geometry. Every page of the dossier is A4, whatever size the original declares."""

from reportlab.lib.pagesizes import A4, landscape

PORTRAIT = A4
LANDSCAPE = landscape(A4)

# The original is reduced to about 92 %, and the band and footer use the space this frees,
# so nothing is ever written on top of the scanned document.
SCALE = 0.92
BAND_SPACE = 43.0
FOOTER_SPACE = 24.0
BAND_TOP_GAP = 4.0
BAND_HEIGHT = 34.0
BAND_MARGIN = 12.0
FOOTER_BASELINE = 10.0


def output_size(width: float, height: float) -> tuple[float, float]:
    return LANDSCAPE if width > height else PORTRAIT


def content_rect(size: tuple[float, float]) -> tuple[float, float, float, float]:
    """The area where the original goes, as (left, bottom, right, top)."""
    width, height = size
    side = width * (1 - SCALE) / 2
    return side, FOOTER_SPACE, width - side, height - BAND_SPACE
