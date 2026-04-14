"""Claude Code hook event bus.

Exposes a singleton ``HOOKS`` that behaviors subscribe to. The daemon's
``POST /hook/event`` endpoint normalizes incoming Claude Code hook payloads
into ``HookEvent`` and publishes them.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class HookEvent:
    session_id: str
    hook: str
    hwnd: int | None
    received_at: float
    raw: dict[str, Any] = field(default_factory=dict)


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
