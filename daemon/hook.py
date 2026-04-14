"""Claude Code → redbridge daemon bridge.

Reads the hook payload from stdin, captures the current console HWND, POSTs
to the daemon's ``/hook/event`` endpoint with a 2 s timeout, and fails
silently if the daemon isn't running. Never prints, never blocks Claude Code
for more than a brief moment.
"""

from __future__ import annotations

import ctypes
import json
import sys
import urllib.request

DAEMON_URL = "http://127.0.0.1:47337/hook/event"
TIMEOUT_SECONDS = 2.0


def _console_hwnd() -> int:
    try:
        return int(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
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

    payload.setdefault("hwnd", _console_hwnd())

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DAEMON_URL,
        data=data,
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
