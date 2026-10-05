"""Baldur's settings: %LOCALAPPDATA%\\Asgard\\settings\\baldur.json.

The file is created with the defaults on first use. A file that won't parse
comes back with broken=True, and every command refuses to run until it's fixed
or deleted: quietly using the defaults could store higher numbers than the
ones you chose. Each estimate run copies the settings it used into
estimate_runs.params, so changing a default never changes past numbers.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from asgard import paths

POLICIES = ("ambient", "overlap", "independent")
REVIEW_MODES = ("off", "metadata", "content")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+$")
# Host names, letters and digits only (punycode for international names); underscores are
# allowed because some internal DNS names have them.
_HOST_RE = re.compile(r"^[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?)*$")
_DOTCOM = ("github.com", "www.github.com", "api.github.com")
_GITHUB_HINT = 'like "github.agency.gov" for GitHub Enterprise Server, or "github.com"'

DEFAULTS: Dict[str, Any] = {
    "repo_roots": [],
    "max_depth": 3,
    "project_keys": [],
    "history_days": 120,
    "idle_gap_minutes": 120,
    "lead_in_minutes": 30,
    "min_session_minutes": 15,
    "max_session_minutes": 240,
    "max_daily_dev_minutes": 480,
    "policy": "ambient",
    "ambient_weight": 0.5,
    "concurrent_fraction": 0.5,
    "round_to_minutes": 15,
    "tour_of_duty": {"start": "07:30", "end": "16:00", "unpaid_minutes": 30},
    "github_api": "",
    "poll_minutes": 5,
    "alerts": True,
    "quiet_outside_tour": True,
    "run_at_logon": False,
    "commit_hook": False,
    "review_mode": "off",
}

# The settings each estimate run records in estimate_runs.params.
ESTIMATE_KEYS = ("project_keys", "idle_gap_minutes", "lead_in_minutes", "min_session_minutes",
                 "max_session_minutes", "max_daily_dev_minutes", "policy", "ambient_weight",
                 "concurrent_fraction", "round_to_minutes", "tour_of_duty")
# The ones that change the numbers; only these go into basis_hash, so editing your
# tour of duty (which only sets flags) doesn't replace every open proposal.
HASH_KEYS = ("idle_gap_minutes", "lead_in_minutes", "min_session_minutes", "max_session_minutes",
             "max_daily_dev_minutes", "policy", "ambient_weight", "concurrent_fraction", "round_to_minutes")


class SettingsError(ValueError):
    """A setting has a value Baldur can't use. The message names it."""


def settings_path() -> Path:
    return paths.data_dir() / "settings" / "baldur.json"


@dataclass
class Settings:
    values: Dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    warnings: List[str] = field(default_factory=list)
    broken: bool = False       # the file exists but couldn't be used; callers refuse to run

    def __getattr__(self, name: str) -> Any:
        values = self.__dict__.get("values", {})
        if name in values:
            return values[name]
        raise AttributeError(name)

    def estimate_params(self) -> Dict[str, Any]:
        return {k: copy.deepcopy(self.values[k]) for k in ESTIMATE_KEYS}

    def params_hash(self) -> str:
        """Hash of the settings that change estimates (HASH_KEYS)."""
        text = json.dumps({k: self.values[k] for k in HASH_KEYS}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def tour_minutes(self) -> int:
        """Paid minutes in the tour of duty: 07:30-16:00 with 30 unpaid is 480."""
        tour = self.values["tour_of_duty"]
        (sh, sm), (eh, em) = (map(int, tour["start"].split(":")), map(int, tour["end"].split(":")))
        return max(0, (eh * 60 + em) - (sh * 60 + sm) - int(tour.get("unpaid_minutes", 0)))

    def tour_bounds(self) -> Tuple[int, int]:
        """Start and end of the tour of duty, in minutes after local midnight."""
        tour = self.values["tour_of_duty"]
        sh, sm = map(int, tour["start"].split(":"))
        eh, em = map(int, tour["end"].split(":"))
        return sh * 60 + sm, eh * 60 + em

    def github_host(self) -> Optional[str]:
        """The web host whose remotes are GitHub repositories, or None when GitHub is off."""
        return github_host(self.values["github_api"])


def github_api_url(value: Any) -> str:
    """The REST API address for what you typed in github_api; "" stays "" (GitHub off).

    GitHub Enterprise Server needs only its host: github.agency.gov becomes
    https://github.agency.gov/api/v3. github.com becomes https://api.github.com,
    and a GHE.com address (octocorp.ghe.com) becomes https://api.octocorp.ghe.com.
    The result is stable: passing it in again gives it back. Only https is
    accepted, and an address carrying a user name or token is refused, so a
    secret can't end up in baldur.json or in estimate_runs.params.
    """
    # Parsed by hand rather than with urllib.parse, whose answers differ between Python
    # versions (ports such as "+443") and whose ValueErrors repeat the address, token included.
    # Messages here never repeat what was typed.
    if not isinstance(value, str):
        raise SettingsError(f"github_api must be text, {_GITHUB_HINT}")
    text = value.strip()
    if not text:
        return ""
    scheme, sep, rest = text.partition("://")
    if not sep:
        scheme, rest = "https", text
    if scheme.lower() != "https":
        raise SettingsError(f"github_api must use https, {_GITHUB_HINT}")
    netloc, slash, tail = rest.partition("/")
    path = (slash + tail).rstrip("/")
    if "@" in netloc:
        raise SettingsError(f"github_api must not hold a user name or token; remove it, {_GITHUB_HINT}")
    host, colon, port_text = netloc.rpartition(":") if ":" in netloc else (netloc, "", "")
    host = host.lower()
    if "?" in rest or "#" in rest or not _HOST_RE.fullmatch(host):    # fullmatch: $ would allow a final \n
        raise SettingsError(f"github_api should be just your GitHub's address, {_GITHUB_HINT}")
    port: Optional[int] = None
    if colon:
        if not re.fullmatch(r"[0-9]{1,5}", port_text) or not 1 <= int(port_text) <= 65535:
            raise SettingsError(f"github_api has a port that isn't a number from 1 to 65535, {_GITHUB_HINT}")
        port = None if int(port_text) == 443 else int(port_text)
    if host in _DOTCOM or host.endswith(".github.com"):
        if host not in _DOTCOM or path or port is not None:
            raise SettingsError('github_api for github.com is just "github.com"')
        return "https://api.github.com"
    if host == "ghe.com" or host.endswith(".ghe.com"):
        tenant = host[4:] if host.startswith("api.") else host
        label = tenant[:-len(".ghe.com")] if tenant.endswith(".ghe.com") else ""
        if not label or "." in label or label == "api" or path or port is not None:
            raise SettingsError('github_api for GHE.com is your address, like "octocorp.ghe.com"')
        return f"https://api.{tenant}"
    if path not in ("", "/api/v3"):
        raise SettingsError(f"github_api should be the server's address without a path, {_GITHUB_HINT}")
    return f"https://{host if port is None else f'{host}:{port}'}/api/v3"


def github_host(api: str) -> Optional[str]:
    """The web host your remotes name, for any github_api value; None when it's off or unusable."""
    try:
        url = github_api_url(api)
    except SettingsError:
        return None
    if not url:
        return None
    host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
    if host == "api.github.com":
        return "github.com"
    if host.endswith(".ghe.com"):
        return host[len("api."):]
    return host


def validate(values: Dict[str, Any]) -> Dict[str, Any]:
    """Return a checked copy of values, or raise SettingsError naming the first bad setting."""
    v = copy.deepcopy(DEFAULTS)
    v.update(values)

    def number(name: str, low: float, high: float, integer: bool = True) -> None:
        x = v[name]
        if isinstance(x, bool) or not isinstance(x, (int, float)) or (integer and int(x) != x):
            raise SettingsError(f"{name} must be a {'whole ' if integer else ''}number")
        if not low <= x <= high:
            raise SettingsError(f"{name} must be between {low:g} and {high:g}")
        v[name] = int(x) if integer else float(x)

    if not isinstance(v["repo_roots"], list) or not all(isinstance(r, str) for r in v["repo_roots"]):
        raise SettingsError('repo_roots must be a list of folders, like ["C:\\\\src"]')
    keys = v["project_keys"]
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise SettingsError('project_keys must be a list of Jira project keys, like ["PROJ", "OPS"]')
    v["project_keys"] = sorted({k.strip().upper() for k in keys if k.strip()})
    bad = [k for k in v["project_keys"] if not _KEY_RE.match(k)]
    if bad:
        raise SettingsError(f"project_keys has {bad[0]!r}, which isn't a Jira project key")
    number("max_depth", 0, 8)
    number("history_days", 1, 3650)
    number("idle_gap_minutes", 5, 24 * 60)
    number("lead_in_minutes", 0, 240)
    number("min_session_minutes", 0, 240)
    number("max_session_minutes", 15, 24 * 60)
    number("max_daily_dev_minutes", 0, 24 * 60)
    number("round_to_minutes", 1, 120)
    number("ambient_weight", 0, 1, integer=False)
    number("concurrent_fraction", 0, 1, integer=False)
    number("poll_minutes", 1, 1440)
    if v["min_session_minutes"] > v["max_session_minutes"]:
        raise SettingsError("min_session_minutes can't be more than max_session_minutes")
    if v["policy"] not in POLICIES:
        raise SettingsError(f"policy must be one of {', '.join(POLICIES)}")
    if v["review_mode"] not in REVIEW_MODES:
        raise SettingsError(f"review_mode must be one of {', '.join(REVIEW_MODES)}")
    tour = v["tour_of_duty"]
    if (not isinstance(tour, dict) or not _TIME_RE.match(str(tour.get("start", "")))
            or not _TIME_RE.match(str(tour.get("end", ""))) or tour["start"] >= tour["end"]):
        raise SettingsError('tour_of_duty needs "start" and "end" like "07:30" and "16:00", start first')
    unpaid = tour.get("unpaid_minutes", 0)
    if isinstance(unpaid, bool) or not isinstance(unpaid, int) or not 0 <= unpaid <= 240:
        raise SettingsError("tour_of_duty.unpaid_minutes must be a whole number from 0 to 240")
    for flag in ("alerts", "quiet_outside_tour", "run_at_logon", "commit_hook"):
        if not isinstance(v[flag], bool):
            raise SettingsError(f"{flag} must be true or false")
    v["github_api"] = github_api_url(v["github_api"])
    return v


def load(path: Optional[Path] = None, create: bool = True) -> Settings:
    """Read baldur.json, creating it with the defaults the first time."""
    path = path or settings_path()
    if not path.exists():
        if create:
            save(DEFAULTS, path)
        return Settings()
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise SettingsError("the file must hold a JSON object {...}")
        unknown = sorted(set(data) - set(DEFAULTS) - {"_help"})
        given = {k: v for k, v in data.items() if k in DEFAULTS}
        github_problem = None
        if "github_api" in given:
            # github_api never changes a number, so a bad one turns GitHub off rather than
            # stopping estimates and approvals the way a bad estimating setting must.
            try:
                given["github_api"] = github_api_url(given["github_api"])
            except SettingsError as exc:
                given["github_api"], github_problem = "", str(exc)
        settings = Settings(validate(given))
        if github_problem:
            settings.warnings.append(f"baldur.json: {github_problem}. Pull request features are off until "
                                     "it's fixed; estimates aren't affected")
        if unknown:
            settings.warnings.append(f"baldur.json: ignored unknown settings {', '.join(unknown)}")
        return settings
    except (OSError, ValueError) as exc:  # json.JSONDecodeError and SettingsError are ValueErrors
        return Settings(warnings=[f"couldn't use {path.name}: {exc}"], broken=True)


def save(values: Dict[str, Any], path: Optional[Path] = None) -> Path:
    path = path or settings_path()
    data = {"_help": "Baldur's settings. See the Baldur spec for what each one does."}
    data.update(validate(values))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\r\n" if os.name == "nt" else "\n") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def update(changes: Dict[str, Any], path: Optional[Path] = None) -> Settings:
    """Change some settings, keeping the rest of the file."""
    path = path or settings_path()
    loaded = load(path)
    if loaded.broken:
        raise SettingsError(f"{path} has a mistake in it; fix or delete it first ({loaded.warnings[0]})")
    current = loaded.values
    current.update(changes)
    save(current, path)
    return load(path)
