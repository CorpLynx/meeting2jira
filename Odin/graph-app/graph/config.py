"""Configuration for the Graph path: national clouds, graph.json, validation.

Why a separate config file rather than a section in the app's config.json
    `app/` is the deliverable and nothing may change inside it to accommodate this exporter - that
    is the rule that keeps app/ stdlib-only and its guardrail tests meaningful. Adding a `graph`
    section would mean editing config.DEFAULTS and validate() in app/src/meeting2jira/config.py.
    So Graph settings live in their own file, in the same per-user directory, and the Jira side of
    the configuration is untouched and still owned by the app.

    One config file per concern, one token store, one state database. Running the COM path and this
    path against the same mailbox still cannot duplicate sub-tasks, because dedupe happens in
    state.db on a source-independent content hash.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_FILE = "graph.json"

# Graph and login hosts per cloud. Getting this wrong in a government tenant does not degrade
# gracefully - the commercial endpoints simply do not hold the mailbox - so the cloud is an explicit
# setting with no autodetection.
CLOUDS: Dict[str, Dict[str, str]] = {
    "commercial": {"graph": "https://graph.microsoft.com",
                   "authority": "https://login.microsoftonline.com"},
    "gcc": {"graph": "https://graph.microsoft.com",
            "authority": "https://login.microsoftonline.com"},
    "gcchigh": {"graph": "https://graph.microsoft.us",
                "authority": "https://login.microsoftonline.us"},
    "dod": {"graph": "https://dod-graph.microsoft.us",
            "authority": "https://login.microsoftonline.us"},
}

# How the token is obtained. Not a cosmetic choice - see auth.py.
AUTH_MODES = ("broker", "interactive", "device_code")

# Least privilege that still answers "what meetings did I attend". ReadBasic is intended to exclude
# meeting bodies and attendee lists, which matches what the COM path already refuses to read.
#
# VERIFY the exact exclusions against current Graph documentation before relying on this in a
# security review; it has not been confirmed against a live tenant. If ReadBasic turns out not to
# carry a field the filters need, Calendars.Read is the fallback - and then the data-minimization
# claim in the README has to be softened to match. Do not quietly switch one for the other.
DEFAULT_SCOPE = "Calendars.ReadBasic"

DEFAULTS: Dict[str, Any] = {
    "_comment": "meeting2jira Graph source. Keys starting with _ are comments.",
    "_client_id": "From IT: the Entra application (client) ID. Public client, no secret.",
    "client_id": "",
    "_tenant_id": "Your tenant GUID, or 'organizations'. Avoid 'common' on a work account.",
    "tenant_id": "organizations",
    "_cloud": "one of: commercial, gcc, gcchigh, dod",
    "cloud": "commercial",
    "_auth_mode": "one of: broker (silent, Windows only), interactive (browser), device_code",
    "auth_mode": "broker",
    "_scope": "Graph delegated permission. Calendars.ReadBasic excludes bodies and attendees.",
    "scope": DEFAULT_SCOPE,
    "_ca_bundle": "Optional PEM bundle, if a TLS-inspecting proxy sits in front of Graph.",
    "ca_bundle": "",
    "_timeout_seconds": "Per-request HTTP timeout.",
    "timeout_seconds": 30,
}


class GraphConfigError(Exception):
    """Configuration is unusable. The message must say what to change."""


def data_dir() -> Path:
    """Per-user state, outside this folder so an upgrade can replace the folder wholesale."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "meeting2jira"


def config_path() -> Path:
    return data_dir() / CONFIG_FILE


def example() -> str:
    return json.dumps(DEFAULTS, indent=2)


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read graph.json merged over DEFAULTS, then validate."""
    path = Path(path) if path else config_path()
    if not path.is_file():
        raise GraphConfigError(
            "No Graph configuration yet at {}.\n"
            "Create it with:  meeting2jira-graph init\n"
            "You will need an Entra application (client) ID from IT - see the README.".format(path))
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise GraphConfigError("{} is not valid JSON: {}".format(path, exc)) from None
    if not isinstance(raw, dict):
        raise GraphConfigError("{} must contain a JSON object.".format(path))

    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in raw.items() if not k.startswith("_")})
    validate(merged)
    return merged


def validate(cfg: Dict[str, Any]) -> None:
    """Reject at load time anything that would otherwise fail later as an opaque HTTP error."""
    client_id = str(cfg.get("client_id") or "").strip()
    if not client_id:
        raise GraphConfigError(
            "client_id is empty. Ask IT to register a public-client Entra application with the "
            "delegated {} permission and redirect URI http://localhost, then put its Application "
            "(client) ID here. See the README for the exact request.".format(DEFAULT_SCOPE))
    # A GUID typo here surfaces as an unhelpful AADSTS error from the token endpoint.
    if len(client_id) != 36 or client_id.count("-") != 4:
        raise GraphConfigError(
            "client_id {!r} does not look like a GUID. It should be the Application (client) ID, "
            "not the application name or the object ID.".format(client_id))

    cloud = str(cfg.get("cloud") or "").strip().lower()
    if cloud not in CLOUDS:
        raise GraphConfigError(
            "cloud {!r} is not one of: {}. GCC High and DoD use different Graph and login hosts, "
            "and the commercial ones simply do not hold the mailbox.".format(
                cfg.get("cloud"), ", ".join(sorted(CLOUDS))))
    cfg["cloud"] = cloud

    mode = str(cfg.get("auth_mode") or "").strip().lower()
    if mode not in AUTH_MODES:
        raise GraphConfigError(
            "auth_mode {!r} is not one of: {}.".format(cfg.get("auth_mode"), ", ".join(AUTH_MODES)))
    cfg["auth_mode"] = mode

    scope = str(cfg.get("scope") or "").strip()
    if not scope:
        raise GraphConfigError("scope is empty; use {} unless IT granted something else.".format(
            DEFAULT_SCOPE))
    if "/" in scope or scope.startswith("http"):
        raise GraphConfigError(
            "scope should be the bare permission name such as {!r}, not a full URL. The cloud's "
            "Graph host is prefixed automatically, which is what makes one config work in "
            "commercial and government tenants alike.".format(DEFAULT_SCOPE))
    cfg["scope"] = scope

    try:
        cfg["timeout_seconds"] = int(cfg.get("timeout_seconds") or 30)
    except (TypeError, ValueError):
        raise GraphConfigError(
            "timeout_seconds {!r} is not a number.".format(cfg.get("timeout_seconds"))) from None
    if cfg["timeout_seconds"] <= 0:
        raise GraphConfigError("timeout_seconds must be greater than zero.")

    bundle = str(cfg.get("ca_bundle") or "").strip()
    if bundle and not Path(bundle).is_file():
        raise GraphConfigError(
            "ca_bundle {!r} does not exist. Export your proxy's chain as Base-64 PEM, or leave the "
            "field empty to use the Windows certificate store.".format(bundle))
    cfg["ca_bundle"] = bundle


def graph_base(cfg: Dict[str, Any]) -> str:
    return CLOUDS[cfg["cloud"]]["graph"]


def authority(cfg: Dict[str, Any]) -> str:
    return "{}/{}".format(CLOUDS[cfg["cloud"]]["authority"], cfg["tenant_id"] or "organizations")


def scopes(cfg: Dict[str, Any]) -> List[str]:
    """Fully-qualified scope list for msal.

    The permission is stored bare and qualified here, so the same graph.json works in a commercial
    tenant and in GCC High without the user having to know that the resource host changes.
    """
    return ["{}/{}".format(graph_base(cfg), cfg["scope"])]
