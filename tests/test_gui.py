import json
import os
import sys
import time
import tkinter as tk
from pathlib import Path

import pytest

from dossier_factures.config import AppConfig
from dossier_factures.gui import app as app_module
from dossier_factures.gui.tasks import TaskRunner
from dossier_factures.model import Notice, NoticeCode
from dossier_factures.odoo_client import (
    InvalidApiKeyError,
    MissingFieldsError,
    NetworkError,
    ReadOnlyViolation,
)
from dossier_factures.pdf_builder import Cancelled
from dossier_factures.pipeline import NothingToGenerate
from dossier_factures.summary import describe_error, notice_lines, notice_text
from gui_scenario import fill_documents

# One Tk per process, like the application.
try:
    _session_root = tk.Tk()
    _session_root.withdraw()
    HAS_DISPLAY = True
except tk.TclError:
    HAS_DISPLAY = False

if not HAS_DISPLAY and os.environ.get("REQUIRE_TK"):
    raise RuntimeError("A display is required here but Tk could not start")

needs_display = pytest.mark.skipif(not HAS_DISPLAY, reason="No display available")


def pump(harness, until, timeout=30):
    """Deliver task results by hand instead of running Tk's event loop.

    With a withdrawn window on macOS, update() may never return. The real event loop is
    exercised by gui_screenshot.py on Windows.
    """
    deadline = time.monotonic() + timeout
    while not until():
        harness.app.tasks.poll()
        harness.root.update_idletasks()
        time.sleep(0.01)
        assert time.monotonic() < deadline, "Timed out waiting for the interface"
    harness.app.tasks.poll()
    harness.root.update_idletasks()


@pytest.fixture
def harness(monkeypatch, backend, scenario, tmp_path):
    """The real window against the fake Odoo, with the vault and dialogs replaced."""
    fill_documents(scenario)
    saved = {}
    dialogs = []
    monkeypatch.setattr(app_module, "get_api_key", lambda *_a: "")
    monkeypatch.setattr(
        app_module, "set_api_key", lambda url, key: saved.update({url: key}) or True
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "showerror",
        lambda title, message, **_k: dialogs.append(("error", message)),
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "askyesno",
        lambda title, message, **_k: dialogs.append(("ask", message)) or True,
    )
    root = tk.Toplevel(_session_root)
    root.withdraw()
    config_file = tmp_path / "config" / "config.json"
    built = []
    factory_calls = []

    def default_factory(url, key, database):
        factory_calls.append((url, database))
        return backend

    def make(config=None, factory=None):
        config = config if config is not None else AppConfig(output_dir=str(tmp_path / "out"))
        app = app_module.App(
            root,
            config=config,
            backend_factory=factory or default_factory,
            config_file=config_file,
        )
        built.append(app)
        harness.app = app
        harness.factory_calls = factory_calls
        return app

    harness = type(
        "H",
        (),
        dict(
            root=root,
            make=make,
            saved=saved,
            dialogs=dialogs,
            backend=backend,
            scenario=scenario,
            config_file=config_file,
            out=tmp_path / "out",
        ),
    )
    yield harness
    if built:
        built[-1].tasks.cancel.set()
        built[-1].tasks.wait(10)
    if root.winfo_exists():
        if built:
            built[-1].close()
        else:
            root.destroy()


def connect(harness, app):
    app.url.set("https://entitat.odoo.com/odoo/action-1")
    app.api_key.set("clau-de-prova")
    app.on_connect()
    pump(harness, lambda: app.connection_status["text"] != "Connectant…")


def generate(harness, app):
    app.result = None
    app.on_generate()
    pump(harness, lambda: app.result is not None or harness.dialogs)


@needs_display
def test_first_run_only_asks_for_the_address_and_the_key(harness):
    app = harness.make()
    assert str(app.generate_button["state"]) == "disabled"
    assert app.connection_form.winfo_manager() == "grid"
    assert app.generation_frame.winfo_manager() == ""
    assert app.connected_bar.winfo_manager() == ""
    assert app.result_frame.winfo_manager() == ""
    assert not hasattr(app, "database")
    assert not hasattr(app, "user")


@needs_display
def test_once_connected_only_the_year_and_the_button_remain(harness):
    app = harness.make()
    connect(harness, app)
    assert app.connection_form.winfo_manager() == ""
    assert app.connected_bar.winfo_manager() == "grid"
    assert app.generation_frame.winfo_manager() == "grid"
    assert app.options_frame.winfo_manager() == ""
    assert app.cancel_button.winfo_manager() == ""
    assert app.result_frame.winfo_manager() == ""
    app.on_toggle_options()
    assert app.options_frame.winfo_manager() == "grid"
    app.on_toggle_options()
    assert app.options_frame.winfo_manager() == ""


@needs_display
def test_changing_the_connection_brings_the_form_back(harness):
    app = harness.make()
    connect(harness, app)
    app.on_change_connection()
    assert app.backend is None
    assert app.connection_form.winfo_manager() == "grid"
    assert app.generation_frame.winfo_manager() == ""
    assert app.api_key.get() == "clau-de-prova"


@needs_display
def test_connection_fills_the_drop_downs_and_saves_only_in_the_vault(harness):
    app = harness.make()
    connect(harness, app)
    assert "Connectat a" in app.connected_label["text"]
    assert str(app.generate_button["state"]) == "normal"
    assert harness.factory_calls == [("https://entitat.odoo.com", "")]
    assert app.year.get() == "2099"
    assert app.project["values"][0] == "(Tots)"
    assert "[P7] Projecte Set" in app.project["values"]
    assert list(app.supplier["values"]) == ["(Tots)", "Proveïdor Àlfa", "Proveïdor Beta"]
    assert harness.saved == {"https://entitat.odoo.com": "clau-de-prova"}
    text = harness.config_file.read_text(encoding="utf-8")
    assert "clau-de-prova" not in text
    assert json.loads(text)["url"] == "https://entitat.odoo.com/odoo/action-1"


@needs_display
def test_several_companies_show_a_selector_only_then(harness, scenario):
    app = harness.make()
    connect(harness, app)
    assert len(app.companies) == 2
    assert app.company.winfo_manager() == "grid"
    scenario["res.company"].pop()
    connect(harness, app)
    assert app.company.winfo_manager() == ""


@needs_display
def test_database_can_be_given_in_the_address(harness):
    app = harness.make()
    app.url.set("https://servidor.example.invalid/?db=entitat")
    app.api_key.set("clau-de-prova")
    app.on_connect()
    pump(harness, lambda: app.backend is not None)
    assert harness.factory_calls == [("https://servidor.example.invalid", "entitat")]
    assert json.loads(harness.config_file.read_text(encoding="utf-8"))["url"].endswith(
        "?db=entitat"
    )


@needs_display
def test_missing_key_is_said_without_calling_odoo(harness):
    app = harness.make()
    app.url.set("https://entitat.odoo.com")
    app.on_connect()
    assert app.connection_status["text"] == "Escriviu la clau d'API."
    assert harness.backend.calls == []


@needs_display
def test_wrong_key_shows_a_clear_message(harness):
    def rejected(url, key, database):
        backend = harness.backend

        def server_version():
            raise InvalidApiKeyError("401")

        backend.server_version = server_version
        return backend

    app = harness.make(factory=rejected)
    connect(harness, app)
    assert "caducat" in app.connection_status["text"]
    assert str(app.generate_button["state"]) == "disabled"
    assert harness.saved == {}
    assert app.api_key.get() == ""


@needs_display
def test_invalid_url_shows_a_clear_message(harness):
    app = harness.make()
    app.url.set("http://entitat.odoo.com")
    app.api_key.set("x")
    app.on_connect()
    assert "https://" in app.connection_status["text"]


@needs_display
def test_saved_connection_connects_on_start(harness, monkeypatch):
    monkeypatch.setattr(app_module, "get_api_key", lambda *_a: "desada")
    app = harness.make(AppConfig(url="https://entitat.odoo.com", output_dir=str(harness.out)))
    pump(harness, lambda: app.backend is not None)
    pump(harness, lambda: not app.tasks.busy)
    assert app.api_key.get() == "desada"
    assert str(app.generate_button["state"]) == "normal"
    assert app.connection_form.winfo_manager() == ""


@needs_display
def test_expired_key_on_start_asks_only_for_a_new_key(harness, monkeypatch):
    monkeypatch.setattr(app_module, "get_api_key", lambda *_a: "caducada")

    def expired(url, key, database):
        def server_version():
            raise InvalidApiKeyError("401")

        harness.backend.server_version = server_version
        return harness.backend

    app = harness.make(
        AppConfig(url="https://entitat.odoo.com", output_dir=str(harness.out)), factory=expired
    )
    pump(harness, lambda: not app.tasks.busy and "caducat" in app.connection_status["text"])
    assert app.connection_form.winfo_manager() == "grid"
    assert app.url.get() == "https://entitat.odoo.com"
    assert app.api_key.get() == ""


@needs_display
def test_selection_is_remembered_for_the_next_time(harness, monkeypatch):
    app = harness.make()
    connect(harness, app)
    app.project.current(1)
    app.supplier.current(1)
    app.split.set(True)
    app.show_names.set(True)
    app.include_receipts.set(False)
    app.output_dir.set(str(harness.out / "triada"))
    project_id = app.projects[0]["id"]
    app.on_close()

    saved = AppConfig.load(harness.config_file)
    assert saved.url == "https://entitat.odoo.com/odoo/action-1"
    assert saved.project_id == project_id
    assert saved.year == 2099
    assert saved.split_quarters is True
    assert saved.show_personnel_names is True
    assert saved.include_receipts is False
    assert saved.output_dir.endswith("triada")

    monkeypatch.setattr(app_module, "get_api_key", lambda url: "desada")
    harness.root = tk.Toplevel(_session_root)
    harness.root.withdraw()
    again = app_module.App(
        harness.root,
        config=saved,
        backend_factory=lambda *_a: harness.backend,
        config_file=harness.config_file,
    )
    harness.app = again
    pump(harness, lambda: again.backend is not None and not again.tasks.busy)
    assert again.url.get() == saved.url
    assert again.api_key.get() == "desada"
    assert again.projects[again.project.current() - 1]["id"] == project_id
    assert again.split.get() is True
    assert again.output_dir.get().endswith("triada")
    # A forgotten supplier filter would leave an incomplete dossier.
    assert again.supplier.current() == 0
    # And what is remembered and reduces the dossier is said where it can't be missed.
    assert again.filter_note.winfo_manager() == "grid"
    assert "només el projecte" in again.filter_note["text"]
    assert "sense els rebuts" in again.filter_note["text"]
    assert again.options_frame.winfo_manager() == ""
    again.tasks.wait(10)
    again.close()


@needs_display
def test_closing_without_connection_keeps_the_saved_selection(harness):
    config = AppConfig(output_dir=str(harness.out), project_id=7, year=2099, year_saved_on=2099)
    app = harness.make(config)
    app.on_close()
    saved = AppConfig.load(harness.config_file)
    assert (saved.project_id, saved.year, saved.year_saved_on) == (7, 2099, 2099)


@needs_display
def test_no_note_without_a_filter(harness):
    app = harness.make()
    connect(harness, app)
    assert app.filter_note.winfo_manager() == ""
    app.supplier.current(1)
    app._refresh_filter_note()
    assert "només el proveïdor" in app.filter_note["text"]
    app.supplier.current(0)
    app._refresh_filter_note()
    assert app.filter_note.winfo_manager() == ""


@needs_display
def test_paste_from_the_menu_replaces_the_box_and_trims(harness):
    app = harness.make()
    app.api_key.set("antiga")
    harness.root.clipboard_clear()
    harness.root.clipboard_append("  clau-nova\n")
    app._paste_into(app.key_entry)
    assert app.api_key.get() == "clau-nova"
    assert app.key_entry.bind("<Button-3>")


@pytest.mark.parametrize(
    ("saved_year", "saved_on", "today", "expected"),
    [
        (None, None, 2027, 2026),  # first time: the year just closed
        (2027, 2027, 2027, 2027),  # chosen this year: respected
        (2026, 2027, 2027, 2026),
        (2025, 2026, 2027, 2026),  # chosen last year: stale, back to the year just closed
        (2020, 2027, 2027, 2026),  # no longer on the list
    ],
)
def test_year_proposal_follows_the_calendar(saved_year, saved_on, today, expected):
    years = [2027, 2026, 2025]
    assert app_module.choose_year(years, 2027, saved_year, saved_on, today) == expected


def test_year_proposal_without_the_year_just_closed():
    assert app_module.choose_year([2026], 2026, None, None, 2026) == 2026
    assert app_module.choose_year([2099, 2026], 2099, None, None, 2026) == 2099
    assert app_module.choose_year([2026], None, None, None, 2026) == 2026


@needs_display
def test_full_generation_writes_files_and_shows_the_summary(harness):
    app = harness.make()
    connect(harness, app)
    generate(harness, app)
    assert harness.dialogs == []
    folder = app.result.folder
    names = {p.name for p in folder.iterdir()}
    assert names == {
        "Dossier de factures de proveïdors 2099.pdf",
        "Registre de factures de proveïdors 2099.xlsx",
    }
    log = app.log.get("1.0", "end")
    assert "Factures i rebuts al dossier: 13 (1.662,50 €)" in log
    assert "Altres despeses sense document, només a l'Excel: 4 (1.282,00 €)" in log
    assert "Total del registre: 2.944,50 €" in log
    assert "Pàgines del dossier:" in log
    assert "Dossier de factures de proveïdors 2099.pdf" in log
    assert "Avisos que cal revisar" in log
    assert "Factura sense document adjunt" in log
    assert "Possibles assentaments duplicats" in log
    assert str(app.open_button["state"]) == "normal"
    assert str(app.generate_button["state"]) == "normal"
    assert app.status["text"].startswith("Dossier generat. Hi ha ")
    assert "avisos" in app.status["text"]


@needs_display
def test_interface_never_calls_anything_but_read_methods(harness):
    app = harness.make()
    connect(harness, app)
    generate(harness, app)
    assert {c[1] for c in harness.backend.calls} <= {
        "search_read",
        "read",
        "fields_get",
        "search_count",
        "context_get",
    }


@needs_display
def test_filters_reach_the_generation(harness):
    app = harness.make()
    connect(harness, app)
    app.supplier.current(2)
    generate(harness, app)
    assert [i.name for i in app.result.dossier.invoices] == ["FC/2099/03/0001"]


@needs_display
def test_quarter_option_produces_one_pdf_per_quarter(harness):
    app = harness.make()
    connect(harness, app)
    app.split.set(True)
    generate(harness, app)
    assert len(app.result.volumes) == 3
    assert len(list(app.result.folder.glob("*.xlsx"))) == 1


@needs_display
def test_checkboxes_change_the_scope(harness):
    app = harness.make()
    connect(harness, app)
    app.include_receipts.set(False)
    app.include_other.set(False)
    generate(harness, app)
    assert {i.move_type for i in app.result.dossier.invoices} == {"in_invoice", "in_refund"}


@needs_display
def test_cancel_stops_and_leaves_nothing(harness):
    app = harness.make()
    connect(harness, app)
    original = harness.backend._execute

    def slow(model, method, kwargs):
        if "raw" in kwargs.get("fields", []):
            time.sleep(0.05)
        return original(model, method, kwargs)

    harness.backend._execute = slow
    app.on_generate()
    assert str(app.cancel_button["state"]) == "normal"
    app.on_cancel()
    pump(harness, lambda: harness.dialogs)
    assert harness.dialogs == [("error", "S'ha cancel·lat. No s'ha generat cap fitxer.")]
    assert not harness.out.exists() or list(harness.out.iterdir()) == []
    assert str(app.generate_button["state"]) == "normal"
    assert app.cancel_button.winfo_manager() == ""
    assert app.result_frame.winfo_manager() == ""


@needs_display
def test_year_without_invoices_says_so(harness):
    app = harness.make()
    connect(harness, app)
    app.year.set(str(1990))
    app.include_other.set(False)
    app.on_generate()
    pump(harness, lambda: harness.dialogs)
    assert harness.dialogs[0][1].startswith("No hi ha cap factura")


@needs_display
def test_oversize_pdf_offers_to_split_and_replaces_the_first_result(harness):
    app = harness.make(AppConfig(output_dir=str(harness.out), split_threshold_mb=0))
    connect(harness, app)
    app.on_generate()
    pump(harness, lambda: harness.dialogs and app.tasks.busy)
    assert harness.dialogs[0][0] == "ask"
    pump(harness, lambda: not app.tasks.busy and app.result and len(app.result.volumes) > 1)
    assert len(list(harness.out.iterdir())) == 1
    assert app.split.get() is True


@needs_display
def test_downloads_are_removed_on_close(harness):
    app = harness.make()
    connect(harness, app)
    generate(harness, app)
    cache = Path(app._cache)
    assert list(cache.iterdir())
    app.close()
    assert not cache.exists()


def test_task_runner_delivers_results_on_poll():
    runner = TaskRunner()
    seen = []
    runner.start(
        lambda progress, _cancel: (progress("pas", 1, 2), "fet")[1],
        lambda result: seen.append(("done", result)),
        lambda error: seen.append(("error", error)),
        lambda *args: seen.append(("progress", args)),
    )
    runner.wait(5)
    assert seen == []
    runner.poll()
    assert seen == [("progress", ("pas", 1, 2)), ("done", "fet")]
    assert not runner.busy


def test_task_runner_reports_errors_and_passes_cancel():
    runner = TaskRunner()
    seen = []

    def work(_progress, cancel):
        assert cancel is runner.cancel
        raise ValueError("x")

    runner.start(work, seen.append, seen.append)
    runner.wait(5)
    runner.poll()
    assert isinstance(seen[0], ValueError)


@pytest.mark.parametrize(
    ("error", "text"),
    [
        (InvalidApiKeyError("401 secret detail"), "caducat"),
        (NetworkError(), "connexió"),
        (MissingFieldsError({"a": ["b"]}), "actualitzar"),
        (ReadOnlyViolation("write"), "només pot llegir"),
        (Cancelled(), "cancel·lat"),
        (NothingToGenerate(), "cap factura"),
        (PermissionError("denied"), "carpeta de sortida"),
        (ValueError("internal"), "inesperat"),
    ],
)
def test_errors_are_described_without_technical_details(error, text):
    message = describe_error(error)
    assert text in message
    assert "secret detail" not in message
    assert "internal" not in message


def test_notice_text_and_groups_follow_importance():
    notices = [
        Notice(NoticeCode.MISSING_VAT, "A/1", {"supplier": "Nom"}),
        Notice(NoticeCode.DOCUMENT_UNREADABLE, "A/2", {"name": "f.pdf"}),
        Notice(NoticeCode.MISSING_VAT, "A/3", {"supplier": "Altre"}),
    ]
    assert notice_text(notices[0]) == "A/1: El proveïdor Nom no té NIF"
    lines = notice_lines(notices)
    assert lines[0] == "Avisos que cal revisar: 3"
    assert lines.index("Documents que no s'han pogut llegir (1)") < lines.index(
        "Proveïdors sense NIF (2)"
    )
    assert notice_lines([]) == ["Cap avís."]


def test_every_notice_code_has_a_title_and_a_place_in_the_summary():
    from dossier_factures.i18n import t
    from dossier_factures.summary import NOTICE_ORDER

    assert set(NOTICE_ORDER) == set(NoticeCode)
    for code in NoticeCode:
        assert t(f"notice_title.{code}") != f"notice_title.{code}"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows credential vault")
def test_key_is_really_stored_in_the_windows_vault():
    import keyring

    from dossier_factures import credentials

    assert "Windows" in type(keyring.get_keyring()).__module__
    url = "https://prova-ci.invalid"
    try:
        assert credentials.set_api_key(url, "clau-de-prova")
        assert credentials.get_api_key(url) == "clau-de-prova"
        assert keyring.get_password(credentials.APP_NAME, url) == "clau-de-prova"
    finally:
        credentials.delete_api_key(url)
    assert keyring.get_password(credentials.APP_NAME, url) is None


def test_notice_amounts_and_dates_read_like_the_rest_of_the_application(backend):
    from dossier_factures.extract import extract_dossier
    from dossier_factures.model import ExtractParams

    dossier = extract_dossier(backend, ExtractParams(2099))
    notice = next(n for n in dossier.notices if n.code is NoticeCode.UNRECONCILED_PAYMENT)
    assert notice_text(notice) == (
        "FC/2099/04/0001: Té un pagament actiu sense conciliar amb la factura: "
        "PAG/7 del 03/06/2099, 80,00 €"
    )
