# Build with: pyinstaller packaging/dossier_factures.spec --noconfirm
import re
from pathlib import Path

import reportlab
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

import dossier_factures

HERE = Path(SPECPATH)
VERSION = dossier_factures.__version__
NUMBERS = tuple(int(n) for n in re.match(r"(\d+)\.(\d+)\.(\d+)", VERSION).groups()) + (0,)
AUTHOR = "Claudi Martí Rovira"

VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004,
    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040304B0', [
      StringStruct('CompanyName', '{author}'),
      StringStruct('FileDescription', '{description}'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{name}'),
      StringStruct('LegalCopyright', '© 2026 {author}. Llicència MIT.'),
      StringStruct('OriginalFilename', '{name}.exe'),
      StringStruct('ProductName', 'Dossier de factures'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [0x0403, 1200])])])
"""


def version_file(name, description):
    path = Path(workpath) / f"{name}-version.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        VERSION_INFO.format(
            numbers=NUMBERS, author=AUTHOR, description=description, version=VERSION, name=name
        ),
        encoding="utf-8",
    )
    return str(path)


FONTS = Path(reportlab.__file__).parent / "fonts"
DATAS = [
    *collect_data_files("dossier_factures"),
    # Only the two fonts the dossier embeds, not the whole folder.
    (str(FONTS / "Vera.ttf"), "reportlab/fonts"),
    (str(FONTS / "VeraBd.ttf"), "reportlab/fonts"),
    # keyring finds its backends through the package metadata.
    *copy_metadata("keyring"),
]
BINARIES = collect_dynamic_libs("pikepdf")
HIDDEN = [
    *collect_submodules("pikepdf"),
    *collect_submodules("win32ctypes.core"),
    "keyring.backends.Windows",
]
EXCLUDES = ["pytest", "pypdfium2", "ruff"]


def analyse(script, excludes=()):
    return Analysis(
        [str(HERE / script)],
        datas=DATAS,
        binaries=BINARIES,
        hiddenimports=HIDDEN,
        excludes=[*EXCLUDES, *excludes],
    )


ICON = str(HERE.parent / "src" / "dossier_factures" / "gui" / "icon.ico")

gui = analyse("gui_entry.py")
gui_pyz = PYZ(gui.pure)
# Unpacking takes seconds: without a picture people would click on the program again.
splash = Splash(str(HERE / "splash.png"), binaries=gui.binaries, datas=gui.datas)
EXE(
    gui_pyz,
    gui.scripts,
    splash,
    splash.binaries,
    gui.binaries,
    gui.datas,
    [],
    name="DossierFactures",
    icon=ICON,
    version=version_file("DossierFactures", "Dossier de factures"),
    console=False,
    upx=False,
)

cli = analyse("cli_entry.py", excludes=["tkinter", "dossier_factures.gui"])
EXE(
    PYZ(cli.pure),
    cli.scripts,
    cli.binaries,
    cli.datas,
    [],
    name="dossier-factures-cli",
    icon=ICON,
    version=version_file("dossier-factures-cli", "Dossier de factures (línia d'ordres)"),
    console=True,
    upx=False,
)
