"""Desktop interface."""

import contextlib
import sys
import tkinter as tk
from pathlib import Path

from ..summary import setup_file_log
from .app import App


def _enable_sharp_text() -> None:
    """Windows blurs the window on high-resolution screens unless told not to."""
    if sys.platform == "win32":
        import ctypes

        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(1)


def _set_icon(root: tk.Tk) -> None:
    if sys.platform == "win32":
        with contextlib.suppress(tk.TclError):
            root.iconbitmap(default=str(Path(__file__).with_name("icon.ico")))


def _close_splash() -> None:
    """The packaged program shows a picture while it unpacks; the window takes over from it."""
    with contextlib.suppress(ImportError):
        import pyi_splash

        pyi_splash.close()


def main() -> int:
    setup_file_log()
    _enable_sharp_text()
    root = tk.Tk()
    _set_icon(root)
    App(root)
    _close_splash()
    root.mainloop()
    return 0


__all__ = ["App", "main"]
