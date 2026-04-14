"""Flagship behavior: lights up when one or more Claude Code sessions are
waiting for the user, and focuses the oldest waiting window on press."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from PIL import Image, ImageDraw

from behaviors.base import Behavior, EventBus, Target, TargetKind
from gfx import font
from registry import register
from sessions import HOOKS, HookEvent
from win_focus import focus_window


@dataclass
class _Wait:
    session_id: str
    hwnd: int | None
    since: float


@register
class ClaudeCodeIdleBehavior(Behavior):
    type_id = "claude_code_idle"
    display_name = "Claude Code idle"
    targets = {TargetKind.KEY}
    config_schema = {
        "type": "object",
        "properties": {
            "waiting_hooks": {
                "type": "array",
                "items": {"type": "string"},
                "default": ["Notification"],
                "description": "Hook event names that mark a session as waiting",
            },
        },
    }

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        self._waiting: dict[str, _Wait] = {}
        self._dirty = True
        self._lock = threading.Lock()
        HOOKS.subscribe(self._on_hook)

    def _waiting_hooks(self) -> set[str]:
        raw = self.config.get("waiting_hooks") or ["Notification"]
        if not isinstance(raw, list):
            return {"Notification"}
        return {str(x) for x in raw if str(x)}

    def _on_hook(self, evt: HookEvent) -> None:
        if not evt.session_id:
            return
        waiting_hooks = self._waiting_hooks()
        with self._lock:
            before = len(self._waiting)
            if evt.hook in waiting_hooks:
                prev = self._waiting.get(evt.session_id)
                since = prev.since if prev else evt.received_at
                hwnd = evt.hwnd if evt.hwnd else (prev.hwnd if prev else None)
                self._waiting[evt.session_id] = _Wait(evt.session_id, hwnd, since)
            else:
                self._waiting.pop(evt.session_id, None)
            if len(self._waiting) != before:
                self._dirty = True

    def tick(self) -> bool:
        with self._lock:
            if self._dirty:
                self._dirty = False
                return True
            return False

    def render(self) -> Image.Image | None:
        w, h = self.size()
        with self._lock:
            count = len(self._waiting)
        bg = (172, 35, 35) if count > 0 else (38, 38, 38)
        fg = (255, 255, 255)
        img = Image.new("RGB", (w, h), bg)
        draw = ImageDraw.Draw(img)

        label = "Claude"
        lf = font(14)
        b = draw.textbbox((0, 0), label, font=lf)
        tw = b[2] - b[0]
        draw.text(((w - tw) // 2 - b[0], 6 - b[1]), label, font=lf, fill=fg)

        if count > 0:
            bf = font(48)
            s = str(count)
            b = draw.textbbox((0, 0), s, font=bf)
            tw = b[2] - b[0]
            th = b[3] - b[1]
            draw.text(
                ((w - tw) // 2 - b[0], (h - th) // 2 - b[1] + 6),
                s,
                font=bf,
                fill=fg,
            )
        else:
            mf = font(16)
            s = "idle"
            b = draw.textbbox((0, 0), s, font=mf)
            tw = b[2] - b[0]
            th = b[3] - b[1]
            draw.text(
                ((w - tw) // 2 - b[0], (h - th) // 2 - b[1] + 8),
                s,
                font=mf,
                fill=(160, 160, 160),
            )
        return img

    def on_press(self) -> None:
        with self._lock:
            oldest_first = sorted(self._waiting.values(), key=lambda w: w.since)
        for w in oldest_first:
            if focus_window(w.hwnd):
                return
