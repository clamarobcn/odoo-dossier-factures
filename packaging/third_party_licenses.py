"""Write the notices of everything packaged into the program, and refuse an unsuitable licence."""

import sys
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT_PACKAGE = "dossier-factures"
FORBIDDEN = ("GPL", "SSPL", "EUPL")
LICENSE_NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")
BUNDLED = """Also packaged, outside the Python packages listed below:

- Python 3.12: Python Software Foundation License.
- Tcl/Tk (only in DossierFactures.exe): BSD-style Tcl licence.
- PyInstaller bootloader: GPL-2.0 with a special exception that allows distributing it with
  programs under any licence.
- Bitstream Vera fonts, embedded in the generated PDFs (shipped with reportlab): Bitstream Vera
  licence.
"""


def runtime_distributions() -> dict[str, metadata.Distribution]:
    found: dict[str, metadata.Distribution] = {}
    pending = [ROOT_PACKAGE]
    while pending:
        name = canonicalize_name(pending.pop())
        if name in found:
            continue
        found[name] = metadata.distribution(name)
        for text in found[name].requires or []:
            requirement = Requirement(text)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
    return found


def licence_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression
    classifiers = [
        c.split(" :: ")[-1] for c in meta.get_all("Classifier", []) if c.startswith("License ::")
    ]
    short = meta.get("License", "")
    if classifiers:
        return " / ".join(classifiers)
    return short.splitlines()[0] if short else "UNKNOWN"


def licence_files(dist: metadata.Distribution) -> list:
    return [
        file
        for file in dist.files or []
        if ".dist-info/" in str(file).replace("\\", "/")
        and file.name.upper().startswith(LICENSE_NAMES)
    ]


def native_libraries(dist: metadata.Distribution) -> list[str]:
    return sorted(
        file.name
        for file in dist.files or []
        if file.suffix.lower() == ".dll" and ".libs" in str(file)
    )


def main(path: str) -> int:
    version_line = f"Dossier de factures {metadata.version(ROOT_PACKAGE)}"
    parts = [f"{version_line}: third-party notices\n", BUNDLED]
    problems = []
    distributions = runtime_distributions()
    for name, dist in sorted(distributions.items()):
        if name == ROOT_PACKAGE:
            continue
        licence = licence_of(dist)
        files = licence_files(dist)
        print(f"{name} {dist.version}: {licence} ({len(files)} file(s))")
        if licence == "UNKNOWN" or any(word in licence.upper() for word in FORBIDDEN):
            problems.append(f"{name}: {licence}")
        if not files:
            problems.append(f"{name}: no licence text found")
        parts.append(f"\n{'=' * 78}\n{name} {dist.version}\nLicence: {licence}\n{'=' * 78}\n")
        libraries = native_libraries(dist)
        if libraries:
            print(f"  native: {', '.join(libraries)}")
            parts.append(f"Native libraries included: {', '.join(libraries)}\n")
        for file in files:
            parts.append(f"\n--- {file.name} ---\n")
            parts.append(Path(file.locate()).read_text(encoding="utf-8", errors="replace"))
    Path(path).write_text("\n".join(parts), encoding="utf-8")
    if problems:
        print("Licences to review:", *problems, sep="\n  ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
