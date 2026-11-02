"""Capture the real window with synthetic data, to see how it looks on Windows from CI."""

import json
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

from PIL import ImageGrab

sys.path.insert(0, str(Path(__file__).parent))

from conftest import FIXTURES, FakeBackend
from dossier_factures import gui as gui_package  # noqa: F401
from dossier_factures.config import AppConfig
from dossier_factures.gui import app as app_module
from gui_scenario import fill_documents


def pump(root, until, timeout=60):
    deadline = time.monotonic() + timeout
    while not until():
        root.update()
        time.sleep(0.02)
        if time.monotonic() > deadline:
            raise TimeoutError
    for _ in range(10):
        root.update()
        time.sleep(0.02)


def capture(root, path):
    root.lift()
    root.attributes("-topmost", True)
    root.update()
    time.sleep(0.4)
    root.update()
    box = (
        root.winfo_rootx(),
        root.winfo_rooty(),
        root.winfo_rootx() + root.winfo_width(),
        root.winfo_rooty() + root.winfo_height(),
    )
    ImageGrab.grab(bbox=box).save(path)
    print("saved", path)


def main(out: str) -> None:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tables = json.loads((FIXTURES / "odoo_scenario.json").read_text(encoding="utf-8"))
    backend = FakeBackend(fill_documents(tables))
    app_module.set_api_key = lambda *_args: True
    app_module.get_api_key = lambda *_args: ""
    work = Path(tempfile.mkdtemp())

    root = tk.Tk()
    app = app_module.App(
        root,
        config=AppConfig(output_dir=str(work)),
        backend_factory=lambda *_args: backend,
        config_file=work / "config.json",
    )
    pump(root, lambda: True)
    capture(root, out_dir / "1-inici.png")

    app.url.set("https://entitat.odoo.com")
    app.api_key.set("clau-de-prova")
    app.on_test()
    pump(root, lambda: app.backend is not None)
    capture(root, out_dir / "2-connectat.png")

    app.on_generate()
    pump(root, lambda: app.result is not None)
    capture(root, out_dir / "3-resum.png")
    app.close()


if __name__ == "__main__":
    main(sys.argv[1])
