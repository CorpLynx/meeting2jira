"""Drive Edge to fill the SeCcHm form from a Plan, then stop for you to review and Submit.

Rules it keeps:
- Real browser, normal UI, normal SSO sign-in. It never reads, copies or stores tokens, cookies
  or the CSRF value; the session lives only in Heimdall's Edge profile.
- Never clicks Submit.
- Headed only, so the certificate picker and the PIN prompt can appear.
- The only Heimdall module that needs a third-party package (Playwright). It imports it inside
  functions, so importing this module never fails, and nothing else in Asgard imports it.

Why it navigates the way it does (each of these made "the catalog URL works sometimes"):
1. SSO is a redirect chain (instance -> IdP -> saml_consume -> home). Deep-linking while it runs
   gets "interrupted by another navigation" or loses the link, so we wait until the URL has
   *stayed* on a signed-in instance page for a few seconds before going anywhere.
2. ServiceNow long-polls (AMB), so "networkidle" never reliably arrives. We wait for
   domcontentloaded and then for a field we know is on the form.
3. Next Experience may wrap the classic form in an iframe. We search every frame for the form.
4. A persistent profile can restore old tabs that navigate on their own. We keep one tab.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from .form import Form
from .templates import Plan

Log = Callable[[str], None]

# Path fragments that mean "still signing in", not "on the instance".
_SIGN_IN_MARKERS = ("login", "saml", "sso", "logout", "/auth", "oauth")

_ITEM_URLS = {
    # Classic form, no Next Experience shell. Next Experience may still wrap it in an iframe.
    "classic": "{base}/com.glideapp.servicecatalog_cat_item_view.do?v=1&sysparm_id={sys_id}",
    # Service Portal page. Single-page app, no iframe.
    "portal": "{base}/sp?id=sc_cat_item&sys_id={sys_id}",
}


# Per action: long enough for a slow form, short enough that a wrong option fails fast.
_ACTION_MS = 10_000


class BrowserError(Exception):
    """Something in Edge didn't go as expected. The message says what to do next."""


def item_url(form: Form) -> str:
    return _ITEM_URLS[form.ui].format(base=form.instance_url, sys_id=form.catalog_sys_id)


# ---------------------------------------------------------------- navigation


def is_signed_in_url(url: str, host: str) -> bool:
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != host:
        return False
    path = parts.path.lower()
    return not any(marker in path for marker in _SIGN_IN_MARKERS)


def settle(page: Any, host: str, timeout_s: float, quiet_s: float = 3.0) -> str:
    """Wait until the tab has stayed on a signed-in instance URL for quiet_s seconds.

    "Arrived on the instance once" is not enough: the first hop of SSO is the instance itself,
    and the end of the chain bounces back through it. Only a URL that stops changing counts.
    """
    deadline = time.monotonic() + timeout_s
    last_url = ""
    since = time.monotonic()
    while time.monotonic() < deadline:
        url = page.url
        now = time.monotonic()
        if url != last_url:
            last_url, since = url, now
        elif now - since >= quiet_s and is_signed_in_url(url, host):
            return url
        page.wait_for_timeout(250)
    raise BrowserError(
        f"Still not signed in to {host} after {int(timeout_s)} s (last page: {safe_url(last_url)}). "
        "Finish signing in in the Edge window, then run Heimdall again."
    )


class Ambiguous(BrowserError):
    """More than one field answers to a label; filling either would be a guess."""


# What ServiceNow puts in a label besides its text: a "*" and, for screen readers, a sentence
# saying the field is mandatory. A label's accessible name can carry either, before or after.
_MARKER = r"(?:\*|mandatory\b[^\n]*?\bsubmit\b|mandatory|required)"


def label_patterns(label: str) -> List[Tuple[str, Any]]:
    """How a label is matched, strictest first: exact, exact plus ServiceNow's mandatory markers,
    then anywhere in the name. Each is tried in turn, and the first one that finds fields wins, so
    "Description" never lands on "Short description" while a field labelled "Description" exists.
    """
    words = r"\s+".join(re.escape(w) for w in label.split())
    marked = re.compile(rf"^\s*(?:{_MARKER}\s*)*{words}\s*(?:{_MARKER}\s*)*$", re.I)
    return [("exact", label), ("marked", marked), ("contains", re.compile(words, re.I))]


def _candidates(loc: Any) -> List[Any]:
    """The matches a person could see, or all of them when none are visible (some checkboxes are
    styled so the real input isn't). ServiceNow pairs visible inputs with hidden twins."""
    found = [loc.nth(i) for i in range(loc.count())]
    visible = [x for x in found if x.is_visible()]
    return visible or found


def resolve(frame: Any, label: Optional[str], selector: Optional[str]) -> Optional[Any]:
    """The one element for a field, None if it isn't on this frame yet, Ambiguous if it's two."""
    if selector:
        found = _candidates(frame.locator(selector))
        if len(found) > 1:
            raise Ambiguous(f'The selector {selector!r} matches {len(found)} elements. Make it specific '
                            "to one field.")
        return found[0] if found else None
    assert label
    for how, pattern in label_patterns(label):
        found = _candidates(frame.get_by_label(pattern, exact=True) if how == "exact"
                            else frame.get_by_label(pattern))
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise Ambiguous(f'"{label}" matches {len(found)} fields on the form ({how} match). Give that '
                            'field a "selector" in the form file, like "#IO\\\\:<sys_id>".')
    return None


def wait_for_field(frame: Any, label: Optional[str], selector: Optional[str], timeout_s: float) -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        target = resolve(frame, label, selector)
        if target is not None:
            return target
        if time.monotonic() >= deadline:
            raise BrowserError(f'No field labelled "{label or selector}" on the form. Check the label in the '
                               'form file matches the form exactly, or give it a "selector".')
        frame.wait_for_timeout(250)


def find_form_frame(page: Any, form: Form, timeout_s: float) -> Optional[Any]:
    """Return the frame (main page or an iframe) that shows the ready field, or None."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for frame in page.frames:
            try:
                target = resolve(frame, form.ready_label, form.ready_selector)
                if target is not None and target.is_visible():
                    return frame
            except Ambiguous:
                raise
            except Exception:  # noqa: BLE001 - a frame detaching mid-check is normal; try the next
                continue
        page.wait_for_timeout(500)
    return None


def open_catalog_item(page: Any, form: Form, log: Log) -> Any:
    from playwright.sync_api import Error as PWError

    base, sys_id = form.instance_url, form.catalog_sys_id
    host = (urlsplit(base).hostname or "").lower()
    target = item_url(form)

    # 1. Sign in on a plain page first and let the whole redirect chain finish.
    log("Opening the instance. If Edge asks, pick your certificate and enter your PIN.")
    try:
        page.goto(f"{base}/navpage.do", wait_until="domcontentloaded", timeout=60_000)
    except PWError:
        pass  # interrupted by the SSO redirect; settle() decides whether we got there
    settle(page, host, form.sign_in_timeout_s)
    log("Signed in.")

    problem = ""
    for attempt in range(1, form.attempts + 1):
        try:
            # 2. domcontentloaded, never networkidle.
            page.goto(target, wait_until="domcontentloaded", timeout=60_000)
        except PWError as exc:
            problem = first_line(str(exc))  # usually the redirect race; settle and check
        # The deep link can bounce through the IdP again if the session was half-made.
        settle(page, host, form.sign_in_timeout_s)

        if sys_id not in unquote(page.url):
            problem = f"landed on {safe_url(page.url)} instead of the catalog item"
        else:
            # 3. Ready means the form is visible, in whichever frame it rendered.
            frame = find_form_frame(page, form, timeout_s=30)
            if frame is not None:
                return frame
            problem = "the page loaded but the ready field never appeared"

        if attempt < form.attempts:
            log(f"Attempt {attempt}/{form.attempts}: {problem}. Retrying.")
            page.wait_for_timeout(2_000 * attempt)

    raise BrowserError(
        f"Could not open the catalog item after {form.attempts} tries ({problem}). Check that "
        '"catalog_sys_id" opens in a normal Edge tab and that "ready_label" matches a field label '
        "on the form exactly. Rerun with --trace to see every redirect."
    )


# ---------------------------------------------------------------- filling


def fill_fields(frame: Any, plan: Plan, log: Log) -> None:
    for field, value in plan.steps:
        if value is None:
            log(f"Left as is: {field.label} (no value in the template)")
            continue
        target = wait_for_field(frame, field.label, field.selector, timeout_s=15)
        try:
            if field.kind == "text":
                target.fill(value, timeout=_ACTION_MS)
                ok = target.input_value() == value
            elif field.kind == "select":
                target.select_option(label=value, timeout=_ACTION_MS)
                ok = target.evaluate("e => e.options[e.selectedIndex] && e.options[e.selectedIndex].label.trim()") \
                    == value.strip()
            elif field.kind == "checkbox":
                target.set_checked(bool(value), timeout=_ACTION_MS)
                ok = target.is_checked() == bool(value)
            else:
                # Reference fields resolve through an autocomplete; type like a person, then
                # leave the field so ServiceNow looks the value up. ServiceNow may rewrite the
                # text to the record's display name, so there's nothing exact to check.
                target.fill("", timeout=_ACTION_MS)
                target.press_sequentially(value, delay=60)
                frame.wait_for_timeout(1_500)
                target.press("Tab")
                ok = True
        except BrowserError:
            raise
        except Exception as exc:  # noqa: BLE001 - report which field, keep the browser open
            raise BrowserError(
                f'Could not fill "{field.label}" ({field.kind}): {first_line(str(exc))}. Check the label '
                'matches the form exactly, or give that field a "selector" in the form file.'
            ) from exc
        if not ok:
            # The page took the input but shows something else (a script reset it, or a mask
            # reformatted it). Say so instead of letting the review page look complete.
            raise BrowserError(f'"{field.label}" didn\'t keep the value Heimdall entered. Check it in Edge, '
                               "and fill it by hand if the form changes it.")
        log(f"Filled: {field.label}")


def attach_file(page: Any, frame: Any, path: str, form: Form, log: Log) -> None:
    file_name = Path(path).name
    try:
        if form.attach_selector:
            with page.expect_file_chooser(timeout=15_000) as chooser:
                frame.locator(form.attach_selector).first.click()
            chooser.value.set_files(path)
        else:
            inputs = frame.locator("input[type=file]")
            if inputs.count():
                inputs.first.set_input_files(path)
            else:
                button = frame.get_by_role("button", name=re.compile("attach", re.I)).or_(
                    frame.get_by_role("link", name=re.compile("attach", re.I))
                ).first
                with page.expect_file_chooser(timeout=15_000) as chooser:
                    button.click()
                chooser.value.set_files(path)
        # Don't trust the upload until the form shows the file name.
        frame.get_by_text(file_name, exact=False).first.wait_for(state="visible", timeout=60_000)
    except Exception as exc:  # noqa: BLE001
        raise BrowserError(
            f"Could not attach {file_name}: {first_line(str(exc))}. Attach it by hand, or set "
            '"attach_selector" in the form file to the paperclip button.'
        ) from exc
    log(f"Attached: {file_name}")


# ---------------------------------------------------------------- run


def run(form: Form, plan: Plan, log: Log, trace: bool = False) -> None:
    try:
        from playwright.sync_api import Error as PWError, sync_playwright
    except ImportError:
        raise BrowserError(
            "Playwright is not installed for this Python. Ask IT to install it in an allowed "
            "path (AppLocker blocks its node.exe in your profile)."
        ) from None

    profile = form.profile_dir
    profile.mkdir(parents=True, exist_ok=True)
    trace_path: Optional[Path] = None

    with sync_playwright() as pw:
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel=form.channel,
                headless=False,  # the certificate picker and PIN prompt need a window
                no_viewport=True,
                accept_downloads=False,
            )
        except PWError as exc:
            raise BrowserError(
                f"Edge would not start under automation: {first_line(str(exc))}. Close any Edge "
                "window using this profile, and check edge://policy for RemoteDebuggingAllowed."
            ) from exc

        if trace:
            context.tracing.start(screenshots=True, snapshots=True)
        try:
            # 4. One tab only: close anything the profile restored.
            page = context.pages[0] if context.pages else context.new_page()
            for extra in context.pages[1:]:
                extra.close()

            frame = open_catalog_item(page, form, log)
            fill_fields(frame, plan, log)
            if plan.attachment:
                attach_file(page, frame, plan.attachment, form, log)

            page.bring_to_front()
            log("Form is filled. Review it in Edge and click Submit yourself. "
                "Close the Edge window when you're done.")
            page.wait_for_event("close", timeout=0)
        except PWError as exc:
            if "closed" in str(exc).lower():
                log("Edge was closed.")
            else:
                raise BrowserError(f"Browser error: {first_line(str(exc))}. Rerun with --trace.") from exc
        finally:
            if trace:
                stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                trace_path = profile.parent / "traces" / f"heimdall-{stamp}.zip"
                trace_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    context.tracing.stop(path=str(trace_path))
                except PWError:
                    trace_path = None
            try:
                context.close()
            except PWError:
                pass

    if trace_path:
        log(f"Trace saved: {trace_path}")
        log("It contains your session cookies and screenshots of the form. Open it with "
            "'playwright show-trace', then delete it. Don't share it.")


def first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else "unknown error"


def safe_url(url: str) -> str:
    # Drop the query string: SAML and SSO hops carry assertions and tokens there.
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else url
