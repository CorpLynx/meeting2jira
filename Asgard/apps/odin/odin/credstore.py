"""Per-user secret storage with Windows DPAPI (CryptProtectData) through ctypes.

Position in the flow
    __main__ calls save_token from `set-token` and load_token before building a JiraClient. Nothing
    else touches the secret, and it is never written to a log, an export, or the state database.

Why ctypes
    keyring and pywin32 would both need pip, which the target machine does not have. Calling
    crypt32 directly keeps the project standard-library-only. The cost is that the argtypes and the
    LocalFree handling have to be exactly right, which is what the DPAPI round-trip check in
    tools/Invoke-WindowsChecks.ps1 exists to prove.

Properties
    * The encrypted file opens only for the same Windows user on the same machine. Copying it
      elsewhere, or a profile reset, makes it unreadable - `set-token` again is the fix.
    * JIRA_PAT overrides the file. That is how the tool runs on macOS and Linux for development,
      where DPAPI does not exist. Do not leave it set permanently.
    * Off Windows, save_token and load_token refuse rather than falling back to anything weaker.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Tuple

TOKEN_FILE = "jira_token.dpapi"
ENV_VAR = "JIRA_PAT"


class CredentialError(Exception):
    pass


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
        if not _crypt32.CryptProtectData(ctypes.byref(blob_in), "meeting2jira", None, None, None,
                                         _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _take(blob_out)

    def _unprotect(data: bytes) -> bytes:
        blob_in, _keepalive = _to_blob(data)
        blob_out = _Blob()
        if not _crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                           _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _take(blob_out)


def save_token(data_dir: Path, token: str) -> Path:
    if sys.platform != "win32":
        raise CredentialError("DPAPI storage is Windows-only. Elsewhere, set the JIRA_PAT environment variable.")
    path = Path(data_dir) / TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_protect(token.encode("utf-8")))
    return path


def load_token(data_dir: Path) -> Tuple[str, str]:
    """Return (token, where_it_came_from)."""
    env = os.environ.get(ENV_VAR)
    if env:
        return env.strip(), f"environment variable {ENV_VAR}"
    path = Path(data_dir) / TOKEN_FILE
    if not path.is_file():
        raise CredentialError("No Jira token stored yet. Run: python -m meeting2jira set-token")
    if sys.platform != "win32":
        raise CredentialError(f"{path} is DPAPI-encrypted and can only be read on Windows. Set {ENV_VAR} instead.")
    try:
        return _unprotect(path.read_bytes()).decode("utf-8"), f"DPAPI file {path}"
    except OSError as exc:
        raise CredentialError(
            f"Could not decrypt {path} ({exc}). DPAPI data only opens for the same Windows user on the "
            "same machine; run set-token again."
        ) from None
