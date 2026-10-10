"""The packaged build (docs/packaging.md): its entry point, and how Asgard behaves when frozen.

The build itself needs PyInstaller and runs in packaging/build.py and the GitHub workflow; these
tests cover the parts that decide whether it works, without building:
- frozen_main runs only Asgard's own scripts, the way python would, and the launcher otherwise;
- every place Asgard starts a Python process uses the build's two programs when frozen;
- setup copies the whole build into %LOCALAPPDATA%\\Asgard\\app, and Valhalla removes it once it has closed;
- the .cmd wrappers prefer the build's asgard-cli.exe, and the build's pins stay exact.
"""
import ast
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from asgard import catalog, install, paths, valhalla  # noqa: E402
from asgard.muninn import db  # noqa: E402

PACKAGING = ROOT / "packaging"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"packaging_{name}", PACKAGING / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


frozen_main = load("frozen_main")
build = load("build")


class Home(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)   # after the test's own cleanups (open files)
        self.dir = Path(self.tmp.name)
        self._saved = {k: os.environ.get(k) for k in ("ASGARD_HOME", "APPDATA")}
        os.environ["ASGARD_HOME"] = str(self.dir / "home")
        os.environ.pop("APPDATA", None)
        self._cwd = os.getcwd()

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class EntryPointTests(unittest.TestCase):
    def test_no_script_opens_the_launcher_with_its_options(self):
        self.assertEqual(frozen_main.resolve([], ROOT), (ROOT / "Asgard.pyw", []))
        self.assertEqual(frozen_main.resolve(["--muninn", "check"], ROOT), (ROOT / "Asgard.pyw", ["--muninn", "check"]))
        self.assertEqual(frozen_main.resolve(["--uninstall"], ROOT)[1], ["--uninstall"])

    def test_a_script_runs_with_its_arguments_like_python(self):
        script, args = frozen_main.resolve([str(ROOT / "apps" / "baldur" / "cli.py"), "report", "today"], ROOT)
        self.assertEqual((script, args), ((ROOT / "apps" / "baldur" / "cli.py").resolve(), ["report", "today"]))
        # relative to the current folder, as python reads it; .pyw too
        script, _ = frozen_main.resolve(["apps/baldur/baldur.pyw"], ROOT, cwd=ROOT)
        self.assertEqual(script, (ROOT / "apps" / "baldur" / "baldur.pyw").resolve())

    def test_only_scripts_inside_the_build_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "x.py"
            outside.write_text("print('no')\n")
            with self.assertRaisesRegex(frozen_main.Refused, "isn't part of Asgard"):
                frozen_main.resolve([str(outside)], ROOT)
            with self.assertRaisesRegex(frozen_main.Refused, "isn't part of Asgard"):
                frozen_main.resolve([str(ROOT / ".." / ".." / "x.py")], ROOT)
        with self.assertRaisesRegex(frozen_main.Refused, "There is no"):
            frozen_main.resolve([str(ROOT / "apps" / "nothing.py")], ROOT)

    def test_main_refuses_with_exit_2_and_a_message(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(frozen_main.main(["/elsewhere/x.py"]), 2)
        self.assertIn("isn't part of Asgard", err.getvalue())

    def test_a_script_sees_its_own_argv_and_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "probe.py"
            script.write_text("import sys, json\nRESULT = json.dumps([sys.argv, sys.path[0]])\n")
            saved = (list(sys.argv), list(sys.path))
            try:
                frozen_main.run_script(script, ["a", "b"])
                self.assertEqual(sys.argv, [str(script), "a", "b"])
                self.assertEqual(sys.path[0], str(script.parent))
            finally:
                sys.argv[:], sys.path[:] = saved

    def test_the_entry_point_imports_only_the_standard_library(self):
        tree = ast.parse((PACKAGING / "frozen_main.py").read_text(encoding="utf-8"))
        top = {node.names[0].name.split(".")[0] for node in tree.body if isinstance(node, ast.Import)}
        top |= {node.module.split(".")[0] for node in tree.body if isinstance(node, ast.ImportFrom) and node.module}
        self.assertLessEqual(top, {"__future__", "importlib", "os", "runpy", "sys", "tempfile", "traceback",
                                   "pathlib", "typing"})


class FrozenProcessTests(Home):
    def patched(self, folder: Path):
        exe = folder / ("Asgard.exe" if os.name == "nt" else "Asgard")
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(paths, "FROZEN", True))
        stack.enter_context(mock.patch.object(sys, "executable", str(exe)))
        return stack

    def programs(self, folder: Path):
        ext = ".exe" if os.name == "nt" else ""
        return str(folder.resolve() / f"asgard-cli{ext}"), str(folder.resolve() / f"Asgard{ext}")

    def test_the_launcher_starts_asgards_scripts_with_the_builds_programs(self):
        with self.patched(ROOT):
            console, windowed = self.programs(ROOT)
            self.assertEqual(catalog.python_paths(), (console, windowed))
            app = catalog.App("baldur", "Baldur", launch={"type": "python", "target": "{app}/apps/baldur/baldur.pyw"})
            spec = catalog.build_spec(app)
            self.assertEqual(spec.argv[0], windowed)
            self.assertTrue(spec.argv[1].endswith("baldur.pyw"))
            self.assertEqual(db.console_python(), console)

    def test_a_script_outside_the_build_gets_a_real_python(self):
        with self.patched(ROOT), mock.patch.object(catalog, "system_python", return_value=("py", "pyw")):
            app = catalog.App("odin", "Odin", launch={"type": "python", "target": str(self.dir / "odin.pyw")})
            self.assertEqual(catalog.build_spec(app).argv[:1], ["pyw"])
            with_venv = catalog.App("odin", "Odin", launch={"type": "python", "target": str(self.dir / "odin.pyw"),
                                                             "interpreter": "C:/odin/.venv/Scripts/pythonw.exe"})
            self.assertEqual(catalog.build_spec(with_venv).argv[0], "C:/odin/.venv/Scripts/pythonw.exe")

    def test_baldurs_weekly_task_runs_without_a_console(self):
        sys.path.insert(0, str(ROOT / "apps" / "baldur"))
        from baldur import cli as baldur_cli
        with self.patched(ROOT):
            action = baldur_cli.schedule_command("MON", "09:00")[-1]
        self.assertTrue(action.startswith(f'"{self.programs(ROOT)[1]}" '), action)
        self.assertIn("cli.py", action)

    def test_ysildirs_client_entry_uses_the_console_program(self):
        if importlib.util.find_spec("pydantic") is None:
            self.skipTest("Ysildir's modules need pydantic (MODULES.md)")
        sys.path.insert(0, str(ROOT / "apps" / "ysildir"))
        from ysildir import clients
        with self.patched(ROOT):
            command, args = clients.launch()
        self.assertEqual(command, self.programs(ROOT)[0])
        self.assertEqual(args[-1], "serve")


class FrozenSetupTests(Home):
    """Setup copies the whole packaged build into %LOCALAPPDATA%\\Asgard\\app (Brandon, Oct 10)."""

    def fake_build(self) -> Path:
        """A packaged build's folder: its programs, a DLL, the payload and a checked-hash .pyc."""
        build = self.dir / "Downloads" / "Asgard"
        ext = ".exe" if os.name == "nt" else ""
        for rel in ("Asgard.pyw", "VERSION", f"Asgard{ext}", f"asgard-cli{ext}", "python312.dll",
                    "asgard/launcher.py", "asgard/asgard.ico", "asgard/__pycache__/launcher.cpython-312.pyc",
                    "apps/baldur/cli.py", "payload.sha256"):
            (build / rel).parent.mkdir(parents=True, exist_ok=True)
            (build / rel).write_text(rel)
        return build

    def setup_from(self, build: Path):
        out = io.StringIO()
        with mock.patch.object(paths, "FROZEN", True), mock.patch.object(install, "SOURCE_ROOT", build), \
                mock.patch.object(sys, "executable", str(build / "asgard-cli")), contextlib.redirect_stdout(out):
            done = install.install()
        return done, out.getvalue()

    def test_setup_copies_the_whole_build_beside_asgards_data(self):
        build = self.fake_build()
        done, text = self.setup_from(build)
        app = self.dir / "home" / "app"
        copied = sorted(p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file())
        self.assertEqual(copied, sorted(p.relative_to(build).as_posix() for p in build.rglob("*") if p.is_file()),
                         "programs, DLLs, Asgard's code and its .pyc files all go")
        self.assertIn(f"to {app}", text)
        self.assertEqual(done["entry"], app / "Asgard.pyw")
        console, windowed = paths.frozen_programs(app)
        self.assertEqual(done["pythonw"], windowed, "the shortcut starts the installed copy, not the download")
        ledger = json.loads((self.dir / "home" / "install-ledger.json").read_text(encoding="utf-8"))
        self.assertIn({"kind": "dir", "path": str(app)}, ledger["items"])
        self.assertEqual((ledger["python"], ledger["pythonw"], ledger["packaged"]), (console, windowed, True))
        # The download can go now; upgrading means running the new download's setup.
        (build / "VERSION").write_text("next")
        self.setup_from(build)
        self.assertEqual((app / "VERSION").read_text(), "next")

    def test_uninstalling_from_the_running_build_removes_app_once_it_closes(self):
        self.setup_from(self.fake_build())
        app = self.dir / "home" / "app"
        with mock.patch.object(paths, "FROZEN", True), mock.patch.object(paths, "CODE_ROOT", app), \
                mock.patch.object(valhalla.winutil, "IS_WINDOWS", True), \
                mock.patch.object(valhalla.winutil, "delete_uninstall_entry", return_value=False), \
                mock.patch.object(valhalla.subprocess, "Popen") as popen:
            res = valhalla.uninstall()
        self.assertTrue(app.exists(), "Windows can't delete the programs that are running")
        self.assertEqual(res.after_exit, str(app))
        self.assertIn("goes as soon as Asgard closes", valhalla.summary(res))
        argv = popen.call_args[0][0]
        self.assertIn("-NoProfile", argv)
        self.assertIn(f"Wait-Process -Id {os.getpid()}", argv[-1])
        self.assertIn(f"Remove-Item -LiteralPath '{app}' -Recurse -Force", argv[-1])
        self.assertNotIn("Add-Type", argv[-1], "cmdlets only: Constrained Language Mode")
        self.assertFalse((self.dir / "home" / "install-ledger.json").exists())

    def test_the_folder_path_is_quoted_for_powershell(self):
        with mock.patch.object(valhalla.subprocess, "Popen"):
            argv = valhalla.remove_after_exit(Path("C:/Users/O'Brien/AppData/Local/Asgard/app"), pid=42)
        self.assertIn("'C:/Users/O''Brien/AppData/Local/Asgard/app'", argv[-1])

    def test_setup_refuses_to_run_from_the_installed_copy(self):
        build = self.fake_build()
        self.setup_from(build)
        app = self.dir / "home" / "app"
        try:
            import tkinter
            tkinter.Tcl()
        except Exception:
            self.skipTest("setup's prerequisites check Tcl/Tk first")
        with mock.patch.object(paths, "FROZEN", True), mock.patch.object(install, "SOURCE_ROOT", app):
            with self.assertRaisesRegex(install.SetupError, "This is the installed copy"):
                install.check_prerequisites()


class WrapperTests(unittest.TestCase):
    CMDS = [ROOT / "setup-Asgard.cmd"] + sorted((ROOT / "apps").glob("*/*.cmd"))

    def test_cmd_files_are_ascii_crlf_and_prefer_the_builds_program(self):
        self.assertEqual(len(self.CMDS), 4)
        for path in self.CMDS:
            with self.subTest(path.name):
                raw = path.read_bytes()
                self.assertTrue(raw.isascii())
                self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), "CRLF line endings")
                text = raw.decode("ascii")
                self.assertIn("asgard-cli.exe", text)
                self.assertNotIn("/dev/null", text, "cmd.exe has no /dev/null; use nul")
                where = '"%~dp0asgard-cli.exe"' if path.name == "setup-Asgard.cmd" else '"%~dp0..\\..\\asgard-cli.exe"'
                self.assertIn(f"if exist {where} set PYEXE={where}", text)


class BuildTests(unittest.TestCase):
    def test_build_pins_are_exact(self):
        lines = [ln for ln in (PACKAGING / "requirements-build.txt").read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")]
        self.assertEqual(sorted(ln.split("==")[0] for ln in lines), ["pyinstaller", "pyinstaller-hooks-contrib"])
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9._-]+==[0-9][0-9A-Za-z.]*", ln) for ln in lines), lines)

    def test_the_build_installs_every_runtime_pin_but_playwright(self):
        pins = build.runtime_pins()
        names = {p.split("==")[0].lower() for p in pins}
        self.assertNotIn("playwright", names)
        self.assertIn("pyside6-essentials", names)
        self.assertTrue({"mcp", "pydantic"} <= names)

    def test_the_spec_ships_what_setup_copies(self):
        tree = ast.parse((PACKAGING / "asgard.spec").read_text(encoding="utf-8"))
        found = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                try:
                    found[node.targets[0].id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
        self.assertLessEqual(set(install.PAYLOAD), set(found["PAYLOAD"]))
        self.assertIn("setup-Asgard.cmd", found["PAYLOAD"])
        self.assertIn("playwright", found["LEFT_OUT"])
        self.assertEqual(found["APP_FOLDERS"], tuple(sorted(p.name for p in (ROOT / "apps").iterdir()
                                                            if (p / p.name / "__init__.py").exists())))
        text = (PACKAGING / "asgard.spec").read_text(encoding="utf-8")
        self.assertIn('contents_directory="."', text)
        self.assertNotIn("onefile", text.replace("never one file", ""))

    def test_hashes_cover_every_file_but_the_list_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "a").mkdir()
            (folder / "a" / "b.txt").write_text("b")
            (folder / "payload.sha256").write_text("old")
            self.assertEqual(list(build.tree_hashes(folder)), ["a/b.txt"])

    def test_platform_tag(self):
        self.assertRegex(build.platform_tag(), r"^(windows|macos|linux)-(x64|arm64|[a-z0-9_]+)$")


if __name__ == "__main__":
    unittest.main()
