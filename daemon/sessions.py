"""Claude Code session store and hook event helpers.

The daemon's ``POST /hook/event`` endpoint normalizes incoming Claude Code
hook payloads into ``HookEvent``, records them in a global ``SessionStore``,
and optionally publishes to the ``HookBus`` for push-based consumers.

Behaviors that care about Claude Code state should *poll*
``SESSIONS.snapshot()`` each tick rather than subscribing to the bus.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Hook event
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HookEvent:
    session_id: str
    hook: str
    hwnd: int | None
    received_at: float
    raw: dict[str, Any] = field(default_factory=dict)


def event_from_payload(payload: dict[str, Any]) -> HookEvent:
    sid = str(payload.get("session_id") or payload.get("sessionId") or "")
    hook = str(payload.get("hook_event_name") or payload.get("hook") or "")
    hwnd_raw = payload.get("hwnd")
    try:
        hwnd = int(hwnd_raw) if hwnd_raw is not None else None
    except (TypeError, ValueError):
        hwnd = None
    return HookEvent(
        session_id=sid,
        hook=hook,
        hwnd=hwnd,
        received_at=time.monotonic(),
        raw=payload,
    )


# ---------------------------------------------------------------------------
# Session store (poll-friendly)
# ---------------------------------------------------------------------------

@dataclass
class SessionInfo:
    session_id: str
    last_hook: str
    hwnd: int | None
    last_seen: float
    cwd: str = ""
    tool_name: str = ""
    transcript_path: str = ""


class SessionStore:
    """Thread-safe store of the latest hook per Claude Code session."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._lock = threading.Lock()

    def record(self, evt: HookEvent) -> None:
        if not evt.session_id:
            return
        with self._lock:
            prev = self._sessions.get(evt.session_id)
            hwnd = evt.hwnd or (prev.hwnd if prev else None)
            cwd = str(evt.raw.get("cwd") or (prev.cwd if prev else "") or "")
            tool_name = str(evt.raw.get("tool_name") or (prev.tool_name if prev else "") or "")
            transcript_path = str(evt.raw.get("transcript_path") or (prev.transcript_path if prev else "") or "")
            if evt.hook == "SessionEnd":
                self._sessions.pop(evt.session_id, None)
            else:
                self._sessions[evt.session_id] = SessionInfo(
                    evt.session_id, evt.hook, hwnd, evt.received_at,
                    cwd=cwd, tool_name=tool_name,
                    transcript_path=transcript_path,
                )

    def snapshot(self) -> list[SessionInfo]:
        with self._lock:
            return list(self._sessions.values())

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


SESSIONS = SessionStore()


# ---------------------------------------------------------------------------
# Push-based hook bus (kept for other consumers)
# ---------------------------------------------------------------------------

class HookBus:
    def __init__(self) -> None:
        self._subs: list[Callable[[HookEvent], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, cb: Callable[[HookEvent], None]) -> None:
        with self._lock:
            if cb not in self._subs:
                self._subs.append(cb)

    def unsubscribe(self, cb: Callable[[HookEvent], None]) -> None:
        with self._lock:
            try:
                self._subs.remove(cb)
            except ValueError:
                pass

    def publish(self, evt: HookEvent) -> None:
        with self._lock:
            subs = list(self._subs)
        for s in subs:
            try:
                s(evt)
            except Exception as e:
                print(f"[hooks] subscriber error: {e}", flush=True)


HOOKS = HookBus()
