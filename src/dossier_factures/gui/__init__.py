"""Desktop interface."""

import contextlib
import sys
import tkinter as tk

from ..summary import setup_file_log
from .app import App


def _enable_sharp_text() -> None:
    """Windows blurs the window on high-resolution screens unless told not to."""
    if sys.platform == "win32":
        import ctypes

        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(1)


def main() -> int:
    setup_file_log()
    _enable_sharp_text()
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


__all__ = ["App", "main"]
