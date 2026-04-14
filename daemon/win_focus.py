"""Bring a window to the foreground on Windows 11, defeating focus-stealing
prevention with the AttachThreadInput trick. Also restores minimized windows
and follows the target across virtual desktops."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

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
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD

SW_RESTORE = 9


def focus_window(hwnd: int | None) -> bool:
    if not hwnd:
        return False
    h = wintypes.HWND(hwnd)
    if not _user32.IsWindow(h):
        return False
    target_tid = _user32.GetWindowThreadProcessId(h, None)
    if not target_tid:
        return False
    current_tid = _kernel32.GetCurrentThreadId()
    attached = False
    try:
        if target_tid != current_tid:
            attached = bool(_user32.AttachThreadInput(current_tid, target_tid, True))
        if _user32.IsIconic(h):
            _user32.ShowWindow(h, SW_RESTORE)
        _user32.BringWindowToTop(h)
        return bool(_user32.SetForegroundWindow(h))
    finally:
        if attached:
            _user32.AttachThreadInput(current_tid, target_tid, False)
