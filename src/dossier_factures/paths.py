"""Where the generated files go."""

from datetime import date
from pathlib import Path

from .i18n import t


def new_output_folder(base: Path, year: int, today: date | None = None) -> Path:
    """Create a subfolder named after the year and date, never reusing an existing one."""
    today = today or date.today()
    name = t("output.folder", year=year, date=today.isoformat())
    folder = Path(base) / name
    suffix = 2
    while folder.exists():
        folder = Path(base) / f"{name} ({suffix})"
        suffix += 1
    folder.mkdir(parents=True)
    return folder
