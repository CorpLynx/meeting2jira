# Packaging: the PyInstaller build

Oct 9, 2026 · status: built, and green on Windows in CI (Oct 10) · owner: Asgard core

Asgard ships two ways. Both hold the same code and keep their data in `%LOCALAPPDATA%\Asgard`:

| | The Python install (zip of the code) | The packaged build (PyInstaller) |
| --- | --- | --- |
| Needs | The agency's Python 3.11+ with Tcl/Tk; IT approval for any package wheels | Nothing installed; IT allows (or signs) the folder's programs and DLLs |
| Setup | `setup-Asgard.cmd` copies it to `%LOCALAPPDATA%\Asgard\app` | The same: `setup-Asgard.cmd` copies the whole build to `%LOCALAPPDATA%\Asgard\app` |
| Brings | Your Python, and only the packages IT installed | Python 3.12, the standard library, PySide6, the MCP SDK and pydantic |
| Made by | GitHub's **Download ZIP** | `packaging/build.py`, locally or in GitHub Actions |

The Python install stays the default: it needs no executables approved. The packaged build is for a machine where IT would rather approve one folder of programs than a Python and its packages.

## Decisions

- **One folder ("onedir"), never one file.** A one-file program unpacks a fresh copy of itself into `%TEMP%` on every start, which App Control blocks and which leaves copies behind. A folder starts faster, stays in one known place, and can be checked file by file (`payload.sha256`).
- **Installed into `%LOCALAPPDATA%\Asgard\app`, like the Python install** (Brandon, Oct 10). Setup copies the whole build there (programs, DLLs, Python and Asgard's code), beside Asgard's data (Muninn, settings, logs, backups), and points the shortcut and Settings > Apps at the copy. The extracted download can go afterwards, and an upgrade is the new download's setup. It doesn't need admin rights, and uninstalling removes everything but your data unless you ask. App Control has to allow programs in that folder (by path, hash or signature); `asgard-cli.exe --self-test` there shows whether it does.
- **Uninstalling the running copy.** Windows won't delete a program while it runs, so when Valhalla runs from `app\` it removes everything else at once and starts a hidden PowerShell that waits for Asgard to exit, then deletes `app\`. It uses cmdlets only (`Wait-Process`, `Remove-Item`), which Constrained Language Mode allows.
- **Secrets stay in Windows Credential Manager.** Baldur's GitHub token is the one thing Asgard keeps outside `%LOCALAPPDATA%\Asgard`. Credential Manager encrypts it to your Windows account; a file in the profile would be readable by anything running as you.
- **Two programs stand in for Python.** `Asgard.exe` is windowed, like `pythonw.exe`; `asgard-cli.exe` has a console, like `python.exe`. Both run `packaging/frozen_main.py`: with no script they open the launcher (`Asgard.pyw`, so `--uninstall` and `--muninn ...` work too); given one of Asgard's `.py` or `.pyw` files they run it as Python would. So everything that starts a process works unchanged: the launcher's tiles, Baldur's weekly task, the shared window's children, Ysildir's client entry, and the `.cmd` wrappers, which use `asgard-cli.exe` when it's beside them (`paths.frozen_programs()`).
- **Only Asgard's own scripts run.** A script outside the build's folder is refused: the build is Asgard, not a general-purpose Python. An external tile such as Odin's runs under a real Python from `PATH` (or its own virtual environment), as it needs its own packages.
- **Asgard's code ships as `.py` files**, at an installed copy's layout (`asgard\`, `apps\`, `Asgard.pyw`), with checked-hash `.pyc` files beside them. Everything that finds files from `__file__` (Muninn's migrations, prompts, QML, `apps.json`) keeps working, and nothing is compiled at start. PyInstaller still reads the code to find every module it imports; `asgard.spec` then takes Asgard's own packages out of the archive so they can't shadow the files. The self-test checks they load from the files.
- **The flat layout** (`contents_directory="."`): the payload sits beside the programs, so `apps\baldur\baldur.cmd` finds `asgard-cli.exe` two folders up and paths match an installed copy.
- **Python 3.12.** Muninn needs SQLite 3.37+ with FTS5 and JSON, which Windows Python has from 3.11, and Ysildir needs 3.10+. Every pin in `requirements.txt` supports 3.12.
- **Playwright is left out.** It brings `node.exe` and a browser driver that App Control would block in this folder anyway. Heimdall's `fill` says what's missing, and `fill --dry-run` (and **Dry run** in the window) still lists every value (MODULES.md, "playwright").
- **Running writes nothing into the folder.** Checked by the build, so the installed copy only changes when setup replaces it.

## Building it

```
py -3.12 packaging\build.py              Windows: the build that ships
python3 packaging/build.py               macOS or Linux: that system's build, to try the steps
```

`--skip-tests` skips the unit tests; `--keep-venv` reuses `build\venv` when you rebuild after changing Asgard's code; `--no-venv` installs into the Python running it. The steps, each stopping the build if it fails:

1. A fresh `build\venv` with the pinned packages, wheels only: `requirements.txt` without Playwright, and `packaging/requirements-build.txt` (PyInstaller and its hooks, build-time only).
2. The unit tests, with those packages.
3. PyInstaller with `packaging/asgard.spec`: `build\dist\Asgard\`.
4. Checks through the built programs, in a temporary `ASGARD_HOME`:
   - `asgard-cli --self-test` (SQLite, Muninn created and checked, Tk, Qt, the MCP SDK, the apps loading from their files, TLS);
   - `--muninn prepare` and `--muninn check`, and the windowed program;
   - Baldur from `setup` through `collect` and `estimate` to `days`, on a small git repository (when git is on `PATH`);
   - `ysildir check`, Heimdall's `--help`, and setup copying the build into the temporary `ASGARD_HOME\app`, with the self-test run again from there (on Windows only in CI, as setup writes Asgard's Settings > Apps entry);
   - a script from outside refused;
   - nothing written into the folder.
5. `payload.sha256` in the folder (one line per file, as [updates.md](updates.md) designs), then `build\Asgard-VERSION-windows-x64.zip` and its `.sha256`.

Built on Linux on Oct 9 (Python 3.12.11, PyInstaller 6.22.3): every check passed. The machine had no graphics libraries, so Qt was left out of the self-test; the build does that only when its own Python can't load Qt either, never on Windows. The zip was 151 MB, with 2,468 files. Ysildir answered an MCP client over stdio from the build.

Built on Windows on Oct 10 by the workflow (Windows Server 2025, Python 3.12.10): every check passed, Qt included. The zip was 85 MB, with 3,086 files. The first run had failed 17 unit tests that Linux can't show, because Windows won't delete an open file. One was a real leak in `muninn.connect()`; the rest were the tests' own cleanup order. Both are fixed.

## In GitHub Actions

`.github/workflows/asgard-package.yml` runs `build.py` on Windows Server 2025 with Python 3.12, and uploads the zip and its `.sha256` as the `asgard-windows-x64` artifact (kept 30 days). It runs:

- **On a tag named `asgard-vX.Y.Z`.** It checks that `VERSION` says `X.Y.Z`, then publishes a GitHub release with the zip.
- **On a pull request or push** that changes the packaging, and on a pull request that changes anything in `Asgard/`.
- **By hand** (`workflow_dispatch`).

Actions are pinned to commit SHAs. The workflow reads the repository only, except the release job, which may write releases.

## On the workstation

1. Check the zip against its `.sha256` (`certutil -hashfile Asgard-....zip SHA256`).
2. Unzip it anywhere and run `setup-Asgard.cmd` in it (or `asgard-cli.exe asgard\install.py`). Setup copies the build to `%LOCALAPPDATA%\Asgard\app`, adds the Start menu shortcut and Settings > Apps entry, and opens Asgard.
3. If it doesn't start, run `asgard-cli.exe --self-test` in `%LOCALAPPDATA%\Asgard\app`. A part that fails with a blocked DLL or program means App Control needs to allow that folder's files (by path, hash or signature).

To upgrade, close Asgard and run the new download's setup. Your data and settings stay in `%LOCALAPPDATA%\Asgard`.

## Alternatives, if PyInstaller won't do

In order:

1. **The Python install**, which is always there: the zip of the code, on the agency's Python.
2. **Python's embeddable package** (the official `python-3.12.x-embed-amd64.zip`) with the wheels unpacked beside it and `pythonw.exe Asgard.pyw` as the shortcut. That's no bootloader at all, and its DLLs are signed by the PSF. It doesn't include Tcl/Tk, so the launcher would need it added.
3. **Nuitka**, which compiles to C. Its programs are harder to inspect and slower to build, and it has the same App Control questions.
4. **cx_Freeze**, a similar freezer with an MSI target. An MSI usually needs admin rights, which Asgard avoids.

## Open

- [ ] **Signing.** Unsigned programs need App Control rules by path or hash. If the agency signs, add a signing step after the build, before `payload.sha256`. To find out what the workstation allows before asking IT, run `tools/asgard_signing_survey.ps1` there (read-only, seven steps; `-AsgardFolder` points it at the unzipped build, `-TryRun` runs the self-test from it). It was parse-checked on PowerShell 7.6 and smoke-run on macOS, never on Windows, so its Windows results need target-machine verification.
- [ ] **App Control's rule for `%LOCALAPPDATA%\Asgard\app`**: by path, by hash (every release changes it), or signing.
- [ ] **Updates** ([updates.md](updates.md)): the build already writes `payload.sha256`; the updater that checks it isn't built.
- [x] **The first Windows run** of the workflow: green on Oct 10 (run 8 on this branch also checks setup copying the build into `app\` and its self-test there).
- [ ] **The self-test on the workstation**, under App Control: `asgard-cli.exe --self-test`.
