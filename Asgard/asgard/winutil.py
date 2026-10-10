"""Windows helpers. Each one is safe to call elsewhere: it does nothing,
returns a neutral value, or raises OSError where the caller needs to know.

Nothing here needs admin rights or PowerShell's Full Language mode: the
shortcut is created through COM from Python's ctypes, with PowerShell only
as a fallback.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Union

IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------
# Child processes
# --------------------------------------------------------------------------

CREATE_NO_WINDOW = 0x08000000


def kill_tree(pid: int) -> bool:
    """End a process and everything it started (taskkill /T). Killing only the parent leaves its
    children running: Stop on Odin's daily run would end PowerShell and leave Python posting.
    Windows only; True when taskkill reported success."""
    if not IS_WINDOWS or pid <= 0:
        return False
    try:
        done = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15,
                              creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


# --------------------------------------------------------------------------
# Window appearance
# --------------------------------------------------------------------------

def enable_dpi_awareness() -> None:
    """Crisp text on scaled displays. Call before creating the Tk window."""
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def apps_use_dark_theme() -> bool:
    override = os.environ.get("ASGARD_THEME", "").lower()
    if override in ("dark", "light"):
        return override == "dark"
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except OSError:
        return False


def set_dark_title_bar(widget: object, dark: bool) -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        hwnd = user32.GetParent(widget.winfo_id())  # type: ignore[attr-defined]
        value = ctypes.c_int(1 if dark else 0)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))  # DWMWA_USE_IMMERSIVE_DARK_MODE
    except Exception:
        pass


# --------------------------------------------------------------------------
# Opening things
# --------------------------------------------------------------------------

def open_path(path: Union[str, Path]) -> None:
    """Open a file, folder or link the way a double-click would."""
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_in_editor(path: Union[str, Path]) -> None:
    """Notepad on Windows: .json often has no file association there."""
    if IS_WINDOWS:
        subprocess.Popen(["notepad.exe", str(path)])
    else:
        open_path(path)


# --------------------------------------------------------------------------
# Shortcuts
# --------------------------------------------------------------------------

def create_shortcut(lnk: Path, target: str, arguments: str = "", working_dir: str = "",
                    icon: str = "", description: str = "") -> str:
    """Create a .lnk file. Returns the method used; raises OSError if every method fails."""
    if not IS_WINDOWS:
        raise OSError("shortcuts can only be created on Windows")
    lnk.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    for name, method in (("com", _shortcut_via_com), ("powershell", _shortcut_via_powershell)):
        try:
            method(lnk, target, arguments, working_dir, icon, description)
            if lnk.exists():
                return name
            errors.append(f"{name}: no file was written")
        except Exception as exc:  # try the next method
            errors.append(f"{name}: {exc}")
    raise OSError("; ".join(errors))


def _shortcut_via_com(lnk: Path, target: str, arguments: str, working_dir: str,
                      icon: str, description: str) -> None:
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def guid(text: str) -> GUID:
        u = uuid.UUID(text)
        return GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))

    clsid_shell_link = guid("00021401-0000-0000-C000-000000000046")
    iid_shell_link_w = guid("000214F9-0000-0000-C000-000000000046")
    iid_persist_file = guid("0000010B-0000-0000-C000-000000000046")

    def method(obj: ctypes.c_void_p, index: int, *argtypes: object, restype: object = ctypes.c_long):
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])

    def check(hr: int, what: str) -> None:
        if hr < 0:
            raise OSError(f"{what} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")

    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoUninitialize.restype = None

    hr = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    initialized = hr in (0, 1)            # S_OK or S_FALSE; otherwise COM was already set up
    try:
        link = ctypes.c_void_p()
        check(ole32.CoCreateInstance(ctypes.byref(clsid_shell_link), None, 1,  # CLSCTX_INPROC_SERVER
                                     ctypes.byref(iid_shell_link_w), ctypes.byref(link)),
              "CoCreateInstance(ShellLink)")
        try:
            # IShellLinkW vtable: 7 SetDescription, 9 SetWorkingDirectory,
            # 11 SetArguments, 17 SetIconLocation, 20 SetPath
            check(method(link, 20, wintypes.LPCWSTR)(link, target), "SetPath")
            if arguments:
                check(method(link, 11, wintypes.LPCWSTR)(link, arguments), "SetArguments")
            if working_dir:
                check(method(link, 9, wintypes.LPCWSTR)(link, working_dir), "SetWorkingDirectory")
            if icon:
                check(method(link, 17, wintypes.LPCWSTR, ctypes.c_int)(link, icon, 0), "SetIconLocation")
            if description:
                check(method(link, 7, wintypes.LPCWSTR)(link, description), "SetDescription")
            persist = ctypes.c_void_p()
            check(method(link, 0, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                link, ctypes.byref(iid_persist_file), ctypes.byref(persist)), "QueryInterface(IPersistFile)")
            try:
                # IPersistFile vtable: 6 Save
                check(method(persist, 6, wintypes.LPCWSTR, wintypes.BOOL)(persist, str(lnk), True),
                      "IPersistFile.Save")
            finally:
                method(persist, 2, restype=ctypes.c_ulong)(persist)  # Release
        finally:
            method(link, 2, restype=ctypes.c_ulong)(link)  # Release
    finally:
        if initialized:
            ole32.CoUninitialize()


_PS_SHORTCUT = (
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:ASG_LNK);"
    "$s.TargetPath=$env:ASG_TARGET;$s.Arguments=$env:ASG_ARGS;"
    "$s.WorkingDirectory=$env:ASG_WD;$s.Description=$env:ASG_DESC;"
    "if($env:ASG_ICON){$s.IconLocation=$env:ASG_ICON};$s.Save()"
)


def _shortcut_via_powershell(lnk: Path, target: str, arguments: str, working_dir: str,
                             icon: str, description: str) -> None:
    from .catalog import powershell_path
    env = dict(os.environ, ASG_LNK=str(lnk), ASG_TARGET=target, ASG_ARGS=arguments,
               ASG_WD=working_dir, ASG_ICON=icon, ASG_DESC=description)
    done = subprocess.run([powershell_path(), "-NoProfile", "-NonInteractive", "-Command", _PS_SHORTCUT],
                          env=env, capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise OSError((done.stderr or done.stdout).strip().splitlines()[0] if (done.stderr or done.stdout)
                      else f"exit code {done.returncode}")


def desktop_dir() -> Optional[Path]:
    """The real Desktop folder, which OneDrive may have redirected."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            value, _ = winreg.QueryValueEx(key, "Desktop")
        return Path(os.path.expandvars(value))
    except OSError:
        home = os.environ.get("USERPROFILE")
        return Path(home) / "Desktop" if home else None


# --------------------------------------------------------------------------
# Settings > Apps entry (per user, no admin)
# --------------------------------------------------------------------------

def write_uninstall_entry(subkey: str, values: Dict[str, Union[str, int]]) -> None:
    if not IS_WINDOWS:
        raise OSError("the Settings > Apps entry is Windows-only")
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_WRITE) as key:
        for name, value in values.items():
            if isinstance(value, int):
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))


def delete_uninstall_entry(subkey: str) -> bool:
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
        return True
    except FileNotFoundError:
        return False
