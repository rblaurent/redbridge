"""Bring a window to the foreground on Windows 11.

- If the target window lives on another virtual desktop, switch to that
  desktop first (via pyvda, which wraps the undocumented IVirtualDesktop*
  interfaces).
- Defeat Windows focus-stealing prevention with the AttachThreadInput trick.
- Restore minimized windows.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

import comtypes
import comtypes.client
from comtypes import GUID, HRESULT, COMMETHOD, IUnknown

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.restype = wintypes.BOOL
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.keybd_event.argtypes = [
    wintypes.BYTE,
    wintypes.BYTE,
    wintypes.DWORD,
    ctypes.c_void_p,
]
_user32.keybd_event.restype = None
_user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
_user32.AllowSetForegroundWindow.restype = wintypes.BOOL
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD

SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
ASFW_ANY = 0xFFFFFFFF


_kernel32.GetConsoleTitleW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD]
_kernel32.GetConsoleTitleW.restype = wintypes.DWORD


def is_window(hwnd: int | None) -> bool:
    if not hwnd:
        return False
    return bool(_user32.IsWindow(wintypes.HWND(hwnd)))


def get_console_title(hwnd: int | None) -> str:
    """Read the console title for the process that owns ``hwnd``."""
    if not hwnd:
        return ""
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    if not pid.value:
        return ""
    try:
        _kernel32.FreeConsole()
    except OSError:
        pass
    if not _kernel32.AttachConsole(pid.value):
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        _kernel32.GetConsoleTitleW(buf, 1024)
        title = buf.value
        # Strip leading status glyph (Claude Code prefixes a spinner char)
        if title and not title[0].isalnum():
            title = title.lstrip().removeprefix(title[0]).lstrip()
        return title
    finally:
        try:
            _kernel32.FreeConsole()
        except OSError:
            pass


def _nudge_foreground() -> None:
    """Press-and-release Alt so our process counts as having 'recent' input,
    letting the next SetForegroundWindow succeed even under Win11's
    foreground-stealing prevention."""
    _user32.keybd_event(VK_MENU, 0, 0, None)
    _user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)


class IVirtualDesktopManager(IUnknown):
    _iid_ = GUID("{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}")
    _methods_ = [
        COMMETHOD(
            [], HRESULT, "IsWindowOnCurrentVirtualDesktop",
            (["in"], wintypes.HWND, "topLevelWindow"),
            (["out"], ctypes.POINTER(wintypes.BOOL), "onCurrentDesktop"),
        ),
        COMMETHOD(
            [], HRESULT, "GetWindowDesktopId",
            (["in"], wintypes.HWND, "topLevelWindow"),
            (["out"], ctypes.POINTER(GUID), "desktopId"),
        ),
        COMMETHOD(
            [], HRESULT, "MoveWindowToDesktop",
            (["in"], wintypes.HWND, "topLevelWindow"),
            (["in"], ctypes.POINTER(GUID), "desktopId"),
        ),
    ]


_CLSID_VDM = GUID("{AA509086-5CA9-4C25-8F95-589D3C07B48A}")
_vdm: IVirtualDesktopManager | None = None


def _get_vdm() -> IVirtualDesktopManager | None:
    global _vdm
    if _vdm is not None:
        return _vdm
    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    try:
        _vdm = comtypes.client.CreateObject(_CLSID_VDM, interface=IVirtualDesktopManager)
    except Exception as e:
        print(f"[focus] VDM unavailable: {e}", flush=True)
        _vdm = None
    return _vdm


def _find_desktop_for_hwnd(hwnd: int):
    """Enumerate every desktop and pick the one whose app list contains hwnd.
    Works around pyvda's inability to resolve the desktop of some window
    types (e.g. conhost) via the direct GetVirtualDesktopId path, which
    returns a null GUID.
    """
    from pyvda import VirtualDesktop, get_virtual_desktops

    for desk in get_virtual_desktops():
        try:
            for app in desk.apps_by_z_order(include_pinned=False):
                if app.hwnd == hwnd:
                    return desk
        except Exception:
            continue
    return None


def _switch_to_window_desktop(hwnd: int) -> None:
    """Switch the active virtual desktop to the one hosting ``hwnd``.
    Requires the window to be tracked by the virtual-desktop COM (Windows
    Terminal, VSCode, normal app windows — NOT raw conhost windows spawned
    by double-clicking a .bat)."""
    try:
        from pyvda import AppView, VirtualDesktop

        target = None
        try:
            target = AppView(hwnd=hwnd).desktop
        except Exception:
            target = None
        if target is None:
            target = _find_desktop_for_hwnd(hwnd)
        if target is None:
            print(
                f"[focus] hwnd {hwnd} is not tracked by the virtual desktop COM "
                "(raw conhost?). Cannot resolve its desktop. Launch the "
                "terminal via Windows Terminal instead.",
                flush=True,
            )
            return
        if target.number != VirtualDesktop.current().number:
            target.go(allow_set_foreground=True)
    except Exception as e:
        print(f"[focus] desktop switch failed: {e}", flush=True)


def focus_window(hwnd: int | None) -> bool:
    if not hwnd:
        return False
    h = wintypes.HWND(hwnd)
    if not _user32.IsWindow(h):
        return False

    _switch_to_window_desktop(int(hwnd))

    target_tid = _user32.GetWindowThreadProcessId(h, None)
    if not target_tid:
        return False
    current_tid = _kernel32.GetCurrentThreadId()

    _user32.AllowSetForegroundWindow(ASFW_ANY)
    _nudge_foreground()

    attached = False
    try:
        if target_tid != current_tid:
            attached = bool(_user32.AttachThreadInput(current_tid, target_tid, True))
        if _user32.IsIconic(h):
            _user32.ShowWindow(h, SW_RESTORE)
        _user32.BringWindowToTop(h)
        ok = bool(_user32.SetForegroundWindow(h))
        if not ok:
            # Some Win11 builds need a second swing after the alt-tap settles.
            _user32.SetForegroundWindow(h)
            ok = _user32.GetForegroundWindow() == int(hwnd)
        return ok
    finally:
        if attached:
            _user32.AttachThreadInput(current_tid, target_tid, False)
