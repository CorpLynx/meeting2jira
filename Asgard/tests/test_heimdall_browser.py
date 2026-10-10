"""Heimdall's browser half against a fake ServiceNow (tests/fake_servicenow.py).

Skipped when Playwright or a browser it can drive isn't available, which is the usual case on
an Asgard machine: Playwright is Heimdall's only third-party package. Uses headless Chromium; the
real thing runs Edge headed, so this proves navigation and field handling, not PIV sign-in.
Set HEIMDALL_TEST_CHANNEL to "msedge" or "chrome" to use an installed browser instead of
Playwright's bundled Chromium.
"""
import dataclasses
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for folder in (HERE, ROOT, ROOT / "apps" / "heimdall"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from fake_servicenow import SYS_ID, FakeServiceNow  # noqa: E402
from heimdall import browser  # noqa: E402
from heimdall import form as forms  # noqa: E402
from heimdall import templates as tpl  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:          # the normal case: Playwright isn't an Asgard requirement
    sync_playwright = None

FIELDS = [
    {"label": "Short description"},   # not required here, so each test sets only what it's about
    {"label": "Description"},
    {"label": "Category", "kind": "select"},
    {"label": "Requested for", "kind": "reference"},
    {"label": "I confirm this is accurate", "kind": "checkbox"},
]


def make_form(fake, fields=None, **changes):
    raw = {"instance_url": "https://placeholder.invalid", "catalog_sys_id": SYS_ID,
           "fields": fields or FIELDS, "attempts": 3}
    form = forms.parse(raw, Path("heimdall.json"))
    # Forms must be https; point this one at the local fake, for the test only.
    changes = dict({"sign_in_timeout_s": 30}, **changes)
    return dataclasses.replace(form, instance_url=fake.instance_url, **changes)


@unittest.skipIf(sync_playwright is None, "Playwright isn't installed (it's optional; Heimdall's fill needs it)")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeServiceNow().start()
        cls.pw = sync_playwright().start()
        channel = os.environ.get("HEIMDALL_TEST_CHANNEL") or None
        try:
            cls.browser = cls.pw.chromium.launch(headless=True, channel=channel)
        except Exception as exc:  # noqa: BLE001 - no bundled browser and no channel: skip, don't fail
            cls.pw.stop()
            cls.fake.stop()
            raise unittest.SkipTest(f"no browser for Playwright to drive ({browser.first_line(str(exc))}); "
                                    "run 'playwright install chromium' or set HEIMDALL_TEST_CHANNEL") from None

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.fake.stop()

    def setUp(self):
        self.fake.catalog_hits = 0
        self.fake.never_sign_in = False
        self.context = self.browser.new_context()     # fresh cookies: every test signs in again
        self.page = self.context.new_page()
        self.log = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)   # after the test's own cleanups (open files)

    def tearDown(self):
        self.context.close()

    def open(self, form):
        return browser.open_catalog_item(self.page, form, self.log.append)

    def plan(self, form, values, attachment=None):
        return tpl.plan(form, tpl.Saved(name="T", values=values), {}, attachment, False)

    # ------------------------------------------------------------ navigation

    def test_recovers_from_a_lost_deep_link_and_finds_the_form_in_the_iframe(self):
        frame = self.open(make_form(self.fake))
        self.assertNotEqual(frame, self.page.main_frame, "the second visit is the Next Experience iframe")
        self.assertTrue(any("instead of the catalog item" in line for line in self.log), self.log)

    def test_opens_the_classic_form_directly(self):
        self.fake.catalog_hits = 2
        frame = self.open(make_form(self.fake))
        self.assertEqual(frame, self.page.main_frame)

    def test_sign_in_that_never_finishes_fails_with_what_to_do(self):
        self.fake.never_sign_in = True
        with self.assertRaises(browser.BrowserError) as cm:
            self.open(make_form(self.fake, sign_in_timeout_s=6))
        self.assertIn("Finish signing in", str(cm.exception))

    # ------------------------------------------------------------ filling

    def test_fills_every_kind_and_attaches(self):
        self.fake.catalog_hits = 2
        form = make_form(self.fake)
        attachment = Path(self.tmp.name) / "secchm.xlsx"
        attachment.write_bytes(b"x")
        plan = self.plan(form, {"Short description": "Scan $today", "Description": "Monthly",
                                "Category": "Hardware", "Requested for": "Jane Example",
                                "I confirm this is accurate": False}, attachment=str(attachment))
        frame = self.open(form)
        browser.fill_fields(frame, plan, self.log.append)
        browser.attach_file(self.page, frame, plan.attachment, form, self.log.append)

        value = lambda sel: frame.locator(sel).input_value()  # noqa: E731
        self.assertTrue(value("#sd").startswith("Scan 20"))
        self.assertEqual(value("#desc"), "Monthly")
        self.assertEqual(value("#cat"), "hw")            # matched " Hardware " by its trimmed label
        self.assertEqual(value("#sys_display\\.rf"), "Jane Example")
        self.assertFalse(frame.locator("#ok").is_checked())
        self.assertEqual(frame.locator("#files").inner_text(), "secchm.xlsx")
        self.assertNotEqual(self.page.title(), "SUBMITTED", "Heimdall must never click Submit")

    def test_short_label_never_lands_on_a_longer_one(self):
        # "Description" is a substring of "Short description"; exact matching must win.
        self.fake.catalog_hits = 2
        form = make_form(self.fake)
        frame = self.open(form)
        browser.fill_fields(frame, self.plan(form, {"Description": "only here"}), self.log.append)
        self.assertEqual(frame.locator("#desc").input_value(), "only here")
        self.assertEqual(frame.locator("#sd").input_value(), "")

    def test_untouched_fields_are_left_alone_and_logged(self):
        self.fake.catalog_hits = 2
        form = make_form(self.fake)
        frame = self.open(form)
        browser.fill_fields(frame, self.plan(form, {"Short description": "x"}), self.log.append)
        self.assertTrue(frame.locator("#ok").is_checked())
        self.assertIn("Left as is: Description (no value in the template)", self.log)

    def test_two_fields_with_one_label_is_refused_not_guessed(self):
        self.fake.catalog_hits = 2
        form = make_form(self.fake, fields=FIELDS + [{"label": "Notes"}])
        frame = self.open(form)
        with self.assertRaises(browser.Ambiguous) as cm:
            browser.fill_fields(frame, self.plan(form, {"Notes": "x"}), self.log.append)
        self.assertIn("selector", str(cm.exception))
        self.assertEqual(frame.locator("#n1").input_value(), "")
        self.assertEqual(frame.locator("#n2").input_value(), "")

    def test_a_selector_settles_an_ambiguous_label(self):
        self.fake.catalog_hits = 2
        form = make_form(self.fake, fields=FIELDS + [{"label": "Notes", "selector": "#n2"}])
        frame = self.open(form)
        browser.fill_fields(frame, self.plan(form, {"Notes": "second"}), self.log.append)
        self.assertEqual((frame.locator("#n1").input_value(), frame.locator("#n2").input_value()), ("", "second"))

    def test_missing_field_names_the_label(self):
        self.fake.catalog_hits = 2
        form = make_form(self.fake, fields=FIELDS + [{"label": "Cost centre"}])
        frame = self.open(form)
        with self.assertRaises(browser.BrowserError) as cm:
            browser.fill_fields(frame, self.plan(form, {"Cost centre": "x"}), self.log.append)
        self.assertIn('"Cost centre"', str(cm.exception))

    def test_a_value_the_form_rewrites_is_reported(self):
        self.fake.catalog_hits = 2
        form = make_form(self.fake, fields=FIELDS + [{"label": "Phone"}])
        frame = self.open(form)
        with self.assertRaises(browser.BrowserError) as cm:
            browser.fill_fields(frame, self.plan(form, {"Phone": "555-0100"}), self.log.append)
        self.assertIn("didn't keep the value", str(cm.exception))

    def test_unknown_select_option_is_reported(self):
        self.fake.catalog_hits = 2
        form = make_form(self.fake)
        frame = self.open(form)
        with self.assertRaises(browser.BrowserError) as cm:
            browser.fill_fields(frame, self.plan(form, {"Category": "Hardwre"}), self.log.append)
        self.assertIn('"Category"', str(cm.exception))


class LabelPatternTests(unittest.TestCase):
    """The matching rules themselves; no browser needed."""

    def matches(self, label, name):
        _, marked = browser.label_patterns(label)[1]
        return bool(marked.match(name))

    def test_marked_pattern_allows_servicenow_markers_only(self):
        self.assertTrue(self.matches("Short description", "* Short description"))
        self.assertTrue(self.matches("Short description",
                                     "Mandatory - must be populated before Submit Short description"))
        self.assertTrue(self.matches("Short  description", "short description *"))
        self.assertFalse(self.matches("Description", "Short description"))
        self.assertFalse(self.matches("Description", "Description of impact"))

    def test_labels_with_regex_characters_are_literal(self):
        self.assertTrue(self.matches("Cost (USD)?", "* Cost (USD)?"))
        self.assertFalse(self.matches("Cost (USD)?", "Cost USD"))


if __name__ == "__main__":
    unittest.main()
