"""Keeping secrets out of Muninn (rule 9: no secrets, minimal content).

Muninn stores error text, event payloads, URLs and commit metadata, and some of those come from
places that can hold a credential: an exception message that echoes a request, a git remote with
a token typed into it, a server address with a user name and password. scrub() masks what looks
like a secret in free text; redact_url() removes the credential part of a URL. Both are applied at
the writer boundary (Run, emit, ensure_source, fail_post, Baldur's collector), so an app can't
forget to.

This is a safety net, not a vault: tokens belong in Windows Credential Manager (or Odin's DPAPI
file) and should never reach a message. Recognition is by shape. Two rules keep it from mangling
ordinary text such as "Add Basic authentication fallback":
- after a strong name (password, secret, passphrase, cookie) anything is masked;
- after a weaker one (token, key, Bearer, Basic, Authorization) written as name=value anything is
  masked too; written "name: value" or "Bearer value", only a value shaped like a credential is:
  it has a digit, or mixes upper and lower case over 12+ characters, or is 20+ characters long.
An opaque secret typed into prose with no such name before it isn't caught.
"""
from __future__ import annotations

import re
from typing import Any, Optional

MASK = "***"

_STRONG = r"(?:password|passwd|pwd|passphrase|secret|client[_-]?secret|private[_-]?key)"
# A name may carry a prefix: GITHUB_TOKEN, JIRA_PAT, auth_token, X-Api-Key. "pat" only with one,
# so "Pat: 1:1 notes" stays as it is.
_PREFIX = r"(?:[A-Za-z0-9]+[_\-.])*"
_WEAK = (r"(?:token|api[_-]?key|apikey|access[_-]?key|auth|authorization|credentials?|session[_-]?id"
         r"|(?:[A-Za-z0-9]+[_\-.])+pat)")
_VALUE = r"(\"[^\"]*\"|'[^']*'|[^\s,;&\"'}\]]+)"

_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)
_COOKIE = re.compile(r"(?im)\b((?:set-)?cookie\s*:\s*)[^\r\n]+")
# "Authorization: <scheme> <credential>" or "Authorization: <credential>", any scheme.
_AUTH_HEADER = re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:([A-Za-z][A-Za-z0-9\-]*)\s+)?([^\s,;\"']+)")
_SCHEME_TOKEN = re.compile(r"(?i)\b(bearer|basic|token)(\s+)([A-Za-z0-9._~+/=\-]{8,})")
_STRONG_PAIR = re.compile(r"(?i)(?<![A-Za-z0-9])(" + _PREFIX + _STRONG + r"[\"']?\s*[:=]\s*)" + _VALUE)
_WEAK_PAIR = re.compile(r"(?i)(?<![A-Za-z0-9])(" + _PREFIX + _WEAK + r"[\"']?\s*[:=]\s*)" + _VALUE)
_QUERY = re.compile(r"(?i)([?&](?:" + _PREFIX + r"(?:access_token|token|api_key|apikey|key|sig|signature|password|"
                    r"pass|pwd|secret|private_token|auth|code))=)[^&#\s]+")
# An environment-style name and its value separated by a space: "JIRA_TOKEN abc123...".
_ENV_SPACE = re.compile(r"(?<![A-Za-z0-9_])((?:[A-Z][A-Z0-9]*_)+(?:TOKEN|PAT|SECRET|PASSWORD|PASSWD|PWD|KEY|APIKEY)"
                        r"\s+)([^\s,;\"']+)")
# curl -u user:password, --user user:password
_CURL_USER = re.compile(r"((?:^|\s)(?:-u|--user)\s+[\"']?[^\s:\"']+:)([^\s\"']+)")
_KNOWN_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                          r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"
                          r"|xox[abprs]-[A-Za-z0-9\-]{10,})(?![A-Za-z0-9])")
# scheme://userinfo@host: userinfo may hold '@' (a typed password) but not '/' or whitespace.
_URL_USERINFO = re.compile(r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.\-]*://)(?P<info>[^\s/?#]*)@(?=[^\s@/?#]+)")
# user:password@host where a typed password holds '/' (not valid in a URL, but people paste it).
# "host:8443/path@x" is a port and a path, not a password, so a value starting with digits and
# a '/' is left alone.
_URL_SLASH_PASSWORD = re.compile(r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^\s/:@?#]+):"
                                 r"(?!\d+(?:/|$))(?P<pw>[^\s@?#]*/[^\s@?#]*)@(?=[A-Za-z0-9.\-]+(?:[:/?#]|$|\s))")
_TOKEN_USER = re.compile(r"^(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|[A-Za-z0-9_\-]{32,})$")


def _credential_shaped(value: str) -> bool:
    v = value.strip("\"'")
    if not v:
        return False
    if any(c.isdigit() for c in v) or len(v) >= 20:
        return True
    return len(v) >= 12 and any(c.isupper() for c in v) and any(c.islower() for c in v)


def _mask_value(value: str) -> str:
    if value[:1] in "\"'" and value[-1:] == value[:1] and len(value) >= 2:
        return value[0] + MASK + value[-1]
    return MASK


def _userinfo(m: "re.Match[str]") -> str:
    info = m.group("info")
    if ":" in info:                              # user:password -> keep the user
        return f"{m.group('scheme')}{info.split(':', 1)[0]}:{MASK}@"
    if _TOKEN_USER.match(info):                  # a token typed as the user name
        return f"{m.group('scheme')}{MASK}@"
    return m.group(0)                            # a plain user name such as git


def scrub(text: Optional[str]) -> Optional[str]:
    """text with anything that looks like a credential masked; None stays None."""
    if not text:
        return text
    out = _PRIVATE_KEY.sub(MASK, text)
    out = _COOKIE.sub(lambda m: m.group(1) + MASK, out)
    out = _URL_SLASH_PASSWORD.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:{MASK}@", out)
    out = _URL_USERINFO.sub(_userinfo, out)
    out = _QUERY.sub(lambda m: m.group(1) + MASK, out)
    out = _AUTH_HEADER.sub(lambda m: m.group(1) + (m.group(2) + " " if m.group(2) else "") + MASK
                           if _credential_shaped(m.group(3)) else m.group(0), out)
    out = _SCHEME_TOKEN.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}" if _credential_shaped(m.group(3))
                            else m.group(0), out)
    out = _ENV_SPACE.sub(lambda m: m.group(1) + MASK if _credential_shaped(m.group(2)) else m.group(0), out)
    out = _CURL_USER.sub(lambda m: m.group(1) + MASK, out)
    out = _STRONG_PAIR.sub(lambda m: m.group(1) + _mask_value(m.group(2)), out)
    # name=value (config, environment, query style) is masked whatever the value; "name: value"
    # reads like prose, so there the value must look like a credential.
    out = _WEAK_PAIR.sub(lambda m: m.group(1) + _mask_value(m.group(2))
                         if m.group(1).rstrip().endswith("=") or _credential_shaped(m.group(2)) else m.group(0), out)
    return _KNOWN_TOKEN.sub(MASK, out)


# Payload fields copied from a source system's own record (a Jira summary, a commit subject).
# They are stored unscrubbed in their own tables, so masking the copy protects nothing, and it
# would turn "Password: 15-character minimum" into "Password: ***" in the history.
VERBATIM_KEYS = frozenset({"summary", "title", "subject", "status", "from", "to", "key", "resolution"})


def scrub_value(value: Any, verbatim: frozenset = frozenset()) -> Any:
    """scrub() applied to every string inside a JSON-able value, dict keys included.

    Other objects (an exception, bytes, a set) become scrubbed text or lists, so nothing reaches
    json.dumps unscrubbed. Values under a key in `verbatim` (at any depth) are kept as they are,
    if they are plain text or lists of it.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {_key(k): (v if k in verbatim and _plain(v) else scrub_value(v, verbatim)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else value
        return [scrub_value(v, verbatim) for v in items]
    if isinstance(value, (bytes, bytearray)):
        return scrub(bytes(value).decode("utf-8", "replace"))
    return scrub(str(value))


def _plain(v: Any) -> bool:
    return v is None or isinstance(v, (str, int, float, bool)) or (
        isinstance(v, (list, tuple)) and all(x is None or isinstance(x, (str, int, float, bool)) for x in v))


def _key(k: Any) -> Any:
    if k is None or isinstance(k, (bool, int, float)):
        return k
    return scrub(k if isinstance(k, str) else str(k))


def redact_url(url: Optional[str]) -> Optional[str]:
    """url without a password or token in it. A plain user name such as git stays: it isn't secret."""
    if not url:
        return url

    def drop(m: "re.Match[str]") -> str:
        info = m.group("info")
        if ":" in info:
            user = info.split(":", 1)[0]
            return f"{m.group('scheme')}{user}@" if user and not _TOKEN_USER.match(user) else m.group("scheme")
        return m.group("scheme") if _TOKEN_USER.match(info) else m.group(0)

    out = _URL_SLASH_PASSWORD.sub(lambda m: f"{m.group('scheme')}{m.group('user')}@", url)
    out = _URL_USERINFO.sub(drop, out)
    return scrub(out)
