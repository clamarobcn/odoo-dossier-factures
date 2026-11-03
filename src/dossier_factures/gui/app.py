"""The main window. All the work is delegated: this module only deals with the interface.

The people using it are not used to computers, so it shows only what the next step needs:
the address and key until connected, then the year and one button.
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
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
from ..odoo_client import (
    InvalidApiKeyError,
    Json2Backend,
    OdooBackend,
    OdooError,
    database_from_url,
    normalize_base_url,
)
from ..pipeline import GenerateOptions, GenerationResult, generate
from ..summary import describe_error, summary_lines
from .tasks import TaskRunner

logger = logging.getLogger(__name__)

POLL_MS = 80
WINDOW_WIDTH = 660
MIN_WINDOW_WIDTH = 560
WRAP_WIDTH = 600
LOG_LINES = 14
FONT_SIZE = 11
PADDING = 10
OK_COLOR = "#1E7B34"
ERROR_COLOR = "#B3261E"
WARNING_COLOR = "#8A5000"
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


def default_folder() -> Path:
    documents = Path.home() / "Documents"
    return documents if documents.is_dir() else Path.home()


def choose_year(
    years: list[int],
    latest: int | None,
    saved_year: int | None,
    saved_on: int | None,
    today_year: int,
) -> int:
    """A choice only holds during the calendar year it was made, so it can't go stale unseen.

    After that the proposal is the year just closed, which is the one a dossier justifies, even
    if the first invoices of the new year are already in Odoo.
    """
    if saved_on == today_year and saved_year in years:
        return saved_year
    if today_year - 1 in years:
        return today_year - 1
    return latest if latest in years else years[0]


def _index_of(items: list, saved_id: int | None) -> int | None:
    return next((i for i, item in enumerate(items) if _record_id(item) == saved_id), None)


def _record_id(item: Any) -> int:
    return item["id"] if isinstance(item, dict) else item.id


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
        self.options_open = False
        self._cache = tempfile.mkdtemp(prefix="dossier-")

        root.title(f"{t('gui.title')} {__version__}")
        root.minsize(MIN_WINDOW_WIDTH, 200)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        self._apply_style()
        self._build_connection()
        self._build_generation()
        self._build_result()
        self._refresh_buttons()
        self._schedule_poll()
        self._connect_on_start()

    # ---- Layout ----

    def _apply_style(self) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            font = tkfont.nametofont(name, root=self.root)
            font.configure(size=max(font.actual("size"), FONT_SIZE))
        self._bold_font = tkfont.nametofont("TkDefaultFont", root=self.root).copy()
        self._bold_font.configure(weight="bold")
        ttk.Style(self.root).configure("Primary.TButton", font=self._bold_font, padding=(18, 8))

    def _build_connection(self) -> None:
        self.url = tk.StringVar(value=self.config.url)
        self.api_key = tk.StringVar(value=self._saved_key())

        form = ttk.Frame(self.root, padding=PADDING)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        self.connection_form = form

        ttk.Label(form, text=t("gui.url")).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        self.url_entry = ttk.Entry(form, textvariable=self.url)
        self.url_entry.grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(form, text=t("gui.api_key")).grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=3
        )
        self.key_entry = ttk.Entry(form, textvariable=self.api_key, show="•")
        self.key_entry.grid(row=1, column=1, sticky="ew", pady=3)
        for entry in (self.url_entry, self.key_entry):
            self._add_paste_menu(entry)
            entry.bind("<Return>", lambda _event: self.on_connect())

        ttk.Label(
            form, text=t("gui.api_key_hint"), foreground=MUTED_COLOR, wraplength=WRAP_WIDTH
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 6))
        self.connect_button = ttk.Button(
            form, text=t("gui.test"), command=self.on_connect, style="Primary.TButton"
        )
        self.connect_button.grid(row=3, column=0, columnspan=2, sticky="w")
        self.connection_status = ttk.Label(form, text="", wraplength=WRAP_WIDTH, justify="left")
        self.connection_status.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        bar = ttk.Frame(self.root, padding=(PADDING, PADDING, PADDING, 0))
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        self.connected_bar = bar
        self.connected_label = ttk.Label(bar, text="", wraplength=WRAP_WIDTH - 100, justify="left")
        self.connected_label.grid(row=0, column=0, sticky="w")
        self.change_button = ttk.Button(
            bar, text=t("gui.change_connection"), command=self.on_change_connection
        )
        self.change_button.grid(row=0, column=1, sticky="e")
        bar.grid_remove()

    def _add_paste_menu(self, entry: ttk.Entry) -> None:
        """The key has to be pasted, and many people only know how to do it with the mouse."""
        menu = tk.Menu(entry, tearoff=0)
        menu.add_command(label=t("gui.paste"), command=lambda: self._paste_into(entry))
        for button in ("<Button-3>", "<Button-2>"):
            entry.bind(button, lambda event: menu.tk_popup(event.x_root, event.y_root))

    def _paste_into(self, entry: ttk.Entry) -> None:
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        entry.delete(0, "end")
        entry.insert(0, text.strip())

    def _build_generation(self) -> None:
        frame = ttk.Frame(self.root, padding=PADDING)
        frame.grid(row=1, column=0, sticky="ew")
        frame.columnconfigure(1, weight=1)
        self.generation_frame = frame

        self.company_label = ttk.Label(frame, text=t("gui.company"))
        self.company = ttk.Combobox(frame, state="readonly")
        self.company_label.grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        self.company.grid(row=0, column=1, sticky="ew", pady=3)
        self.company.bind("<<ComboboxSelected>>", lambda _event: self._on_company_changed())
        self.company_label.grid_remove()
        self.company.grid_remove()

        ttk.Label(frame, text=t("gui.year")).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        self.year = ttk.Combobox(frame, state="readonly", width=8)
        self.year.grid(row=1, column=1, sticky="w", pady=3)
        self.year.bind("<<ComboboxSelected>>", lambda _event: self._on_year_changed())

        self.filter_note = ttk.Label(
            frame, text="", foreground=WARNING_COLOR, wraplength=WRAP_WIDTH, justify="left"
        )
        self.filter_note.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.filter_note.grid_remove()

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        buttons.columnconfigure(2, weight=1)
        self.generate_button = ttk.Button(
            buttons, text=t("gui.generate"), command=self.on_generate, style="Primary.TButton"
        )
        self.generate_button.grid(row=0, column=0, padx=(0, 8))
        self.cancel_button = ttk.Button(buttons, text=t("gui.cancel"), command=self.on_cancel)
        self.cancel_button.grid(row=0, column=1)
        self.cancel_button.grid_remove()
        self.options_button = ttk.Button(
            buttons, text=t("gui.more_options"), command=self.on_toggle_options
        )
        self.options_button.grid(row=0, column=3, sticky="e")

        self._build_options(frame)
        frame.grid_remove()

    def _build_options(self, parent: ttk.Frame) -> None:
        options = ttk.Frame(parent)
        options.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        options.columnconfigure(1, weight=1)
        self.options_frame = options

        ttk.Label(options, text=t("gui.project")).grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=3
        )
        self.project = ttk.Combobox(options, state="readonly")
        self.project.grid(row=0, column=1, columnspan=2, sticky="ew", pady=3)
        ttk.Label(options, text=t("gui.supplier")).grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=3
        )
        self.supplier = ttk.Combobox(options, state="readonly")
        self.supplier.grid(row=1, column=1, columnspan=2, sticky="ew", pady=3)
        for combo in (self.project, self.supplier):
            combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_filter_note())

        self.split = tk.BooleanVar(value=self.config.split_quarters)
        self.include_receipts = tk.BooleanVar(value=self.config.include_receipts)
        self.include_other = tk.BooleanVar(value=self.config.include_other_expenses)
        self.show_names = tk.BooleanVar(value=self.config.show_personnel_names)
        row = 2
        for variable, key in (
            (self.split, "gui.quarterly_pdf"),
            (self.include_receipts, "gui.include_receipts"),
            (self.include_other, "gui.include_other_expenses"),
            (self.show_names, "gui.show_personnel_names"),
        ):
            ttk.Checkbutton(options, text=t(key), variable=variable).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=1
            )
            row += 1
        for variable in (self.include_receipts, self.include_other):
            variable.trace_add("write", lambda *_: self._refresh_filter_note())

        ttk.Label(options, text=t("gui.output_folder")).grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=(6, 3)
        )
        self.output_dir = tk.StringVar(value=self.config.output_dir or str(default_folder()))
        ttk.Entry(options, textvariable=self.output_dir).grid(
            row=row, column=1, sticky="ew", padx=(0, 6), pady=(6, 3)
        )
        ttk.Button(options, text=t("gui.browse"), command=self.on_browse).grid(
            row=row, column=2, pady=(6, 3)
        )
        options.grid_remove()

    def _build_result(self) -> None:
        frame = ttk.Frame(self.root, padding=(PADDING, 0, PADDING, PADDING))
        frame.grid(row=2, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        self.result_frame = frame

        self.status = ttk.Label(frame, text="", wraplength=WRAP_WIDTH, justify="left")
        self.status.grid(row=0, column=0, columnspan=2, sticky="w")
        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)
        self.open_button = ttk.Button(
            frame, text=t("gui.open_folder"), command=self.on_open_folder, style="Primary.TButton"
        )
        self.open_button.grid(row=2, column=0, columnspan=2, sticky="w", pady=6)

        self.log = tk.Text(
            frame,
            height=LOG_LINES,
            wrap="word",
            state="disabled",
            relief="flat",
            font="TkDefaultFont",
        )
        self.log.grid(row=3, column=0, sticky="nsew")
        self.log_scrollbar = ttk.Scrollbar(frame, command=self.log.yview)
        self.log_scrollbar.grid(row=3, column=1, sticky="ns")
        self.log.configure(yscrollcommand=self.log_scrollbar.set)
        frame.grid_remove()
        self._show_result_parts(running=False)

    def _show_result_parts(self, running: bool) -> None:
        if running:
            self.progress.grid()
        else:
            self.progress.grid_remove()
        for widget in (self.open_button, self.log, self.log_scrollbar):
            if running:
                widget.grid_remove()
            else:
                widget.grid()

    def _fit_window(self) -> None:
        """Follow what is on show. On a small screen the summary gives up its height."""
        self.root.update_idletasks()
        # Taskbar and title bar take about 100 px: the buttons must stay visible on a laptop.
        height = min(self.root.winfo_reqheight(), self.root.winfo_screenheight() - 100)
        width = max(self.root.winfo_width(), WINDOW_WIDTH)
        self.root.geometry(f"{width}x{height}")

    # ---- Small helpers ----

    def _schedule_poll(self) -> None:
        self.tasks.poll()
        self._poll_id = self.root.after(POLL_MS, self._schedule_poll)

    def _saved_key(self) -> str:
        if not self.config.url:
            return ""
        try:
            return get_api_key(normalize_base_url(self.config.url))
        except OdooError:
            return ""

    def _show_connection_form(self, message: str = "", color: str = MUTED_COLOR) -> None:
        self.backend = None
        self.connected_bar.grid_remove()
        self.generation_frame.grid_remove()
        self.result_frame.grid_remove()
        self.connection_form.grid()
        self._set_status(self.connection_status, message, color)
        self._refresh_buttons()
        self._fit_window()

    def _show_connected(self, message: str, color: str) -> None:
        self.connection_form.grid_remove()
        self.connected_bar.grid()
        self.generation_frame.grid()
        self._set_status(self.connected_label, message, color)
        self._set_status(self.connection_status, "", MUTED_COLOR)
        self._refresh_buttons()
        self._fit_window()

    def _refresh_buttons(self) -> None:
        busy = self.tasks.busy
        ready = self.backend is not None and not busy
        self.generate_button.configure(state="normal" if ready else "disabled")
        self.connect_button.configure(state="disabled" if busy else "normal")
        self.change_button.configure(state="disabled" if busy else "normal")

    @staticmethod
    def _set_status(label: ttk.Label, text: str, color: str) -> None:
        label.configure(text=text, foreground=color)

    def _write_log(self, lines: list[str]) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(lines) + "\n")
        self.log.see("1.0")
        self.log.configure(state="disabled")

    def _start(self, work, on_done, on_progress=lambda *_: None) -> None:
        self.tasks.start(work, on_done, self._on_task_error, on_progress)
        self._refresh_buttons()

    def _on_task_error(self, error: BaseException) -> None:
        """The default handler for a failed task."""
        logger.error("Task failed: %s", type(error).__name__, exc_info=error)
        self._finish_running()
        self.result_frame.grid_remove()
        self._refresh_buttons()
        self._fit_window()
        messagebox.showerror(t("gui.error_title"), describe_error(error), parent=self.root)

    # ---- Connection ----

    def _connect_on_start(self) -> None:
        """With everything saved from last time, the secretary only has to press Generate."""
        if self.url.get() and self.api_key.get():
            self.connection_form.grid_remove()
            self.connected_bar.grid()
            self._set_status(self.connected_label, t("gui.testing"), MUTED_COLOR)
            self.on_connect()
        self._fit_window()

    def on_connect(self) -> None:
        if self.tasks.busy:
            return
        address, key = self.url.get().strip(), self.api_key.get().strip()
        try:
            url = normalize_base_url(address)
        except OdooError as error:
            self._set_status(self.connection_status, describe_error(error), ERROR_COLOR)
            return
        if not key:
            self._set_status(self.connection_status, t("gui.missing_key"), ERROR_COLOR)
            return
        backend = self.backend_factory(url, key, database_from_url(address))
        self._set_status(self.connection_status, t("gui.testing"), MUTED_COLOR)
        saved_company = self.config.company_id

        def work(_progress, _cancel) -> _Connection:
            info = check_connection(backend)
            if len(info.companies) > 1:
                company = info.companies[_index_of(info.companies, saved_company) or 0]
                backend.default_context["allowed_company_ids"] = [company.id]
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
            self._on_connected(backend, url, key, connection)

        def failed(error: BaseException) -> None:
            logger.warning("Connection failed: %s", type(error).__name__)
            if isinstance(error, InvalidApiKeyError):
                # An expired key is invisible behind the dots: leave the box ready for the new one.
                self.api_key.set("")
            self._show_connection_form(describe_error(error), ERROR_COLOR)

        self.tasks.start(work, done, failed)
        self._refresh_buttons()

    def _on_connected(
        self, backend: OdooBackend, url: str, key: str, connection: _Connection
    ) -> None:
        self.backend = backend
        info = connection.info
        logger.info("Connected to Odoo %s", info.version)
        names = ", ".join(c.name for c in info.companies)
        message = t("gui.connected", company=names)
        color = OK_COLOR
        if not set_api_key(url, key):
            message = f"{message}\n{t('gui.key_not_saved')}"
            color = ERROR_COLOR

        self.companies = info.companies
        if len(self.companies) > 1:
            self.company.configure(values=[c.name for c in self.companies])
            self.company.current(_index_of(self.companies, self.config.company_id) or 0)
            self.company_label.grid()
            self.company.grid()
        else:
            self.company_label.grid_remove()
            self.company.grid_remove()

        self.years = connection.years
        self.year.configure(values=[str(y) for y in self.years])
        selected = choose_year(
            self.years,
            connection.latest_year,
            self.config.year,
            self.config.year_saved_on,
            date.today().year,
        )
        self.year.set(str(selected))

        self.projects = connection.projects
        self.project.configure(values=[t("gui.all"), *[p["name"] for p in self.projects]])
        saved_project = _index_of(self.projects, self.config.project_id)
        self.project.current(0 if saved_project is None else saved_project + 1)
        self._fill_suppliers(connection.suppliers)

        self._save_config()
        self._show_connected(message, color)
        # The suppliers came from the newest year: bring them in line with the one on show.
        if selected != connection.latest_year:
            self._on_year_changed()

    def on_change_connection(self) -> None:
        self._show_connection_form()

    def _fill_suppliers(self, suppliers: list[dict[str, Any]]) -> None:
        self.suppliers = suppliers
        self.supplier.configure(values=[t("gui.all"), *[s["name"] for s in suppliers]])
        self.supplier.current(0)
        self._refresh_filter_note()

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

    def _refresh_filter_note(self) -> None:
        """A remembered filter must never reduce the dossier without being seen."""
        details = []
        if self.project.current() > 0:
            name = self.projects[self.project.current() - 1]["name"]
            details.append(t("gui.filter_project", name=name))
        if self.supplier.current() > 0:
            name = self.suppliers[self.supplier.current() - 1]["name"]
            details.append(t("gui.filter_supplier", name=name))
        if not self.include_receipts.get():
            details.append(t("gui.filter_no_receipts"))
        if not self.include_other.get():
            details.append(t("gui.filter_no_other"))
        shown = bool(self.filter_note.winfo_manager())
        if details:
            self.filter_note.configure(text=t("gui.filter_note", details=", ".join(details)))
            self.filter_note.grid()
        else:
            self.filter_note.grid_remove()
        if shown != bool(details):
            self._fit_window()

    def on_toggle_options(self) -> None:
        self.options_open = not self.options_open
        if self.options_open:
            self.options_frame.grid()
            self.options_button.configure(text=t("gui.fewer_options"))
        else:
            self.options_frame.grid_remove()
            self.options_button.configure(text=t("gui.more_options"))
        self._fit_window()

    def _save_config(self) -> None:
        self.config.url = self.url.get().strip()
        self.config.output_dir = self.output_dir.get().strip()
        self.config.split_quarters = self.split.get()
        self.config.include_receipts = self.include_receipts.get()
        self.config.include_other_expenses = self.include_other.get()
        self.config.show_personnel_names = self.show_names.get()
        if self.backend is not None:
            # The drop-downs are only filled once connected: otherwise keep the saved selection.
            project = self.project.current()
            self.config.project_id = self.projects[project - 1]["id"] if project > 0 else None
            self.config.company_id = self._selected_company()
            self.config.year = int(self.year.get())
            self.config.year_saved_on = date.today().year
        try:
            self.config.save(self.config_file)
        except OSError as error:
            logger.warning("Configuration not saved: %s", type(error).__name__)

    # ---- Generation ----

    def on_browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.output_dir.get() or str(default_folder()))
        if chosen:
            self.output_dir.set(chosen)

    def _selected_company(self) -> int | None:
        return self.companies[self.company.current()].id if len(self.companies) > 1 else None

    def _params(self) -> ExtractParams:
        project = self.project.current()
        supplier = self.supplier.current()
        return ExtractParams(
            year=int(self.year.get()),
            company_id=self._selected_company(),
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
        self.cancel_button.configure(state="normal", text=t("gui.cancel"))
        self.cancel_button.grid()
        self.status.configure(text="")
        self.result_frame.grid()
        self._show_result_parts(running=True)
        self._fit_window()
        self._start(
            lambda progress, cancel: generate(backend, params, options, progress, cancel),
            self._on_generated,
            self._on_progress,
        )

    def _on_progress(self, message: str, current: int | None, total: int | None) -> None:
        if current is not None and total:
            self.status.configure(
                text=t("gui.step_count", message=message, current=current, total=total)
            )
            self.progress.configure(mode="determinate", maximum=total, value=current)
        else:
            self.status.configure(text=message)
            self.progress.configure(value=0)

    def _finish_running(self) -> None:
        self.cancel_button.grid_remove()
        self.progress.configure(value=0)

    def _on_generated(self, result: GenerationResult) -> None:
        self.result = result
        self.last_folder = result.folder
        self._finish_running()
        finished = t("gui.finished")
        if result.notices:
            finished = f"{finished} {t('gui.finished_notices', count=len(result.notices))}"
        self.status.configure(text=finished)
        self._write_log(summary_lines(result))
        self._show_result_parts(running=False)
        self._refresh_buttons()
        self._fit_window()
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
