"""Drive the packaged programs against a simulated Odoo, as a person would. Windows only."""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import keyring
import openpyxl
import pikepdf
from PIL import ImageGrab

sys.path.insert(0, str(Path(__file__).parent))

from conftest import FIXTURES, FakeBackend
from dossier_factures import __version__
from dossier_factures.config import APP_NAME
from dossier_factures.i18n import t
from dossier_factures.odoo_client import READ_ONLY_METHODS
from gui_scenario import fill_documents

KEY = "clau-de-prova-executable"
EXPECTED_PAGES = 15
# The middle of "Generar el dossier" with this scenario, which has two companies.
GENERATE_BUTTON = (95, 146)
WM_CLOSE = 0x0010
MOUSE_DOWN, MOUSE_UP = 0x0002, 0x0004


class FakeOdoo(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        tables = json.loads((FIXTURES / "odoo_scenario.json").read_text(encoding="utf-8"))
        self.backend = FakeBackend(fill_documents(tables))
        self.methods: set[str] = set()
        self.last_call = 0.0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def _reply(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._reply(200, self.server.backend.server_version())

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.headers.get("Authorization") != f"bearer {KEY}":
            self._reply(401, {"name": "AccessDenied", "message": "bad key"})
            return
        _, _, _, model, method = self.path.split("/")
        self.server.methods.add(method)
        self.server.last_call = time.monotonic()
        self._reply(200, self.server.backend._execute(model, method, body))


def check_documents(folder: Path) -> None:
    pdfs, sheets = list(folder.rglob("*.pdf")), list(folder.rglob("*.xlsx"))
    assert len(pdfs) == 1, pdfs
    assert len(sheets) == 1, sheets
    with pikepdf.open(pdfs[0]) as pdf:
        assert len(pdf.pages) == EXPECTED_PAGES, len(pdf.pages)
    sheet = openpyxl.load_workbook(sheets[0]).active
    values = {str(cell.value) for row in sheet.iter_rows() for cell in row}
    assert any("Total" in value for value in values)
    print("documents ok:", pdfs[0].name, sheets[0].name)


def run_cli(exe: Path, server: FakeOdoo, work: Path) -> None:
    env = {**os.environ, "ODOO_API_KEY": KEY, "APPDATA": str(work / "appdata")}
    version = subprocess.run([exe, "--version"], capture_output=True, text=True, check=True)
    assert version.stdout.strip() == __version__, version.stdout
    out = work / "out"
    started = time.monotonic()
    done = subprocess.run(
        [exe, "--url", server.url, "generate", "--year", "2099", "--out", out],
        capture_output=True,
        encoding="utf-8",
        env=env,
        timeout=300,
    )
    print(done.stdout[-1500:], done.stderr[-1500:])
    assert done.returncode == 0, done.returncode
    print(f"command line generated in {time.monotonic() - started:.0f} s")
    check_documents(out)


def check_https(exe: Path, work: Path) -> None:
    """The simulated Odoo is plain HTTP, so try the certificates on a site that isn't Odoo.

    Getting as far as "no API here" means the secure connection itself worked.
    """
    env = {**os.environ, "ODOO_API_KEY": KEY, "APPDATA": str(work / "appdata")}
    done = subprocess.run(
        [exe, "--url", "https://github.com", "test"],
        capture_output=True,
        encoding="utf-8",
        env=env,
        timeout=180,
    )
    assert done.returncode == 2, done.returncode
    assert t("error.network") not in done.stderr, done.stderr
    print("https ok:", done.stderr.strip()[:60])


def _window_box(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def _click(hwnd: int, x: int, y: int) -> None:
    user32 = ctypes.windll.user32
    point = wintypes.POINT(x, y)
    user32.ClientToScreen(hwnd, ctypes.byref(point))
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    user32.SetCursorPos(point.x, point.y)
    time.sleep(0.2)
    user32.mouse_event(MOUSE_DOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSE_UP, 0, 0, 0, 0)


def _wait(condition, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"Timed out waiting for {what}"
        time.sleep(0.1)


def run_gui(exe: Path, server: FakeOdoo, work: Path, shots: Path) -> None:
    appdata = work / "appdata"
    out = work / "out"
    temp = work / "temp"
    temp.mkdir()
    config_file = appdata / APP_NAME / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(json.dumps({"url": server.url, "output_dir": str(out)}), "utf-8")
    env = {
        **os.environ,
        "ODOO_API_KEY": KEY,
        "APPDATA": str(appdata),
        "TEMP": str(temp),
        "TMP": str(temp),
    }
    user32 = ctypes.windll.user32
    title = f"Dossier de factures {__version__}"
    started = time.monotonic()
    process = subprocess.Popen([exe], env=env, cwd=work)
    try:
        # What a person sees while the program unpacks.
        while not user32.FindWindowW(None, title):
            assert time.monotonic() - started < 120, "Timed out waiting for the window"
            ImageGrab.grab().save(shots / "exe-0-arrencada.png")
            time.sleep(0.1)
        print(f"window after {time.monotonic() - started:.1f} s")
        hwnd = user32.FindWindowW(None, title)
        _wait(lambda: server.backend.calls, 60, "the automatic connection")
        _wait(lambda: time.monotonic() - server.last_call > 2, 60, "the connection to finish")
        time.sleep(1)
        ImageGrab.grab(bbox=_window_box(hwnd)).save(shots / "exe-1-connectat.png")

        _click(hwnd, *GENERATE_BUTTON)
        _wait(lambda: list(out.rglob("*.xlsx")), 120, "the generated files")
        _wait(lambda: time.monotonic() - server.last_call > 2, 60, "the generation to finish")
        time.sleep(2)
        ImageGrab.grab(bbox=_window_box(hwnd)).save(shots / "exe-2-resum.png")
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        assert process.wait(60) == 0
    finally:
        if process.poll() is None:
            process.kill()
    print(f"window closed cleanly after {time.monotonic() - started:.0f} s")

    check_documents(out)
    saved = config_file.read_text("utf-8")
    assert KEY not in saved
    assert json.loads(saved)["year"] == 2099
    log = (appdata / APP_NAME / "dossier.log").read_text("utf-8")
    assert KEY not in log
    assert not list(temp.glob("dossier-*")), "downloads left behind"
    try:
        assert keyring.get_password(APP_NAME, server.url) == KEY, "key not in the Windows vault"
    finally:
        keyring.delete_password(APP_NAME, server.url)
    print("configuration, log, temporary files and vault ok")


def main(dist: str, shots_dir: str) -> None:
    folder = Path(dist)
    shots = Path(shots_dir)
    shots.mkdir(parents=True, exist_ok=True)
    server = FakeOdoo()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    work = Path(tempfile.mkdtemp())
    try:
        (work / "cli").mkdir()
        (work / "gui").mkdir()
        run_cli(folder / "dossier-factures-cli.exe", server, work / "cli")
        check_https(folder / "dossier-factures-cli.exe", work / "cli")
        server.backend.calls.clear()
        run_gui(folder / "DossierFactures.exe", server, work / "gui", shots)
        assert server.methods <= READ_ONLY_METHODS, server.methods
        print("methods used:", sorted(server.methods))
        for exe in folder.glob("*.exe"):
            print(exe.name, f"{exe.stat().st_size / 1024 / 1024:.0f} MB")
    finally:
        server.shutdown()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
