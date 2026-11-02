"""User settings stored in a JSON file. The API key never goes here (see credentials.py)."""

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "DossierFactures"


def config_path() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / APP_NAME / "config.json"


@dataclass
class AppConfig:
    url: str = ""
    database: str = ""
    user: str = ""
    output_dir: str = ""
    target_dpi: int = 150
    jpeg_quality: int = 70
    split_threshold_mb: int = 100
    include_receipts: bool = True
    include_other_expenses: bool = True
    show_personnel_names: bool = False
    # Journal name -> label of the payment form, when the journal type is not enough.
    payment_form_overrides: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        path = path or config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path | None = None) -> None:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
