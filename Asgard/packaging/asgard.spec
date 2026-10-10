# PyInstaller spec for Asgard's packaged build (docs/packaging.md). Run it through build.py,
# which installs the pinned packages first and checks the result:
#
#     py -3.12 packaging\build.py            (Windows; python3 packaging/build.py elsewhere)
#
# One folder ("onedir"), never one file: a one-file program unpacks itself into %TEMP% on every
# start, which writes programs into the profile (AGENTS.md rule 2) and which App Control blocks.
# The folder holds two programs that share everything else:
#   Asgard.exe       windowed, like pythonw: the launcher, and every app window it starts
#   asgard-cli.exe   console, like python: the command lines, setup, and Ysildir's stdio server
# Asgard's own code is shipped as .py files at its usual layout (asgard\, apps\), with checked-hash
# .pyc files beside them, and runs from there (packaging/frozen_main.py says why). PyInstaller still
# reads it, to find every standard-library and third-party module it imports.
#
# Left out on purpose: Playwright (Heimdall `fill`). It brings node.exe and a browser driver that
# App Control would block in this folder anyway; `fill` says what's missing and `fill --dry-run`
# still works (MODULES.md, "playwright").
import compileall
import py_compile
import shutil
from pathlib import Path

HERE = Path(SPECPATH)                       # noqa: F821 (PyInstaller defines SPECPATH and the classes below)
ROOT = HERE.parent                          # Asgard/
APP_FOLDERS = ("baldur", "heimdall", "ysildir")
OURS = {"asgard"} | set(APP_FOLDERS)       # packages that run from the .py files, never from the archive
PAYLOAD = ("Asgard.pyw", "VERSION", "README.md", "MODULES.md", "requirements.txt", "setup-Asgard.cmd",
           "asgard", "apps")
LEFT_OUT = ["playwright", "pytest", "_pytest", "IPython", "matplotlib", "numpy"]
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store")


def stage_payload(target: Path) -> Path:
    """Copy the payload to the work folder and compile it; the build ships this copy."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in PAYLOAD:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, target / name, ignore=SKIP)
        else:
            shutil.copy2(src, target / name)
    # Checked-hash .pyc files: Python uses them whatever the files' times are after copying, and
    # still notices an edited .py. Without them every start would compile Asgard again.
    if not compileall.compile_dir(str(target), quiet=1,
                                  invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH):
        raise SystemExit("Asgard's code didn't compile; fix that before packaging.")
    return target


def our_modules() -> list:
    """Every module of Asgard's own packages, so PyInstaller follows their imports."""
    found = []
    packages = [(ROOT, "asgard")] + [(ROOT / "apps" / name, name) for name in APP_FOLDERS]
    for base, package in packages:
        for path in sorted((base / package).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            parts = list(path.relative_to(base).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            found.append(".".join(parts))
    return found


payload = stage_payload(Path(workpath) / "payload")       # noqa: F821
datas = [(str(path), str(path.parent.relative_to(payload)))
         for path in sorted(payload.rglob("*")) if path.is_file()]

a = Analysis(                                                       # noqa: F821
    [str(HERE / "frozen_main.py")],
    pathex=[str(ROOT)] + [str(ROOT / "apps" / name) for name in APP_FOLDERS],
    hiddenimports=our_modules(),
    datas=datas,
    excludes=LEFT_OUT,
    noarchive=False,
)
# Asgard's packages run from the shipped .py files (datas), so they mustn't also sit in the archive,
# where they would shadow those files and every path worked out from __file__ would be wrong.
a.pure = [entry for entry in a.pure if entry[0].split(".")[0] not in OURS]
pyz = PYZ(a.pure)                                                   # noqa: F821

icon = str(ROOT / "asgard" / "asgard.ico")
# contents_directory=".": the payload sits beside the programs, at an installed copy's layout, so
# the .cmd wrappers find asgard-cli.exe two folders up from apps\<name>\ and paths stay short.
# Each program's bootloader reads it, so both get it.
windowed = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Asgard", console=False,     # noqa: F821
               icon=icon, upx=False, contents_directory=".")
console = EXE(pyz, a.scripts, [], exclude_binaries=True, name="asgard-cli", console=True,   # noqa: F821
              icon=icon, upx=False, contents_directory=".")
COLLECT(windowed, console, a.binaries, a.datas, name="Asgard", upx=False)                    # noqa: F821
