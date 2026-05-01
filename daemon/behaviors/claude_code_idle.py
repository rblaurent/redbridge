"""Tri-state Claude Code indicator (polling).

Polls the global session store each tick and renders an animated star
glyph in one of three modes:

- idle     — no tracked sessions: grey static glyph
- thinking — >=1 session processing: orange cycling glyph
- waiting  — >=1 session needs user: orange glyph + red pill w/ count

Press the key to focus the oldest waiting (or thinking) session's terminal.
"""

from __future__ import annotations

import time

from PIL import Image, ImageDraw, ImageFont

from behaviors.base import Behavior, EventBus, Target, TargetKind
from gfx import font
from registry import register
from sessions import SESSIONS, SessionInfo
from win_focus import focus_window, is_window


STALE_AFTER_SECONDS = 30 * 60
PURGE_INTERVAL_SECONDS = 2.0

THINKING_HOOKS = frozenset({"UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStop"})
WAITING_HOOKS = frozenset({"Notification", "Stop"})

CLAUDE_ORANGE = (193, 95, 60)
IDLE_GREY = (96, 96, 96)

FRAMES: tuple[str, ...] = (
    "✢",
    "✳",
    "✶",
    "✻",
    "✽",
    "✻",
    "✶",
    "✳",
)

_SYMBOL_FONT_CACHE: dict[int, ImageFont.ImageFont] = {}
_SYMBOL_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\seguisym.ttf",
    r"C:\Windows\Fonts\seguiemj.ttf",
]


def _symbol_font(size: int) -> ImageFont.ImageFont:
    cached = _SYMBOL_FONT_CACHE.get(size)
    if cached is not None:
        return cached
    for path in _SYMBOL_FONT_CANDIDATES:
        try:
            f = ImageFont.truetype(path, size)
            _SYMBOL_FONT_CACHE[size] = f
            return f
        except OSError:
            continue
    return font(size)


def _poll() -> tuple[int, int, list[SessionInfo]]:
    """Read the session store, prune dead entries, return (thinking, waiting, alive).

    Sessions with an hwnd are kept until their window is gone (SessionEnd or is_window check).
    Sessions without an hwnd fall back to the stale timeout.
    """
    now = time.monotonic()
    cutoff = now - STALE_AFTER_SECONDS
    alive: list[SessionInfo] = []
    for s in SESSIONS.snapshot():
        if not s.hwnd:
            if s.last_seen < cutoff:
                SESSIONS.drop(s.session_id)
                print(f"[claude_code] purge session={s.session_id[:8]} reason=stale", flush=True)
            continue
        if not is_window(s.hwnd):
            SESSIONS.drop(s.session_id)
            print(f"[claude_code] purge session={s.session_id[:8]} reason=window gone", flush=True)
            continue
        alive.append(s)
    t = sum(1 for s in alive if s.last_hook in THINKING_HOOKS)
    w = sum(1 for s in alive if s.last_hook in WAITING_HOOKS)
    return t, w, alive


@register
class ClaudeCodeIdleBehavior(Behavior):
    type_id = "claude_code_idle"
    display_name = "Claude Code"
    targets = {TargetKind.KEY}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        self._frame = 0
        self._t = 0
        self._w = 0
        self._alive: list[SessionInfo] = []
        self._last_purge = 0.0

    def tick(self) -> bool:
        now = time.monotonic()
        if now - self._last_purge >= PURGE_INTERVAL_SECONDS:
            self._last_purge = now
            t, w, alive = _poll()
        else:
            snap = [s for s in SESSIONS.snapshot() if s.hwnd]
            alive = snap
            t = sum(1 for s in snap if s.last_hook in THINKING_HOOKS)
            w = sum(1 for s in snap if s.last_hook in WAITING_HOOKS)

        prev = (self._t, self._w)
        self._t, self._w, self._alive = t, w, alive

        if t > 0:
            self._frame = (self._frame + 1) % len(FRAMES)
            return True
        return (t, w) != prev

    def render(self) -> Image.Image | None:
        w, h = self.size()
        t_count, w_count = self._t, self._w

        img = Image.new("RGB", (w, h), (0, 0, 0))

        if not self._alive:
            return img

        draw = ImageDraw.Draw(img)

        glyph_size = 72
        if t_count > 0:
            glyph = FRAMES[self._frame]
            color = CLAUDE_ORANGE
        else:
            glyph = FRAMES[0]
            color = IDLE_GREY

        gf = _symbol_font(glyph_size)
        b = draw.textbbox((0, 0), glyph, font=gf)
        gw = b[2] - b[0]
        gh = b[3] - b[1]
        gx = (w - gw) // 2 - b[0]
        gy = (h - gh) // 2 - b[1]
        draw.text((gx, gy), glyph, fill=color, font=gf)

        if w_count > 0:
            self._draw_pill(draw, w, w_count)

        return img

    @staticmethod
    def _draw_pill(draw: ImageDraw.ImageDraw, key_w: int, count: int) -> None:
        text = str(count) if count < 100 else "99"
        f = font(14)
        d = 24
        cx = key_w - d // 2 - 6
        cy = 6 + d // 2
        draw.ellipse(
            (cx - d // 2, cy - d // 2, cx + d // 2, cy + d // 2),
            fill=(80, 80, 80),
        )
        draw.text((cx, cy), text, fill=(255, 255, 255), font=f, anchor="mm")

    def on_press(self) -> None:
        waiting = sorted(
            (s for s in self._alive if s.last_hook in WAITING_HOOKS),
            key=lambda s: s.last_seen,
        )
        thinking = sorted(
            (s for s in self._alive if s.last_hook in THINKING_HOOKS),
            key=lambda s: s.last_seen,
        )
        candidates = waiting or thinking
        if not candidates:
            print("[claude_code] press: no tracked sessions", flush=True)
            return
        for s in candidates:
            ok = focus_window(s.hwnd)
            print(
                f"[claude_code] press: session={s.session_id[:8]} "
                f"hwnd={s.hwnd} focus={'ok' if ok else 'fail'}",
                flush=True,
            )
            if ok:
                return
