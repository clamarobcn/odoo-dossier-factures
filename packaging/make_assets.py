"""Draw the program icon and the splash picture. Run by hand; the results are committed."""

from pathlib import Path

import reportlab
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
FONTS = Path(reportlab.__file__).parent / "fonts"
BLUE = (31, 78, 121)
LINE = (150, 170, 190)
FOLD = (205, 218, 230)
SCALE = 4


def draw_icon(size: int) -> Image.Image:
    """A sheet with a folded corner and text lines, readable down to 16 px."""
    big = size * SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = big / 16
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=3 * unit, fill=BLUE)
    left, top, right, bottom = 3.5 * unit, 2 * unit, 12.5 * unit, 14 * unit
    corner = 3 * unit
    draw.polygon(
        [
            (left, top),
            (right - corner, top),
            (right, top + corner),
            (right, bottom),
            (left, bottom),
        ],
        fill="white",
    )
    draw.polygon(
        [(right - corner, top), (right, top + corner), (right - corner, top + corner)], fill=FOLD
    )
    for row in (6.5, 9, 11.5):
        y = row * unit
        draw.rectangle((left + unit, y, right - unit, y + 0.9 * unit), fill=LINE)
    return image.resize((size, size), Image.LANCZOS)


def draw_splash() -> Image.Image:
    image = Image.new("RGB", (440, 150), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 439, 149), outline=BLUE, width=2)
    icon = draw_icon(88)
    image.paste(icon, (28, 31), icon)
    title = ImageFont.truetype(str(FONTS / "VeraBd.ttf"), 24)
    note = ImageFont.truetype(str(FONTS / "Vera.ttf"), 15)
    draw.text((136, 46), "Dossier de factures", font=title, fill=BLUE)
    draw.text((136, 84), "S'està obrint, un moment…", font=note, fill=(80, 80, 80))
    return image


def main() -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    target = ROOT.parent / "src" / "dossier_factures" / "gui" / "icon.ico"
    draw_icon(256).save(target, sizes=[(s, s) for s in sizes])
    draw_splash().save(ROOT / "splash.png", optimize=True)


if __name__ == "__main__":
    main()
