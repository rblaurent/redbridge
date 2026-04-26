"""Claude Code → redbridge daemon bridge.

Reads the hook payload from stdin, resolves the terminal HWND by walking the
process tree (Claude Code spawns us without a console, so GetConsoleWindow is
useless), POSTs to the daemon's ``/hook/event`` endpoint with a 2 s timeout,
and fails silently if the daemon isn't running.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import urllib.request
from ctypes import wintypes

DAEMON_URL = "http://127.0.0.1:47337/hook/event"
TIMEOUT_SECONDS = 2.0

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
_kernel32.Process32First.restype = wintypes.BOOL
_kernel32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
_kernel32.Process32Next.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.GetConsoleWindow.argtypes = []
_kernel32.GetConsoleWindow.restype = wintypes.HWND
_kernel32.AttachConsole.argtypes = [wintypes.DWORD]
_kernel32.AttachConsole.restype = wintypes.BOOL
_kernel32.FreeConsole.argtypes = []
_kernel32.FreeConsole.restype = wintypes.BOOL

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.GetParent.argtypes = [wintypes.HWND]
_user32.GetParent.restype = wintypes.HWND

INVALID_HANDLE = wintypes.HANDLE(-1).value


def _parent_map() -> dict[int, int]:
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return {}
    parents: dict[int, int] = {}
    try:
        pe = _PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        if _kernel32.Process32First(snap, ctypes.byref(pe)):
            while True:
                parents[pe.th32ProcessID] = pe.th32ParentProcessID
                if not _kernel32.Process32Next(snap, ctypes.byref(pe)):
                    break
    finally:
        _kernel32.CloseHandle(snap)
    return parents


def _ancestors(pid: int, parents: dict[int, int]) -> list[int]:
    chain: list[int] = []
    seen: set[int] = set()
    while pid and pid not in seen:
        chain.append(pid)
        seen.add(pid)
        pid = parents.get(pid, 0)
    return chain


def _pid_to_toplevel_hwnd() -> dict[int, int]:
    """Map each PID to one of its visible top-level window HWNDs."""
    result: dict[int, int] = {}

    def cb(hwnd: int, _lparam: int) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return True
        if _user32.GetParent(hwnd):  # not top-level
            return True
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            result.setdefault(pid.value, hwnd)
        return True

    _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return result


def _exe_for_pid() -> dict[int, str]:
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return {}
    names: dict[int, str] = {}
    try:
        pe = _PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        if _kernel32.Process32First(snap, ctypes.byref(pe)):
            while True:
                try:
                    names[pe.th32ProcessID] = pe.szExeFile.decode("latin-1", "replace")
                except Exception:
                    pass
                if not _kernel32.Process32Next(snap, ctypes.byref(pe)):
                    break
    finally:
        _kernel32.CloseHandle(snap)
    return names


def _console_hwnd_for_pid(pid: int) -> int:
    """Attach to ``pid``'s console, read its window, detach. Returns 0 if the
    process has no classic console (e.g. ConPTY)."""
    try:
        _kernel32.FreeConsole()
    except Exception:
        pass
    if not _kernel32.AttachConsole(pid):
        return 0
    try:
        h = _kernel32.GetConsoleWindow()
        return int(h) if h else 0
    finally:
        try:
            _kernel32.FreeConsole()
        except Exception:
            pass


_SKIP_EXE = frozenset({"explorer.exe", "services.exe", "svchost.exe", "csrss.exe"})


def _resolve_terminal(chain: list[int], pid_hwnd: dict[int, int], pid_exe: dict[int, str] | None = None) -> int:
    pid_exe = pid_exe or {}
    # Best: first ancestor with a visible top-level window — this is the
    # terminal emulator process itself (WindowsTerminal.exe, VSCode, etc.)
    # and its HWND is tracked by the virtual-desktop COM.
    for pid in chain:
        exe = pid_exe.get(pid, "").lower()
        if exe in _SKIP_EXE:
            continue
        hwnd = pid_hwnd.get(pid)
        if hwnd:
            return int(hwnd)
    # Fallback: attach to each ancestor's console. Catches the classic
    # cmd-from-explorer case where conhost owns the visible window and no
    # ancestor has a top-level one. Returns conhost's HWND, which is not
    # virtual-desktop-tracked — desktop switching won't work, but focus will.
    for pid in chain[1:]:
        h = _console_hwnd_for_pid(pid)
        if h:
            return h
    return 0


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0
    if not raw or not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    try:
        parents = _parent_map()
        chain = _ancestors(os.getpid(), parents)
        names = _exe_for_pid()
        pid_hwnd = _pid_to_toplevel_hwnd()
        payload["_chain"] = [
            {"pid": pid, "exe": names.get(pid, "?"), "hwnd": pid_hwnd.get(pid, 0)}
            for pid in chain
        ]
        if not payload.get("hwnd"):
            payload["hwnd"] = _resolve_terminal(chain, pid_hwnd, names)
    except Exception:
        if not payload.get("hwnd"):
            try:
                h = _kernel32.GetConsoleWindow()
                payload["hwnd"] = int(h) if h else 0
            except Exception:
                payload["hwnd"] = 0

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DAEMON_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS).read()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
