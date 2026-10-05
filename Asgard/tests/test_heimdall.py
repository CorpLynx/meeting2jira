"""Heimdall's form file, templates and command line. No browser: browser.py needs Playwright and
a ServiceNow instance, so it's exercised by hand; everything here is standard library."""
import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT, ROOT / "apps" / "heimdall"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from heimdall import cli  # noqa: E402
from heimdall import form as forms  # noqa: E402
from heimdall import templates as tpl  # noqa: E402

FORM = {
    "instance_url": "https://snow.example.gov/",
    "catalog_sys_id": "0123456789ABCDEF0123456789abcdef",
    "fields": [
        {"label": "Short description", "kind": "text", "required": True},
        {"label": "Category", "kind": "select", "default": "Software"},
        {"label": "Requested for", "kind": "reference"},
        {"label": "I confirm", "kind": "checkbox", "default": True},
    ],
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        self._home = os.environ.get("ASGARD_HOME")
        os.environ["ASGARD_HOME"] = str(self.dir)
        self.write_form(FORM)

    def tearDown(self):
        if self._home is None:
            os.environ.pop("ASGARD_HOME", None)
        else:
            os.environ["ASGARD_HOME"] = self._home
        self.tmp.cleanup()

    def write_form(self, body):
        path = forms.form_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body), encoding="utf-8")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, f"{argv} failed: {err}")
        return out

    def fails(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 1, f"{argv} should fail; stdout={out!r}")
        self.assertTrue(err.startswith("Heimdall: "), err)
        return err


class FormFileTests(Base):
    def test_example_form_loads(self):
        form = forms.parse(json.loads(forms.EXAMPLE.read_text(encoding="utf-8")), forms.EXAMPLE)
        self.assertTrue(form.fields)
        self.assertEqual(form.field(form.ready_label).label, form.ready_label)

    def test_normalizes_url_and_sys_id_and_defaults_ready_field(self):
        form = forms.load()
        self.assertEqual(form.instance_url, "https://snow.example.gov")
        self.assertEqual(form.catalog_sys_id, "0123456789abcdef0123456789abcdef")
        self.assertEqual(form.ready_label, "Short description")
        self.assertEqual(form.profile_dir, self.dir / "heimdall" / "edge-profile")

    def test_rejects_unusable_forms_with_the_reason(self):
        cases = [
            ({"instance_url": "http://snow.example.gov"}, "https://"),
            ({"catalog_sys_id": "abc"}, "32-character"),
            ({"ui": "workspace"}, '"ui"'),
            ({"fields": []}, '"fields"'),
            ({"fields": [{"label": "A"}, {"label": " a "}]}, "both labelled"),
            ({"fields": [{"label": "A", "kind": "date"}]}, '"kind"'),
            ({"fields": [{"label": "A", "kind": "checkbox", "default": "maybe"}]}, "true or false"),
            ({"fields": [{"label": "A", "default": "$tomorrow"}]}, "$tomorrow"),
            ({"attempts": 0}, '"attempts"'),
        ]
        for change, needle in cases:
            with self.subTest(change=change):
                with self.assertRaises(forms.FormError) as cm:
                    forms.parse(dict(FORM, **change), Path("heimdall.json"))
                self.assertIn(needle, str(cm.exception))

    def test_missing_form_says_to_run_init(self):
        forms.form_path().unlink()
        self.assertIn("heimdall init", self.fails("fields"))

    def test_init_copies_the_example_and_will_not_clobber(self):
        forms.form_path().unlink()
        self.ok("init")
        self.assertEqual(forms.form_path().read_bytes(), forms.EXAMPLE.read_bytes())
        self.assertIn("--force", self.fails("init"))
        self.ok("init", "--force")


class TemplateCommandTests(Base):
    def test_save_show_list_edit_rename_delete(self):
        self.ok("template", "save", "Monthly scan", "--set", "short description=Scan for $today",
                "--set", "Requested for=Jane Example", "--description", "the usual")
        listed = json.loads(self.ok("template", "list", "--json"))["templates"]
        self.assertEqual([(t["name"], t["fields_set"], t["problems"]) for t in listed],
                         [("Monthly scan", 2, [])])

        shown = json.loads(self.ok("template", "show", "MONTHLY SCAN", "--json"))
        # Labels are stored the way the form spells them, whatever case was typed.
        self.assertEqual(shown["values"], {"Short description": "Scan for $today", "Requested for": "Jane Example"})

        self.ok("template", "edit", "monthly scan", "--set", "I confirm=no", "--unset", "Requested for",
                "--rename", "Monthly scan v2")
        shown = json.loads(self.ok("template", "show", "Monthly scan v2", "--json"))
        self.assertEqual(shown["values"], {"Short description": "Scan for $today", "I confirm": False})
        self.assertEqual(shown["description"], "the usual")
        self.assertIn('No template named "Monthly scan"', self.fails("template", "show", "Monthly scan"))

        self.ok("template", "delete", "monthly scan V2")
        self.assertEqual(json.loads(self.ok("template", "list", "--json")), {"templates": []})

    def test_save_refuses_duplicates_unless_replace(self):
        self.ok("template", "save", "A", "--set", "Category=Hardware")
        self.assertIn("--replace", self.fails("template", "save", "a", "--set", "Category=Network"))
        self.ok("template", "save", "a", "--set", "Requested for=Bob", "--replace")
        self.assertEqual(json.loads(self.ok("template", "show", "A", "--json"))["values"], {"Requested for": "Bob"})

    def test_rejects_values_the_form_cant_take(self):
        cases = [
            (["--set", "Colour=Blue"], "not a field on the form"),
            (["--set", "I confirm=perhaps"], "true or false"),
            (["--set", "Short description=Cost $5"], "$$"),
            (["--set", "Short description=On $date"], "$date"),
            (["--set", "no equals sign"], "LABEL=VALUE"),
            (["--set", "Category=x", "--set", "category=y"], "given twice"),
        ]
        for extra, needle in cases:
            with self.subTest(extra=extra):
                self.assertIn(needle, self.fails("template", "save", "Bad", *extra))
        self.assertFalse(tpl.templates_path().exists(), "a rejected save must not write the file")

    def test_values_keep_equals_signs_and_escaped_dollars(self):
        self.ok("template", "save", "Eq", "--set", "Short description=a=b costs $$5")
        plan = json.loads(self.ok("fill", "-t", "Eq", "--dry-run", "--json"))
        self.assertEqual(plan["fields"][0]["value"], "a=b costs $5")

    def test_bad_template_names_are_refused(self):
        for name in ["", " ", "-dash first", "x" * 65, "semi;colon"]:
            with self.subTest(name=name):
                self.assertIn("Template names", self.fails("template", "save", name, "--set", "Category=x"))

    def test_broken_file_is_never_overwritten(self):
        path = tpl.templates_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"templates": {"A": ', encoding="utf-8")
        before = path.read_bytes()
        self.assertIn("won't overwrite", self.fails("template", "save", "B", "--set", "Category=x"))
        self.assertIn("won't overwrite", self.fails("template", "list"))
        self.assertEqual(path.read_bytes(), before)

    def test_saving_leaves_no_temporary_files(self):
        self.ok("template", "save", "A", "--set", "Category=x")
        self.ok("template", "edit", "A", "--set", "Category=y")
        self.assertEqual(sorted(p.name for p in tpl.templates_path().parent.iterdir()),
                         ["heimdall-templates.json", "heimdall.json"])

    def test_edit_with_nothing_to_change_says_so(self):
        self.ok("template", "save", "A", "--set", "Category=x")
        self.assertIn("Nothing to change", self.fails("template", "edit", "A"))

    def test_template_that_drifted_from_the_form_is_flagged_and_wont_fill(self):
        self.ok("template", "save", "Old", "--set", "Requested for=Jane", "--set", "Short description=x")
        self.write_form(dict(FORM, fields=[f for f in FORM["fields"] if f["label"] != "Requested for"]))
        listed = json.loads(self.ok("template", "list", "--json"))["templates"]
        self.assertEqual(listed[0]["problems"], ['"Requested for" is not a field on the form any more'])
        code, out, _ = self.run_cli("template", "show", "Old")
        self.assertEqual(code, 1)
        self.assertIn("Problem:", out)
        self.assertIn("doesn't match the form", self.fails("fill", "-t", "Old", "--dry-run"))


class FillPlanTests(Base):
    def plan(self, *argv):
        return json.loads(self.ok("fill", "--dry-run", "--json", *argv))

    def test_defaults_then_template_then_overrides(self):
        self.ok("template", "save", "T", "--set", "Short description=From template",
                "--set", "Category=Hardware", "--set", "I confirm=false")
        plan = self.plan("-t", "T", "--set", "Category=Network")
        got = {f["label"]: (f["value"], f["action"]) for f in plan["fields"]}
        self.assertEqual(got, {
            "Short description": ("From template", "set"),
            "Category": ("Network", "set"),               # override beats template
            "Requested for": (None, "leave"),             # nothing anywhere: left untouched
            "I confirm": (False, "uncheck"),              # template beats form default
        })
        self.assertEqual(plan["template"], "T")
        self.assertTrue(plan["item_url"].endswith("sysparm_id=0123456789abcdef0123456789abcdef"))

    def test_form_defaults_apply_without_a_template(self):
        plan = self.plan("--set", "Short description=x")
        got = {f["label"]: f["value"] for f in plan["fields"]}
        self.assertEqual((got["Category"], got["I confirm"]), ("Software", True))

    def test_empty_value_clears_a_default(self):
        plan = self.plan("--set", "Short description=x", "--set", "Category=")
        self.assertEqual(plan["fields"][1]["action"], "clear")

    def test_required_fields_must_have_a_value(self):
        self.assertIn("Short description", self.fails("fill", "--dry-run"))
        self.assertIn("Short description", self.fails("fill", "--dry-run", "--set", "Short description="))

    def test_placeholders_fill_in_the_date(self):
        form = forms.load()
        saved = tpl.Saved(name="D", values={"Short description": "Scan $today / ${today_us}"})
        plan = tpl.plan(form, saved, {}, None, False, today=dt.date(2026, 10, 4))
        self.assertEqual(plan.steps[0][1], "Scan 2026-10-04 / 10/04/2026")

    def test_attachment_from_template_override_and_none(self):
        a, b = self.dir / "a.xlsx", self.dir / "b.xlsx"
        a.write_bytes(b"a")
        b.write_bytes(b"b")
        self.ok("template", "save", "T", "--set", "Short description=x", "--attachment", str(a))
        self.assertEqual(self.plan("-t", "T")["attachment"], str(a))
        self.assertEqual(self.plan("-t", "T", "--attachment", str(b))["attachment"], str(b))
        self.assertIsNone(self.plan("-t", "T", "--no-attachment")["attachment"])
        a.unlink()
        self.assertIn("--no-attachment", self.fails("fill", "-t", "T", "--dry-run"))

    def test_edit_can_drop_the_attachment(self):
        a = self.dir / "a.xlsx"
        a.write_bytes(b"a")
        self.ok("template", "save", "T", "--set", "Short description=x", "--attachment", str(a))
        self.ok("template", "edit", "T", "--no-attachment")
        self.assertIsNone(json.loads(self.ok("template", "show", "T", "--json"))["attachment"])

    def test_text_dry_run_says_it_stops_before_submit(self):
        out = self.ok("fill", "--dry-run", "--set", "Short description=x")
        self.assertIn("Requested for: (left as is)", out)
        self.assertIn("stops before Submit", out)

    def test_json_without_dry_run_is_a_usage_error(self):
        code, _, err = self.run_cli("fill", "--json", "--set", "Short description=x")
        self.assertEqual(code, 2)
        self.assertIn("--dry-run", err)


class BoundaryTests(Base):
    def test_template_commands_never_import_playwright(self):
        # A UI must be able to use everything except fill without Playwright installed.
        code = ("import sys; sys.argv=['heimdall','template','list','--json'];"
                "import runpy; sys.modules['playwright']=None;"
                "runpy.run_path(sys.argv0, run_name='__main__')")
        script = ROOT / "apps" / "heimdall" / "cli.py"
        env = dict(os.environ, ASGARD_HOME=str(self.dir))
        done = subprocess.run(
            [sys.executable, "-c", code.replace("sys.argv0", repr(str(script)))],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"templates": []})


if __name__ == "__main__":
    unittest.main()
