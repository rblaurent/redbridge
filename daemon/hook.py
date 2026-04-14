"""Claude Code hook bridge. Stub — filled in at step 8.

Reads JSON from stdin, captures GetConsoleWindow() HWND, POSTs to
/hook/event with a 2s timeout, fails silently.
"""

from __future__ import annotations

import sys


def main() -> int:
    # TODO step 8: read stdin JSON, attach hwnd from GetConsoleWindow(),
    # POST to http://127.0.0.1:7337/hook/event with 2s timeout.
    _ = sys.stdin.read()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
