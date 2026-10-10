"""Configuration: the defaults, the loader, and the validator.

Position in the flow
    Read first by every CLI command. `load_config` merges the user's JSON over DEFAULTS and
    validates the result, so nothing downstream has to defend against a missing or malformed key.

What lives here
    * DEFAULTS            every setting and its default value, with the reasoning inline
    * load_config/         merge + validate, returning a plain dict with "data_dir" added
      build_config
    * validate            collects *all* problems and raises one ConfigError listing them, because
                          fixing config one error per run is miserable
    * parse_hhmm          shared by validation and rules.TourOfDuty; it lives here rather than in
                          rules.py because rules imports config and the reverse would be circular

Conventions
    * Keys beginning with "_" are comments and are stripped before merging, which is how
      config.example.json documents itself inside valid JSON.
    * Anything omitted falls back to DEFAULTS, so a user's config can be as small as two keys.
    * Validation messages must say what to change, not just what is wrong. A misconfiguration
      caught here is worth ten cryptic failures later against live Jira.

Scope
    Jira Data Center only. There is no Cloud/basic-auth path; a leftover `auth` or `email` key is
    reported as an error rather than ignored.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from asgard import paths as asgard_paths

# The key rule Muninn enforces on every key column: a number that doesn't start with 0.
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[1-9]\d*$")

# Filter keys that hold a list of plain substrings. Kept in one place so config validation,
# the router, and the README stay in step.
TEXT_FILTER_KEYS = (
    "skip_subject_contains",
    "skip_organizer_contains",
    "skip_location_contains",
    "skip_categories",
)

DEFAULTS: Dict[str, Any] = {
    "jira": {
        # Jira Data Center only. Authentication is always a personal access token sent as a
        # bearer header; there is no Cloud/basic-auth path to choose between.
        "base_url": "",
        "default_parent": "",
        "subtask_type": "Sub-task",
        "labels": ["meeting"],
        "assign_to_me": True,
        "log_work": False,
        "transition_to": None,         # e.g. "Done"; None leaves the sub-task in its initial status
        # Adds a deterministic label (m2j-<first 10 chars of the meeting's content hash>) to every
        # sub-task. It is what makes recovery from an ambiguous create exact: if a create times
        # out, the next step is a JQL lookup for that label rather than a guess based on the
        # summary text. Turn it off only if label clutter is unacceptable; recovery is then
        # skipped and an ambiguous create is reported for you to resolve by hand.
        "dedupe_label": True,
        "extra_fields": {},            # merged into the create payload (required custom fields, etc.)
        "max_creates_per_run": 40,     # safety valve against a runaway first run
        # Warn when the personal access token is close to expiring, so the first sign is not a run
        # of 401s. Jira Data Center exposes /rest/pat/latest/tokens; older versions and some
        # configurations do not, in which case the check quietly does nothing. 0 disables it.
        "warn_token_expiry_days": 14,
        "ca_bundle": None,             # extra PEM bundle; Windows cert store is always used
        "proxy": None,                 # e.g. "http://proxy.agency.gov:8080"; None = Windows settings
        "timeout_seconds": 30,
    },
    "filters": {
        "meetings_only": True,         # drop personal appointments with no attendees
        "teams_only": False,
        "only_ended": True,            # never log a meeting that hasn't finished yet
        "skip_cancelled": True,
        "skip_declined": True,
        "skip_not_responded": False,
        "skip_tentative": False,
        "skip_free": True,
        "skip_private": True,
        "skip_all_day": True,
        "min_minutes": 5,
        "max_minutes": 480,
        # Two ways to exclude by text, both optional:
        #   *_contains      plain case-insensitive substrings. Easy to read and edit, but they
        #                   match inside words ("PTO" also matches "OPTOMETRIST").
        #   skip_subject_patterns
        #                   regular expressions, for when you need word boundaries or anchors,
        #                   e.g. "(?i)\\bPTO\\b".
        "skip_subject_contains": [],
        "skip_organizer_contains": [],
        "skip_location_contains": [],
        "skip_categories": [],          # whole category names, compared case-insensitively
        "skip_subject_patterns": [],
    },
    # Making a hidden failure visible.
    #
    # A scheduled task runs with no window. last_run.json plus `status` and `doctor` already record
    # what happened, but all three require the user to go and look. These settings push a failure
    # into view instead.
    #
    # What is deliberately NOT here, and why:
    #   * Windows toast notifications need WinRT type loading, which Constrained Language Mode
    #     blocks outright - and the orchestrator has to stay CLM-safe.
    #   * BurntToast and friends need the PowerShell Gallery, which is unavailable.
    #   * mshta.exe can raise a dialog with no dependencies, but it is a well-known
    #     living-off-the-land binary that endpoint protection and AppLocker commonly block. Using it
    #     would make this tool look like the thing the controls exist to stop.
    # What is left is unglamorous and works everywhere: leave a file where the user will see it.
    "notify": {
        "desktop_alert": True,         # write ATTENTION-Odin.txt to the Desktop on failure
        "alert_after_failures": 1,     # consecutive failed runs before alerting; 2 rides out a blip
        "use_msg_exe": False,          # additionally try msg.exe, which is absent on some builds
    },
    # Tour of duty: your scheduled working hours, in local wall-clock time.
    #
    # This classifies meetings; it deliberately does NOT decide which days get scanned. The scan
    # window is already safe to widen because the state database makes re-runs idempotent, so
    # "did I miss a late meeting" is answered by -DaysBack, not by this.
    #
    # A meeting is "inside" when it falls entirely within the window, "outside" when it does not
    # touch it at all, and "partial" when it straddles the edge. Only wholly-outside meetings
    # trigger outside_action; a partial meeting is treated as inside, because work that ran past
    # the end of your tour is still work and should not be silently dropped.
    "tour_of_duty": {
        "enabled": False,
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "start": "07:00",              # 24-hour local time
        "end": "15:30",
        "grace_minutes": 15,           # tolerate a meeting starting just before / ending just after
        "outside_action": "label",     # include | label | route | skip
        "outside_label": "outside-tod",
        "outside_parent": None,        # required when outside_action is "route"
    },
    # Muninn, Asgard's shared database. Odin keeps Jira there for the other apps, and posts the time
    # you approve in Baldur. Each part can be turned off; the meeting push always uses Muninn.
    "muninn": {
        "sync_issues": True,           # your issues, tracked parents and their children, keys others mention
        "sync_worklogs": True,         # your worklogs, so Baldur sees what Jira already holds
        # Post the days you approve in Baldur that Jira is missing. Only approved minutes, only what
        # Jira doesn't hold yet (which is why it needs the worklog sync), at most max_posts_per_run.
        "post_approved": True,
        "max_posts_per_run": 20,
        "history_days": 365,           # how far back the first sync reaches
        "max_issues_per_run": 500,     # per stream; a bigger first sync carries on next run
    },
    "rules": [],
    "templates": {
        "summary": "Meeting: {subject} ({start_local:%Y-%m-%d %H:%M})",
        "description": (
            "Logged automatically from my calendar by Odin.\n\n"
            "When: {start_local:%Y-%m-%d %H:%M} - {end_local:%H:%M} ({minutes} min)\n"
            "Organizer: {organizer}\n"
            "Location: {location}"
        ),
        "worklog_comment": "Meeting: {subject}",
    },
    "csv": {
        "datetime_formats": [
            "%m/%d/%Y %I:%M:%S %p",
            "%m/%d/%Y %I:%M %p",
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%Y %H:%M",
        ],
    },
}


class ConfigError(Exception):
    """Raised for missing or invalid configuration."""


def asgard_dir() -> Path:
    """Asgard's per-user folder (asgard.paths): ASGARD_HOME when set, else %LOCALAPPDATA%\\Asgard."""
    return asgard_paths.data_dir()


def legacy_data_dirs() -> List[Path]:
    """Where Odin kept its files before they moved under Asgard (Oct 2026), newest naming first.

    Two folders came before %LOCALAPPDATA%\\Asgard\\odin: the on-premises install kept its files in
    %LOCALAPPDATA%\\odin (and before that, %LOCALAPPDATA%\\meeting2jira). Both are looked for: missing
    one leaves its state.db behind, and a run with no history re-creates every meeting ever synced.
    """
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return [Path(base) / "odin", Path(base) / "meeting2jira"]
    return [Path.home() / ".meeting2jira", Path.home() / "meeting2jira"]


def move_legacy_data(target: Path) -> Optional[Path]:
    """Move Odin's old folder to target, once. Returns the folder moved, if any.

    One rename on the same drive, so everything comes across as it was: config, state.db, logs,
    exports, and the DPAPI token files, which open for the same Windows user wherever they sit.
    Nothing happens when target already exists, or under ASGARD_HOME (tests point that at a
    temporary folder, and must never move a real one).
    """
    if os.environ.get("ASGARD_HOME"):
        return None
    if target.exists():
        # Something made the new folder before the move (by hand, or an old script). The history must
        # never be left behind: without state.db the next run re-creates every meeting ever synced.
        for old in legacy_data_dirs():
            if (old / "state.db").is_file() and not (target / "state.db").exists():
                raise ConfigError(
                    f"Odin's history (state.db) is still in {old}, but its files now live in {target}, "
                    f"which doesn't have it. Move everything from {old} into {target} (or delete {target} "
                    "if it holds nothing you need), then run again. Running now would re-create every "
                    "meeting already synced.")
        return None
    present = [old for old in legacy_data_dirs() if old.is_dir()]
    if len(present) > 1:
        # Two old folders, so which one holds the history is a guess. Guessing wrong leaves a state.db
        # behind, and the first run without it re-creates every meeting already synced.
        where = " and ".join(str(p) for p in present)
        raise ConfigError(
            f"Odin found two of its old folders ({where}) and can't tell which to keep. Move the one "
            f"with the newest state.db to {target} yourself, check the other holds nothing you need, "
            "then run again.")
    for old in present:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(old, target)
        except OSError as exc:
            raise ConfigError(
                f"Odin's files are moving to {target}, but {old} couldn't be moved ({exc}). Close "
                "anything using it (Odin's window, a sync that's running, a log open in an editor) and "
                "run again.") from None
        return old
    return None


def default_data_dir() -> Path:
    """Odin's files: %LOCALAPPDATA%\\Asgard\\odin, beside Asgard's (ASGARD_HOME\\odin when set).

    Odin is an Asgard app: its config, token, logs and exports sit beside Asgard's own files, and
    its records are in Muninn. A folder from before (%LOCALAPPDATA%\\odin on the on-premises
    install, or %LOCALAPPDATA%\\meeting2jira before that) is moved here the first time.
    """
    target = asgard_dir() / "odin"
    move_legacy_data(target)
    return target


def default_config_path() -> Path:
    return default_data_dir() / "config.json"


def _strip_comments(obj: Any) -> Any:
    """Drop keys that start with '_' so the JSON config can carry notes."""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    path = Path(path) if path else default_config_path()
    if not path.is_file():
        raise ConfigError(f"No config file at {path}. Run `odin setup` (or `odin init`) first.")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            user = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    cfg = _merge(DEFAULTS, _strip_comments(user))
    validate(cfg)
    cfg["data_dir"] = str(path.parent)
    return cfg


def build_config(overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Defaults + overrides, validated. Used by tests and tooling."""
    cfg = _merge(DEFAULTS, _strip_comments(overrides))
    validate(cfg)
    return cfg


OUTSIDE_ACTIONS = ("include", "label", "route", "skip")
_WEEKDAY_PREFIXES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_hhmm(value: Any) -> int:
    """'15:30' -> minutes since local midnight. Raises ValueError on anything else.

    Lives here rather than in rules.py so config validation can use it without importing rules,
    which would be circular.
    """
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"expected a 24-hour time like 07:00, got {value!r}")
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"expected a 24-hour time like 07:00, got {value!r}") from None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"{value!r} is not a valid 24-hour time")
    return hours * 60 + minutes


def _tour_of_duty_problems(tod: Dict[str, Any]) -> List[str]:
    """Validate tour_of_duty. Only meaningful when enabled, but bad values are reported either way
    so a typo isn't discovered months later when the setting is finally switched on."""
    problems: List[str] = []
    if not isinstance(tod, dict):
        return ["tour_of_duty must be an object"]

    for key in ("start", "end"):
        try:
            parse_hhmm(tod.get(key))
        except (ValueError, TypeError) as exc:
            problems.append(f"tour_of_duty.{key}: {exc}")

    days = tod.get("days")
    if not isinstance(days, list):
        problems.append('tour_of_duty.days must be a list, e.g. ["Mon","Tue","Wed","Thu","Fri"]')
    else:
        for day in days:
            if not isinstance(day, str) or day.strip()[:3].lower() not in _WEEKDAY_PREFIXES:
                problems.append(f"tour_of_duty.days contains {day!r}; use Mon/Tue/Wed/Thu/Fri/Sat/Sun")
        if tod.get("enabled") and not days:
            problems.append("tour_of_duty.days is empty, so every meeting would count as outside "
                            "your tour. List your working days or set enabled to false.")

    grace = tod.get("grace_minutes")
    if grace is not None and not isinstance(grace, bool):
        try:
            if int(grace) < 0:
                problems.append("tour_of_duty.grace_minutes cannot be negative")
        except (TypeError, ValueError):
            problems.append(f"tour_of_duty.grace_minutes must be a number, got {grace!r}")

    action = tod.get("outside_action")
    if action not in OUTSIDE_ACTIONS:
        problems.append(f"tour_of_duty.outside_action must be one of {', '.join(OUTSIDE_ACTIONS)}, "
                        f"got {action!r}")
    if action == "route" and not ISSUE_KEY_RE.match(str(tod.get("outside_parent") or "")):
        problems.append('tour_of_duty.outside_action is "route", so tour_of_duty.outside_parent '
                        "must be an issue key like PROJ-123 (often a comp-time or overtime issue)")
    if action == "label":
        label = tod.get("outside_label")
        if not isinstance(label, str) or not label.strip():
            problems.append('tour_of_duty.outside_action is "label", so tour_of_duty.outside_label '
                            "must be a non-empty string")
        elif any(ch.isspace() for ch in label):
            problems.append(f"tour_of_duty.outside_label: {label!r} contains a space; Jira labels "
                            f"cannot. Use {'-'.join(label.split())!r}")
    return problems


def _muninn_problems(section: Any) -> List[str]:
    if not isinstance(section, dict):
        return ["muninn must be an object"]
    problems = []
    for key in ("sync_issues", "sync_worklogs", "post_approved"):
        if not isinstance(section.get(key), bool):
            problems.append(f"muninn.{key} must be true or false")
    for key, low, high in (("max_posts_per_run", 0, 500), ("history_days", 1, 3650), ("max_issues_per_run", 1, 10000)):
        try:
            value = int(section.get(key))
        except (TypeError, ValueError):
            continue     # reported as a non-number
        if not low <= value <= high:
            problems.append(f"muninn.{key} must be between {low} and {high}")
    return problems


def validate(cfg: Dict[str, Any]) -> None:
    j = cfg["jira"]
    problems = []

    url = str(j.get("base_url") or "")
    local_http = url.startswith(("http://localhost", "http://127.0.0.1"))
    if not (url.startswith("https://") or local_http):
        problems.append("jira.base_url must be an https:// URL")
    # These are leftovers from when Cloud was supported. Fail loudly rather than ignoring them,
    # so nobody is left believing a Cloud setting is in effect.
    if j.get("auth") not in (None, "bearer"):
        problems.append(f"jira.auth is {j['auth']!r}; this tool supports Jira Data Center only, "
                        "which always uses a bearer personal access token. Remove the key.")
    if j.get("email"):
        problems.append("jira.email only applied to Jira Cloud, which is no longer supported. "
                        "Remove the key and use a Data Center personal access token.")
    if not ISSUE_KEY_RE.match(str(j.get("default_parent") or "")):
        problems.append("jira.default_parent must be an issue key like PROJ-123")
    if not isinstance(j.get("extra_fields"), dict):
        problems.append("jira.extra_fields must be an object")
    if not isinstance(j.get("labels"), list):
        problems.append("jira.labels must be a list")
    else:
        for label in j["labels"]:
            # Jira rejects whitespace in labels with an unhelpful 400 at create time.
            if not isinstance(label, str) or not label.strip():
                problems.append(f"jira.labels contains {label!r}; labels must be non-empty strings")
            elif any(ch.isspace() for ch in label):
                problems.append(f"jira.labels: {label!r} contains a space; Jira labels cannot. "
                                f"Use {'-'.join(label.split())!r}")
    if not str(j.get("subtask_type") or "").strip():
        problems.append("jira.subtask_type is required, e.g. 'Sub-task' (`check` lists the valid names)")

    # Numbers arriving as unparseable strings would otherwise fail deep in a request with a
    # message about int() or float() rather than about the config.
    for section, key, kind in (("jira", "max_creates_per_run", int),
                               ("jira", "timeout_seconds", float),
                               ("filters", "min_minutes", int),
                               ("filters", "max_minutes", int),
                               ("muninn", "max_posts_per_run", int),
                               ("muninn", "history_days", int),
                               ("muninn", "max_issues_per_run", int)):
        value = cfg[section].get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            kind(value)
        except (TypeError, ValueError):
            problems.append(f"{section}.{key} must be a number, got {value!r}")

    # The cap is the safety valve against a runaway run, so there is deliberately no spelling for
    # "unlimited". 0 used to disable it silently, which read like "create nothing" and did the
    # opposite. For a genuine backfill, pass a large --max for that one run.
    try:
        if int(j.get("max_creates_per_run") or 0) < 1:
            problems.append("jira.max_creates_per_run must be at least 1. It is the safety valve "
                            "against a runaway run; for a backfill pass a large --max instead.")
    except (TypeError, ValueError):
        pass   # already reported as a non-number above

    for name, template in (cfg.get("templates") or {}).items():
        if not isinstance(template, str):
            problems.append(f"templates.{name} must be a string, got {template!r}")

    problems.extend(_tour_of_duty_problems(cfg.get("tour_of_duty") or {}))
    problems.extend(_muninn_problems(cfg.get("muninn")))

    if not isinstance(cfg.get("rules"), list):
        problems.append("rules must be a list")
    else:
        for i, rule in enumerate(cfg["rules"]):
            name = (rule.get("name") if isinstance(rule, dict) else None) or f"rules[{i}]"
            if not isinstance(rule, dict) or not isinstance(rule.get("match"), dict):
                problems.append(f"{name}: needs a 'match' object")
            elif rule.get("skip") is not True and not ISSUE_KEY_RE.match(str(rule.get("parent") or "")):
                problems.append(f"{name}: needs either \"skip\": true or a 'parent' issue key")

    for pattern in cfg["filters"].get("skip_subject_patterns") or []:
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"filters.skip_subject_patterns: bad regex {pattern!r}: {exc}")

    # A bare string here is a likely typo: "OOO" would otherwise be treated as the three
    # single-character substrings "O", "O", "O" and skip almost everything.
    for key in TEXT_FILTER_KEYS:
        value = cfg["filters"].get(key)
        if value is None:
            continue
        if isinstance(value, str) or not isinstance(value, list):
            problems.append(f"filters.{key} must be a list of strings, e.g. [\"OOO\", \"PTO\"]")
        elif not all(isinstance(item, str) for item in value):
            problems.append(f"filters.{key} must contain only strings")

    if problems:
        raise ConfigError("Config problems:\n  - " + "\n  - ".join(problems))
