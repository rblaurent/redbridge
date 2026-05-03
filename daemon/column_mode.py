"""Column profile registry and active-profile state for column 1.

Profiles define the five targets that make up a column (key, status key,
dial rotate, dial press, strip).  Tapping strip:1 cycles through them;
the runtime re-instantiates the affected behaviors via the "column:swap" bus event.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_col1_index: int = 0

COLUMN_1_PROFILES: list[dict] = [
    {
        "name": "RedMatter",
        "key_1":         {"behavior": "redmatter_launcher",       "config": {}},
        "key_5":         {"behavior": "redmatter_ai_status",      "config": {}},
        "dial_rotate_1": {"behavior": "redmatter_session_scroll", "config": {}},
        "dial_press_1":  {"behavior": "redmatter_session_focus",  "config": {}},
        "strip_1":       {"behavior": "redmatter_session_strip",  "config": {}},
    },
    {
        "name": "Axl",
        "key_1":         {"behavior": "axl_status_key",     "config": {}},
        "key_5":         {"behavior": "axl_aggregate",      "config": {}},
        "dial_rotate_1": {"behavior": "axl_session_scroll", "config": {}},
        "dial_press_1":  {"behavior": "axl_session_focus",  "config": {}},
        "strip_1":       {"behavior": "axl_session_strip",  "config": {}},
    },
    {
        "name": "RedCompute",
        "key_1":         {"behavior": "redcompute_launcher",   "config": {}},
        "key_5":         {"behavior": "redcompute_job_status", "config": {}},
        "dial_rotate_1": {"behavior": "redcompute_job_scroll", "config": {}},
        "dial_press_1":  {"behavior": "redcompute_job_focus",  "config": {}},
        "strip_1":       {"behavior": "redcompute_job_strip",  "config": {}},
    },
]


def cycle_col1() -> dict:
    """Advance column 1 to the next profile and return it."""
    global _col1_index
    with _lock:
        _col1_index = (_col1_index + 1) % len(COLUMN_1_PROFILES)
        return COLUMN_1_PROFILES[_col1_index]


def current_col1() -> dict:
    with _lock:
        return COLUMN_1_PROFILES[_col1_index]
