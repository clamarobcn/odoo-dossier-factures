"""The main window. All the work is delegated: this module only deals with the interface."""

import contextlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .. import __version__
from ..compress import CompressOptions
from ..config import AppConfig
from ..credentials import get_api_key, set_api_key
from ..extract import (
    ConnectionInfo,
    check_connection,
    latest_invoice_year,
    list_analytic_accounts,
    list_suppliers,
    list_years,
)
from ..i18n import t
from ..model import ExtractParams
from ..odoo_client import Json2Backend, OdooBackend, OdooError, guess_database, normalize_base_url
from ..pipeline import GenerateOptions, GenerationResult, generate
from ..summary import describe_error, summary_lines
from .tasks import TaskRunner

logger = logging.getLogger(__name__)

POLL_MS = 80
WINDOW_HEIGHT = 720
MIN_WINDOW_HEIGHT = 600
PADDING = 8
OK_COLOR = "#1E7B34"
ERROR_COLOR = "#B3261E"
MUTED_COLOR = "#595959"

BackendFactory = Callable[[str, str, str], OdooBackend]


def _default_backend(url: str, api_key: str, database: str) -> OdooBackend:
    return Json2Backend(url, api_key, database)


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


@dataclass
class _Connection:
    info: ConnectionInfo
    years: list[int]
    latest_year: int | None
    projects: list[dict[str, Any]]
    suppliers: list[dict[str, Any]]


class App:
    def __init__(
        self,
        root: tk.Tk,
        config: AppConfig | None = None,
        backend_factory: BackendFactory = _default_backend,
        config_file: Path | None = None,
    ) -> None:
        self.root = root
        self.config = config if config is not None else AppConfig.load(config_file)
        self.config_file = config_file
        self.backend_factory = backend_factory
        self.tasks = TaskRunner()
        self.backend: OdooBackend | None = None
        self.result: GenerationResult | None = None
        self.projects: list[dict[str, Any]] = []
        self.suppliers: list[dict[str, Any]] = []
        self.companies: list = []
        self.years: list[int] = []
        self.last_folder: Path | None = None
        self._cache = tempfile.mkdtemp(prefix="dossier-")
        self._step = ""
        self._connected_as: tuple | None = None

        root.title(f"{t('gui.title')} {__version__}")
        # Taskbar and title bar take about 100 px: the buttons must stay visible on a small laptop.
        height = min(WINDOW_HEIGHT, root.winfo_screenheight() - 100)
        root.minsize(760, min(MIN_WINDOW_HEIGHT, height))
        root.geometry(f"840x{height}")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        self._build_connection()
        self._build_generation()
        self._build_progress()
        self._set_connected(False)
        self._schedule_poll()
        self._connect_on_start()

    # ---- Layout ----

    def _build_connection(self) -> None:
        frame = ttk.LabelFrame(self.root, text=t("gui.connection"), padding=PADDING)
        frame.grid(row=0, column=0, sticky="ew", padx=PADDING, pady=(PADDING, 4))
        frame.columnconfigure(1, weight=2)
        frame.columnconfigure(3, weight=1)

        self.url = tk.StringVar(value=self.config.url)
        self.database = tk.StringVar(value=self.config.database)
        self.user = tk.StringVar(value=self.config.user)
        self.api_key = tk.StringVar(value=self._saved_key())

        ttk.Label(frame, text=t("gui.url")).grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
        url_entry = ttk.Entry(frame, textvariable=self.url)
        url_entry.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=2)
        url_entry.bind("<FocusOut>", lambda _event: self._fill_database())
        ttk.Label(frame, text=t("gui.database")).grid(row=0, column=2, sticky="w", padx=(0, 6))
        ttk.Entry(frame, textvariable=self.database).grid(row=0, column=3, sticky="ew", pady=2)

        ttk.Label(frame, text=t("gui.user")).grid(row=1, column=0, sticky="w", padx=(0, 6), pady=2)
        ttk.Entry(frame, textvariable=self.user).grid(
            row=1, column=1, sticky="ew", padx=(0, 12), pady=2
        )
        ttk.Label(frame, text=t("gui.api_key")).grid(row=1, column=2, sticky="w", padx=(0, 6))
        ttk.Entry(frame, textvariable=self.api_key, show="•").grid(
            row=1, column=3, sticky="ew", pady=2
        )

        ttk.Label(frame, text=t("gui.api_key_hint"), foreground=MUTED_COLOR).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(2, 4)
        )
        self.test_button = ttk.Button(frame, text=t("gui.test"), command=self.on_test)
        self.test_button.grid(row=3, column=0, sticky="w", pady=(2, 0))
        self.connection_status = ttk.Label(frame, text="", wraplength=620, justify="left")
        self.connection_status.grid(row=3, column=1, columnspan=3, sticky="w", padx=(6, 0))

        for variable in (self.url, self.database, self.user, self.api_key):
            variable.trace_add("write", lambda *_: self._on_connection_edited())

    def _build_generation(self) -> None:
        frame = ttk.LabelFrame(self.root, text=t("gui.generation"), padding=PADDING)
        frame.grid(row=1, column=0, sticky="ew", padx=PADDING, pady=4)
        frame.columnconfigure(1, weight=1)
        row = 0

        self.company_label = ttk.Label(frame, text=t("gui.company"))
        self.company = ttk.Combobox(frame, state="readonly")
        self.company_label.grid(row=row, column=0, sticky="w", padx=(0, 6), pady=2)
        self.company.grid(row=row, column=1, sticky="ew", pady=2, columnspan=2)
        self.company.bind("<<ComboboxSelected>>", lambda _event: self._on_company_changed())
        self.company_label.grid_remove()
        self.company.grid_remove()
        row += 1

        ttk.Label(frame, text=t("gui.year")).grid(
            row=row, column=0, sticky="w", padx=(0, 6), pady=2
        )
        self.year = ttk.Combobox(frame, state="readonly", width=8)
        self.year.grid(row=row, column=1, sticky="w", pady=2)
        self.year.bind("<<ComboboxSelected>>", lambda _event: self._on_year_changed())
        row += 1

        ttk.Label(frame, text=t("gui.project")).grid(
            row=row, column=0, sticky="w", padx=(0, 6), pady=2
        )
        self.project = ttk.Combobox(frame, state="readonly")
        self.project.grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
        row += 1

        ttk.Label(frame, text=t("gui.supplier")).grid(
            row=row, column=0, sticky="w", padx=(0, 6), pady=2
        )
        self.supplier = ttk.Combobox(frame, state="readonly")
        self.supplier.grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
        row += 1

        ttk.Label(frame, text=t("gui.format")).grid(
            row=row, column=0, sticky="w", padx=(0, 6), pady=2
        )
        formats = ttk.Frame(frame)
        formats.grid(row=row, column=1, columnspan=2, sticky="w")
        self.split = tk.BooleanVar(value=False)
        ttk.Radiobutton(formats, text=t("gui.single_pdf"), variable=self.split, value=False).grid(
            row=0, column=0, padx=(0, 16)
        )
        ttk.Radiobutton(formats, text=t("gui.quarterly_pdf"), variable=self.split, value=True).grid(
            row=0, column=1
        )
        row += 1

        self.include_receipts = tk.BooleanVar(value=self.config.include_receipts)
        self.include_other = tk.BooleanVar(value=self.config.include_other_expenses)
        self.show_names = tk.BooleanVar(value=self.config.show_personnel_names)
        for variable, key in (
            (self.include_receipts, "gui.include_receipts"),
            (self.include_other, "gui.include_other_expenses"),
            (self.show_names, "gui.show_personnel_names"),
        ):
            ttk.Checkbutton(frame, text=t(key), variable=variable).grid(
                row=row, column=1, columnspan=2, sticky="w"
            )
            row += 1

        ttk.Label(frame, text=t("gui.output_folder")).grid(
            row=row, column=0, sticky="w", padx=(0, 6), pady=(6, 2)
        )
        self.output_dir = tk.StringVar(value=self.config.output_dir or str(Path.home()))
        ttk.Entry(frame, textvariable=self.output_dir).grid(
            row=row, column=1, sticky="ew", padx=(0, 6), pady=(6, 2)
        )
        ttk.Button(frame, text=t("gui.browse"), command=self.on_browse).grid(
            row=row, column=2, pady=(6, 2)
        )
        row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.generate_button = ttk.Button(buttons, text=t("gui.generate"), command=self.on_generate)
        self.generate_button.grid(row=0, column=0, padx=(0, 8))
        self.cancel_button = ttk.Button(
            buttons, text=t("gui.cancel"), command=self.on_cancel, state="disabled"
        )
        self.cancel_button.grid(row=0, column=1)

    def _build_progress(self) -> None:
        frame = ttk.LabelFrame(self.root, text=t("gui.progress"), padding=PADDING)
        frame.grid(row=2, column=0, sticky="nsew", padx=PADDING, pady=(4, PADDING))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        self.status = ttk.Label(frame, text=t("gui.ready"))
        self.status.grid(row=0, column=0, columnspan=2, sticky="w")
        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)

        self.log = tk.Text(
            frame, height=6, wrap="word", state="disabled", relief="flat", font="TkDefaultFont"
        )
        self.log.grid(row=2, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, command=self.log.yview)
        scrollbar.grid(row=2, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

        self.open_button = ttk.Button(
            frame, text=t("gui.open_folder"), command=self.on_open_folder, state="disabled"
        )
        self.open_button.grid(row=3, column=0, sticky="w", pady=(6, 0))

    # ---- Small helpers ----

    def _schedule_poll(self) -> None:
        self.tasks.poll()
        self._poll_id = self.root.after(POLL_MS, self._schedule_poll)

    def _saved_key(self) -> str:
        if not self.config.url:
            return ""
        try:
            return get_api_key(normalize_base_url(self.config.url), self.config.user)
        except OdooError:
            return ""

    def _fill_database(self) -> None:
        if self.database.get().strip():
            return
        with contextlib.suppress(OdooError):
            self.database.set(guess_database(self.url.get()))

    def _connection_fields(self) -> tuple:
        return (self.url.get(), self.database.get(), self.user.get(), self.api_key.get())

    def _on_connection_edited(self) -> None:
        if self._connected_as is not None and self._connection_fields() != self._connected_as:
            self._set_connected(False)

    def _set_connected(self, connected: bool) -> None:
        if not connected:
            self._connected_as = None
            self.backend = None
            self._set_status(self.connection_status, t("gui.not_connected"), MUTED_COLOR)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        busy = self.tasks.busy
        ready = self._connected_as is not None and not busy
        self.generate_button.configure(state="normal" if ready else "disabled")
        self.test_button.configure(state="disabled" if busy else "normal")

    @staticmethod
    def _set_status(label: ttk.Label, text: str, color: str) -> None:
        label.configure(text=text, foreground=color)

    def _write_log(self, lines: list[str], replace: bool = False) -> None:
        self.log.configure(state="normal")
        if replace:
            self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(lines) + "\n")
        self.log.see("1.0" if replace else "end")
        self.log.configure(state="disabled")

    def _start(self, work, on_done, on_progress=lambda *_: None) -> None:
        self.tasks.start(work, on_done, self._on_task_error, on_progress)
        self._refresh_buttons()

    def _on_task_error(self, error: BaseException) -> None:
        """The default handler for a failed task."""
        logger.error("Task failed: %s", type(error).__name__, exc_info=error)
        self._finish_running()
        self._refresh_buttons()
        messagebox.showerror(t("gui.error_title"), describe_error(error), parent=self.root)

    # ---- Connection ----

    def _connect_on_start(self) -> None:
        """With everything saved from last time, the secretary only has to press Generate."""
        if self.url.get() and self.api_key.get():
            self.on_test(quiet=True)

    def on_test(self, quiet: bool = False) -> None:
        self._fill_database()
        if not self.api_key.get().strip():
            self._set_status(self.connection_status, t("gui.missing_key"), ERROR_COLOR)
            return
        fields = self._connection_fields()
        try:
            url = normalize_base_url(fields[0])
        except OdooError as error:
            self._set_status(self.connection_status, describe_error(error), ERROR_COLOR)
            return
        backend = self.backend_factory(url, fields[3].strip(), fields[1].strip())
        self._set_status(self.connection_status, t("gui.testing"), MUTED_COLOR)

        def work(_progress, _cancel) -> _Connection:
            info = check_connection(backend)
            latest = latest_invoice_year(backend)
            years = list_years(backend)
            return _Connection(
                info,
                years,
                latest,
                list_analytic_accounts(backend),
                list_suppliers(backend, latest or years[0]),
            )

        def done(connection: _Connection) -> None:
            self._on_connected(backend, fields, url, connection)

        def failed(error: BaseException) -> None:
            logger.warning("Connection failed: %s", type(error).__name__)
            self._set_status(self.connection_status, describe_error(error), ERROR_COLOR)
            self._refresh_buttons()

        self.tasks.start(work, done, failed)
        self._refresh_buttons()

    def _on_connected(
        self, backend: OdooBackend, fields: tuple, url: str, connection: _Connection
    ) -> None:
        self.backend = backend
        self._connected_as = fields
        info = connection.info
        names = ", ".join(c.name for c in info.companies)
        message = t("gui.connected", company=names, version=info.version)
        color = OK_COLOR
        if not set_api_key(url, fields[2].strip(), fields[3].strip()):
            message = f"{message}\n{t('gui.key_not_saved')}"
            color = ERROR_COLOR
        self._set_status(self.connection_status, message, color)

        self.companies = info.companies
        if len(self.companies) > 1:
            self.company.configure(values=[c.name for c in self.companies])
            self.company.current(0)
            self.company_label.grid()
            self.company.grid()
        else:
            self.company_label.grid_remove()
            self.company.grid_remove()

        self.years = connection.years
        self.year.configure(values=[str(y) for y in self.years])
        selected = connection.latest_year if connection.latest_year in self.years else self.years[0]
        self.year.set(str(selected))

        self.projects = connection.projects
        self.project.configure(values=[t("gui.all"), *[p["name"] for p in self.projects]])
        self.project.current(0)
        self._fill_suppliers(connection.suppliers)

        self._save_config()
        self._refresh_buttons()

    def _fill_suppliers(self, suppliers: list[dict[str, Any]]) -> None:
        self.suppliers = suppliers
        self.supplier.configure(values=[t("gui.all"), *[s["name"] for s in suppliers]])
        self.supplier.current(0)

    def _on_year_changed(self) -> None:
        if self.backend is None or self.tasks.busy:
            return
        backend, year = self.backend, int(self.year.get())

        def done(suppliers) -> None:
            self._fill_suppliers(suppliers)
            self._refresh_buttons()

        self._start(lambda _p, _c: list_suppliers(backend, year), done)

    def _on_company_changed(self) -> None:
        if self.backend is not None and self.companies:
            company = self.companies[self.company.current()]
            self.backend.default_context["allowed_company_ids"] = [company.id]
            self._on_year_changed()

    def _save_config(self) -> None:
        self.config.url = self.url.get().strip()
        self.config.database = self.database.get().strip()
        self.config.user = self.user.get().strip()
        self.config.output_dir = self.output_dir.get().strip()
        self.config.include_receipts = self.include_receipts.get()
        self.config.include_other_expenses = self.include_other.get()
        self.config.show_personnel_names = self.show_names.get()
        try:
            self.config.save(self.config_file)
        except OSError as error:
            logger.warning("Configuration not saved: %s", type(error).__name__)

    # ---- Generation ----

    def on_browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.output_dir.get() or str(Path.home()))
        if chosen:
            self.output_dir.set(chosen)

    def _params(self) -> ExtractParams:
        project = self.project.current()
        supplier = self.supplier.current()
        company = self.companies[self.company.current()].id if len(self.companies) > 1 else None
        return ExtractParams(
            year=int(self.year.get()),
            company_id=company,
            analytic_account_id=self.projects[project - 1]["id"] if project > 0 else None,
            supplier_id=self.suppliers[supplier - 1]["id"] if supplier > 0 else None,
            include_receipts=self.include_receipts.get(),
            include_other_expenses=self.include_other.get(),
            show_personnel_names=self.show_names.get(),
        )

    def _options(self, split: bool) -> GenerateOptions:
        return GenerateOptions(
            output_dir=Path(self.output_dir.get().strip()),
            form_overrides=self.config.payment_form_overrides,
            cache_dir=Path(self._cache),
            compress=CompressOptions(self.config.target_dpi, self.config.jpeg_quality),
            split_quarters=split,
            split_threshold_mb=self.config.split_threshold_mb,
        )

    def on_generate(self, split: bool | None = None) -> None:
        if self.backend is None:
            return
        if not self.output_dir.get().strip():
            messagebox.showerror(t("gui.error_title"), t("gui.missing_folder"), parent=self.root)
            return
        self._save_config()
        backend = self.backend
        params = self._params()
        options = self._options(self.split.get() if split is None else split)

        self.last_folder = None
        self.open_button.configure(state="disabled")
        self.cancel_button.configure(state="normal", text=t("gui.cancel"))
        self._write_log([], replace=True)
        self._step = ""
        self._start(
            lambda progress, cancel: generate(backend, params, options, progress, cancel),
            self._on_generated,
            self._on_progress,
        )

    def _on_progress(self, message: str, current: int | None, total: int | None) -> None:
        if message != self._step:
            self._step = message
            self._write_log([message])
        if current is not None and total:
            self.status.configure(
                text=t("gui.step_count", message=message, current=current, total=total)
            )
            self.progress.configure(mode="determinate", maximum=total, value=current)
        else:
            self.status.configure(text=message)
            self.progress.configure(value=0)

    def _finish_running(self) -> None:
        self.cancel_button.configure(state="disabled", text=t("gui.cancel"))
        self.progress.configure(value=0)
        self.status.configure(text=t("gui.ready"))

    def _on_generated(self, result: GenerationResult) -> None:
        self.result = result
        self.last_folder = result.folder
        self._finish_running()
        finished = t("gui.finished")
        if result.notices:
            finished = f"{finished} {t('gui.finished_notices', count=len(result.notices))}"
        self.status.configure(text=finished)
        self._write_log(summary_lines(result), replace=True)
        self.open_button.configure(state="normal")
        self._refresh_buttons()
        if result.oversize:
            self._offer_split(result)

    def _offer_split(self, result: GenerationResult) -> None:
        size = result.volumes[0].path.stat().st_size / 1024 / 1024
        question = t(
            "gui.oversize_question", size=f"{size:.0f}", limit=self.config.split_threshold_mb
        )
        if messagebox.askyesno(t("gui.oversize_title"), question, parent=self.root):
            shutil.rmtree(result.folder, ignore_errors=True)
            self.split.set(True)
            self.on_generate(split=True)

    def on_cancel(self) -> None:
        self.tasks.cancel.set()
        self.cancel_button.configure(state="disabled", text=t("gui.cancelling"))

    def on_open_folder(self) -> None:
        if self.last_folder is not None:
            open_folder(self.last_folder)

    def on_close(self) -> None:
        if self.tasks.busy:
            if not messagebox.askyesno(
                t("gui.close_title"), t("gui.close_question"), parent=self.root
            ):
                return
            self.tasks.cancel.set()
            self.tasks.wait(30)
        self._save_config()
        self.close()

    def close(self) -> None:
        """Remove the downloaded documents, which only live for the session."""
        self.root.after_cancel(self._poll_id)
        shutil.rmtree(self._cache, ignore_errors=True)
        self.root.destroy()
