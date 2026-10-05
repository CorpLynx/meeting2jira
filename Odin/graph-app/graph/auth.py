"""Acquire a Graph access token with msal, and cache it under DPAPI.

This is the only file in the project that imports a third-party package, and the import is lazy so
that everything else stays testable without it.

Why msal rather than hand-rolled OAuth
    The earlier plan for this path was a stdlib authorization-code + PKCE flow with a loopback
    listener, written that way only because the project was stdlib-only. With pip permitted for this
    folder, msal removes the PKCE dance, the loopback server, the state/nonce validation and the
    refresh bookkeeping - which is exactly the part of OAuth where hand-written code is subtly wrong
    in ways that still appear to work.

The three modes, and why the choice is not cosmetic
    broker       Uses the Windows account broker (WAM). The token comes from the device's existing
                 primary refresh token, so it is silent, needs no prompt, and the device itself
                 satisfies MFA and device-compliance Conditional Access. For a scheduled task that
                 runs unattended on a managed workstation this is the only mode that reliably works
                 every morning without someone present to click.

                 Cost: the msal[broker] extra pulls pymsalruntime, a NATIVE binary. That is a larger
                 approval surface than pure-Python msal and is a separate conversation with IT. It is
                 the default here because it is the right answer when it is allowed, and it fails
                 with an explanation pointing at the alternatives when it is not.

    interactive  Pure-Python msal opens a browser once; later runs use the cached refresh token.
                 Works with no native dependency. The catch for automation: when Conditional Access
                 eventually forces reauthentication, an unattended run cannot answer, so it fails
                 and the meetings wait for a person. Acceptable, but it must be noticed - which is
                 what last_export.json is for.

    device_code  Prints a code to enter on another device. The fallback when no browser can open on
                 the machine itself. Same unattended caveat as interactive.

Token cache
    msal's SerializableTokenCache holds a refresh token, so it is a secret and is stored exactly the
    way the Jira PAT is: DPAPI, per user, per machine, through ctypes. It is a separate file from the
    Jira token because a refresh token can be large and because the two have different lifetimes.

    Off Windows there is no DPAPI, so the cache is held in memory only and every run re-authenticates.
    That is development behaviour, not a supported deployment, and it refuses to write a plaintext
    fallback rather than silently leaving a refresh token on disk.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from . import config as cfgmod

log = logging.getLogger(__name__)

CACHE_FILE = "graph_token_cache.dpapi"


class AuthError(Exception):
    """Token acquisition failed. The message must say what to do next."""


# ---------------------------------------------------------------------------------------------
# DPAPI, duplicated in spirit from app/src/meeting2jira/credstore.py.
#
# Not imported from there: app/ must remain independently copyable, and this folder must not reach
# into it. The surface used here is small and stable (two calls), so a copy is cheaper than coupling
# two deliverables together.
# ---------------------------------------------------------------------------------------------
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _PBLOB = ctypes.POINTER(_Blob)
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [_PBLOB, wintypes.LPCWSTR, _PBLOB, ctypes.c_void_p,
                                          ctypes.c_void_p, wintypes.DWORD, _PBLOB]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [_PBLOB, ctypes.c_void_p, _PBLOB, ctypes.c_void_p,
                                            ctypes.c_void_p, wintypes.DWORD, _PBLOB]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    def _to_blob(data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _take(blob: "_Blob") -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))

    def _protect(data: bytes) -> bytes:
        blob_in, _keepalive = _to_blob(data)
        blob_out = _Blob()
        if not _crypt32.CryptProtectData(ctypes.byref(blob_in), "meeting2jira-graph", None, None,
                                         None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _take(blob_out)

    def _unprotect(data: bytes) -> bytes:
        blob_in, _keepalive = _to_blob(data)
        blob_out = _Blob()
        if not _crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                           _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _take(blob_out)


def cache_path() -> Path:
    return cfgmod.data_dir() / CACHE_FILE


def _load_msal():
    """Import msal, or explain how to install it. Lazy, so the rest of the package needs no pip."""
    try:
        import msal                      # noqa: WPS433 - deliberately deferred
    except ImportError:
        raise AuthError(
            "msal is not installed. From this folder:\n"
            "  pip install -r requirements.txt\n"
            "If you use an internal mirror:\n"
            "  pip install -i https://your-mirror/simple -r requirements.txt") from None
    return msal


class TokenCache:
    """msal's serializable cache, persisted with DPAPI.

    Deliberately conservative about failure. A cache that cannot be read is not an error - it just
    means the next sign-in is interactive - but a cache that cannot be *written* is reported, because
    silently losing it turns every scheduled run into a prompt nobody is there to answer.
    """

    def __init__(self, path: Optional[Path] = None, enabled: bool = True):
        msal = _load_msal()
        self.path = Path(path) if path else cache_path()
        self.enabled = enabled and sys.platform == "win32"
        self._cache = msal.SerializableTokenCache()
        if enabled and sys.platform != "win32":
            log.warning("Not Windows: the token cache is in memory only, so this run will "
                        "re-authenticate. Expected off the target machine; not a deployment mode.")
        self._read()

    def _read(self) -> None:
        if not self.enabled or not self.path.is_file():
            return
        try:
            self._cache.deserialize(_unprotect(self.path.read_bytes()).decode("utf-8"))
        except Exception as exc:          # noqa: BLE001 - a bad cache must never block sign-in
            log.warning("Ignoring the stored token cache (%s). Re-authenticating. DPAPI data only "
                        "opens for the same Windows user on the same machine.", exc)

    def save(self) -> None:
        if not self.enabled or not self._cache.has_state_changed:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(_protect(self._cache.serialize().encode("utf-8")))
            # Readable only by this user anyway via DPAPI; this is belt and braces on the ACL.
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except Exception as exc:          # noqa: BLE001
            log.warning("Could not save the token cache to %s (%s). The next run will have to "
                        "sign in again.", self.path, exc)

    @property
    def msal_cache(self):
        return self._cache


def _application(cfg: Dict[str, Any], cache: TokenCache, mode: str):
    msal = _load_msal()
    kwargs: Dict[str, Any] = {
        "client_id": cfg["client_id"],
        "authority": cfgmod.authority(cfg),
        "token_cache": cache.msal_cache,
    }
    if mode == "broker":
        # Only passed for broker mode: older msal builds reject the keyword outright, and a
        # TypeError here would otherwise look like a configuration problem.
        kwargs["enable_broker_on_windows"] = True
    try:
        return msal.PublicClientApplication(**kwargs)
    except TypeError as exc:
        if mode != "broker":
            raise
        raise AuthError(
            "This msal build does not support brokered sign-in ({}). Either install the broker "
            "extra:\n"
            "  pip install \"msal[broker]\"\n"
            "or set auth_mode to \"interactive\" in graph.json.".format(exc)) from None


def acquire_token(cfg: Dict[str, Any], interactive_ok: bool = False,
                  cache: Optional[TokenCache] = None) -> str:
    """Return a bearer token for Graph.

    interactive_ok gates every path that needs a human. A scheduled run passes False, so a session
    that has genuinely expired fails loudly instead of hanging forever on a prompt that no one will
    ever see - which is the failure mode that quietly loses days of meetings.
    """
    scopes = cfgmod.scopes(cfg)
    cache = cache if cache is not None else TokenCache()
    mode = cfg["auth_mode"]
    app = _application(cfg, cache, mode)

    # Silent first, always. With the broker this is normally the only step that runs.
    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(scopes, account=accounts[0])
    if result is None and mode == "broker":
        # WAM can mint a token from the device's primary refresh token with no cached account,
        # which is the case on the very first scheduled run after setup.
        try:
            result = app.acquire_token_silent(scopes, account=None)
        except Exception as exc:         # noqa: BLE001 - broker unavailable is not fatal yet
            log.debug("Brokered silent acquisition did not work (%s).", exc)

    if result is None:
        if not interactive_ok:
            raise AuthError(
                "No usable cached token, and this run is non-interactive. Run `meeting2jira-graph "
                "login` once as yourself, then retry. If this happens on a schedule, Conditional "
                "Access is forcing reauthentication: auth_mode \"broker\" avoids that, because the "
                "device itself satisfies the policy.")
        result = _interactive(app, scopes, mode)

    cache.save()

    if "access_token" not in result:
        raise AuthError(_explain(result, cfg))
    return result["access_token"]


def _interactive(app, scopes, mode: str) -> Dict[str, Any]:
    msal = _load_msal()
    if mode == "device_code":
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise AuthError(
                "Could not start the device-code flow: {}. The application may not be configured "
                "as a public client.".format(flow.get("error_description") or flow))
        # Printed, not logged: the user has to read and act on it right now.
        print(flow["message"], flush=True)
        return app.acquire_token_by_device_flow(flow)

    kwargs: Dict[str, Any] = {"scopes": scopes}
    if mode == "broker":
        # The broker needs a parent window to anchor its dialog to. With no GUI of our own, msal's
        # console handle is the documented stand-in.
        handle = getattr(msal.PublicClientApplication, "CONSOLE_WINDOW_HANDLE", None)
        if handle is not None:
            kwargs["parent_window_handle"] = handle
    return app.acquire_token_interactive(**kwargs)


def _explain(result: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    """Turn an AADSTS error into something that says what to change.

    The raw errors are long, opaque, and the useful part is the AADSTS code buried in the middle.
    These four cover essentially every first-run failure for this kind of app.
    """
    error = str(result.get("error") or "")
    description = str(result.get("error_description") or result)

    if "AADSTS65001" in description or error == "consent_required":
        return ("Consent has not been granted for {} on this application. Ask IT for admin consent "
                "to the delegated permission - most federal tenants disable user consent, so you "
                "cannot grant it yourself.\n\n{}".format(cfg["scope"], description))
    if "AADSTS700016" in description or "AADSTS90002" in description:
        return ("The tenant or the application was not found. Check client_id and tenant_id in "
                "graph.json, and that cloud is right: a GCC High or DoD tenant is not visible from "
                "the commercial login host.\n\n{}".format(description))
    if "AADSTS50076" in description or "AADSTS53000" in description:
        return ("Conditional Access requires MFA or a compliant device for this sign-in. Set "
                "auth_mode to \"broker\" in graph.json so the token comes from the device's own "
                "signed-in state, which satisfies the policy.\n\n{}".format(description))
    if "AADSTS7000218" in description:
        return ("The application is registered as a confidential client and is being used as a "
                "public one. Ask IT to enable the public-client/native flow on the registration "
                "(there must be no client secret involved).\n\n{}".format(description))
    return "Token acquisition failed ({}): {}".format(error or "unknown", description)


def forget() -> bool:
    """Delete the cached token. Returns whether a file was actually removed."""
    path = cache_path()
    try:
        path.unlink()
        return True
    except OSError:
        return False
