"""Minimal Jira REST v2 client for Jira Data Center, using only the standard library.

Position in the flow
    The only module that talks to the network. sync.py and __main__.py call it; it knows nothing
    about meetings, filters, or state. Tests drive it against a local http.server, never real Jira.

Why urllib instead of requests
    On Windows, ssl.create_default_context() loads the Windows certificate store, so an agency root
    CA or TLS-inspection certificate that Windows already trusts is trusted here too. `requests`
    ships its own CA bundle (certifi) and typically fails behind TLS inspection. Proxy settings come
    from the Windows user settings (static proxy; PAC scripts are not evaluated).

Authentication
    Data Center personal access token, sent as a bearer header. There is no Cloud/basic-auth path.

Retry policy, which is a correctness matter rather than a convenience
    * GET is safe to repeat, so it retries 429, 502, 503 and 504.
    * Anything else retries only 429, which Jira rejects before processing. A 502/503/504 or a
      timeout on a POST may mean Jira applied the write and the response was lost on the way back;
      retrying that would create a duplicate issue.
    * Such failures are raised with `ambiguous=True` so the caller can look before leaping. See
      sync.recover_created_issue.

Errors carry advice
    _STATUS_HINTS and _network_hint turn bare HTTP codes into something actionable, and an HTML
    response is reported as "an SSO page is probably intercepting API calls" rather than a JSON
    parse error, because that is what it almost always means on a federal network.
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

from . import __version__

log = logging.getLogger(__name__)

_RETRY_CODES = (429, 502, 503, 504)
# Gateway/proxy failures: the request may or may not have reached Jira and been applied.
_AMBIGUOUS_CODES = (502, 503, 504)
# A 502/503/504 or timeout on POST is ambiguous: Jira may have created the issue before the proxy gave
# up. Retrying would risk a duplicate, so non-GET calls only retry 429, which is rejected before processing.
_RETRY_CODES_UNSAFE_METHODS = (429,)
_STATUS_HINTS = {
    401: " (personal access token rejected: expired, revoked, or for a different Jira. Create a new one under Profile > Personal Access Tokens, then run set-token again)",
    403: " (authenticated but not allowed; after several failed logins Jira may require a CAPTCHA - log in once in a browser)",
    404: " (check jira.base_url and the issue/project key)",
}


class JiraError(Exception):
    """A failed Jira call.

    ambiguous=True means the request may have been applied server-side anyway: a proxy timeout,
    a connection reset, or a 502/503/504 on a non-GET. The caller must not simply retry such a
    call; for issue creation it has to look for the issue first (see sync.recover_created_issue).
    """

    def __init__(self, message: str, status: Optional[int] = None, ambiguous: bool = False):
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - best effort only
        return exc.reason or ""
    try:
        data = json.loads(body)
        parts = list(data.get("errorMessages") or [])
        parts += [f"{k}: {v}" for k, v in (data.get("errors") or {}).items()]
        if parts:
            return "; ".join(parts)
    except (ValueError, AttributeError):
        pass
    return body[:300].strip() or str(exc.reason)


def _network_hint(reason: Any) -> str:
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (" - the server certificate isn't trusted by Python. If your agency CA isn't in the "
                "Windows certificate store, export the chain as PEM and set jira.ca_bundle.")
    if isinstance(reason, ssl.SSLError):
        return (" - TLS handshake failed. If Jira requires a CAC/PIV client certificate, ask the Jira "
                "admins which endpoint accepts personal access tokens.")
    return " - check jira.base_url, VPN, and proxy settings (jira.proxy)."


class JiraClient:
    def __init__(self, base_url: str, token: str, ca_bundle: Optional[str] = None,
                 proxy: Optional[str] = None, timeout: float = 30, max_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._headers = {
            # Data Center personal access tokens are bearer tokens. There is no basic-auth path.
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"meeting2jira/{__version__}",
        }
        context = ssl.create_default_context()      # includes the Windows cert store
        if ca_bundle:
            context.load_verify_locations(cafile=ca_bundle)
        handlers: list = [urllib.request.HTTPSHandler(context=context)]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self._opener = urllib.request.build_opener(*handlers)

    @classmethod
    def from_config(cls, jcfg: Dict[str, Any], token: str) -> "JiraClient":
        return cls(jcfg["base_url"], token, ca_bundle=jcfg.get("ca_bundle"),
                   proxy=jcfg.get("proxy"), timeout=float(jcfg.get("timeout_seconds") or 30))

    # ---- transport -------------------------------------------------------------------------
    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, method=method, headers=self._headers)
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    content_type = resp.headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                detail = _error_detail(exc)
                retryable = _RETRY_CODES if method == "GET" else _RETRY_CODES_UNSAFE_METHODS
                if exc.code in retryable and attempt < self.max_retries:
                    delay = self._retry_delay(exc, attempt)
                    log.warning("Jira returned %s; retrying in %ss", exc.code, delay)
                    time.sleep(delay)
                    attempt += 1
                    continue
                hint = _STATUS_HINTS.get(exc.code, "")
                # A gateway-level 5xx on a write may mean Jira applied it and the response was
                # lost on the way back. GETs change nothing, so they are never ambiguous.
                ambiguous = method != "GET" and exc.code in _AMBIGUOUS_CODES
                raise JiraError(f"{method} {path} -> HTTP {exc.code}: {detail}{hint}", exc.code,
                                ambiguous=ambiguous) from None
            except urllib.error.URLError as exc:
                raise JiraError(f"{method} {path} failed: {exc.reason}{_network_hint(exc.reason)}",
                                ambiguous=method != "GET") from None
            except OSError as exc:   # timeouts and resets during read
                raise JiraError(f"{method} {path} failed: {exc}{_network_hint(exc)}",
                                ambiguous=method != "GET") from None

            if not raw:
                return None
            if "json" not in content_type.lower():
                raise JiraError(
                    f"{method} {path} returned '{content_type or 'unknown'}' instead of JSON. An SSO login "
                    "page or web proxy is probably intercepting API calls; ask the Jira admins which URL "
                    "accepts token-authenticated REST calls."
                )
            return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
        header = exc.headers.get("Retry-After") if exc.headers else None
        try:
            return min(60.0, float(header)) if header else float(2 ** (attempt + 1))
        except ValueError:
            return float(2 ** (attempt + 1))

    # ---- API ---------------------------------------------------------------------------------
    def server_info(self) -> Dict[str, Any]:
        return self.request("GET", "/rest/api/2/serverInfo")

    def myself(self) -> Dict[str, Any]:
        return self.request("GET", "/rest/api/2/myself")

    def get_issue(self, key: str, fields: str = "summary,issuetype,project,status") -> Dict[str, Any]:
        return self.request("GET", f"/rest/api/2/issue/{quote(key)}?fields={fields}")

    def get_project(self, key: str) -> Dict[str, Any]:
        return self.request("GET", f"/rest/api/2/project/{quote(key)}")

    def create_issue(self, fields: Dict[str, Any]) -> str:
        return self.request("POST", "/rest/api/2/issue", {"fields": fields})["key"]

    def personal_access_tokens(self) -> Optional[List[Dict[str, Any]]]:
        """The current user's personal access tokens, or None if this Jira does not expose them.

        Data Center only, and only reasonably recent versions: the endpoint arrived with PATs and is
        absent or restricted elsewhere. Returns None rather than raising for every "not available"
        case, because a token-expiry warning is a nicety and must never be the reason a sync fails.

        Note what this cannot do: the response never echoes the token itself, so there is no way to
        tell which entry is the one in use. The caller has to say so rather than implying certainty.
        """
        try:
            data = self.request("GET", "/rest/pat/latest/tokens")
        except JiraError as exc:
            # 404 = version predates the endpoint. 403/401 = present but not permitted here.
            if exc.status in (401, 403, 404, 405):
                log.debug("This Jira does not expose personal access tokens (HTTP %s)", exc.status)
                return None
            raise
        if isinstance(data, dict):
            data = data.get("values") or data.get("tokens") or []
        if not isinstance(data, list):
            return None
        return [token for token in data if isinstance(token, dict)]

    def search_issue_keys(self, jql: str, max_results: int = 5) -> List[str]:
        """Return issue keys matching a JQL query.

        Used to find out whether an ambiguous create actually landed. GET is deliberate: it is
        safe to retry, unlike the POST form of /search.
        """
        query = urlencode({"jql": jql, "fields": "key", "maxResults": max_results})
        data = self.request("GET", f"/rest/api/2/search?{query}") or {}
        return [str(issue["key"]) for issue in data.get("issues") or [] if issue.get("key")]

    def add_worklog(self, key: str, seconds: int, started: datetime, comment: str) -> Optional[str]:
        """Log time against an issue. Returns the new worklog's id when Jira reports one.

        timeSpentSeconds is the meeting's real duration; Jira rejects 0, so it is floored at
        one minute.
        """
        body = {
            "timeSpentSeconds": max(60, int(seconds)),
            "started": started.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
            "comment": comment,
        }
        created = self.request("POST", f"/rest/api/2/issue/{quote(key)}/worklog", body) or {}
        worklog_id = created.get("id") if isinstance(created, dict) else None
        return str(worklog_id) if worklog_id is not None else None

    def transition(self, key: str, name: str) -> bool:
        data = self.request("GET", f"/rest/api/2/issue/{quote(key)}/transitions") or {}
        wanted = name.strip().lower()
        for t in data.get("transitions", []):
            names = {str(t.get("name", "")).lower(), str((t.get("to") or {}).get("name", "")).lower()}
            if wanted in names:
                self.request("POST", f"/rest/api/2/issue/{quote(key)}/transitions", {"transition": {"id": t["id"]}})
                return True
        return False
