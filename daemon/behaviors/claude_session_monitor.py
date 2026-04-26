"""Claude Code session monitor — carousel, detail, scroll, focus.

Four behaviors that work together via shared module-level state:

- claude_session_carousel  (strip)       — vertical scrollable session list
- claude_session_detail    (strip)       — detail card for selected session
- claude_session_scroll    (dial rotate) — scroll the carousel
- claude_session_focus     (dial press)  — focus the selected session's terminal
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass

from PIL import Image, ImageDraw

from behaviors.base import Behavior, EventBus, Target, TargetKind
from gfx import STRIP_BG, font, font_semibold, font_semilight, strip_bg
from registry import register
from sessions import SESSIONS, SessionInfo
from win_focus import focus_window, get_console_title, is_window


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STALE_AFTER_SECONDS = 30 * 60
DONE_STALE_SECONDS = 3 * 60

THINKING_HOOKS = frozenset({"UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStop"})
WAITING_HOOKS = frozenset({"Notification", "Stop"})

CLAUDE_ORANGE = (193, 95, 60)
STATUS_GREEN = (100, 180, 100)
STATUS_RED = (210, 100, 80)
DIM_GREY = (90, 90, 90)

DEFAULT_CONTEXT_MAX = 1_000_000
TRANSCRIPT_READ_INTERVAL = 5.0


# ---------------------------------------------------------------------------
# Shared carousel state
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_selected_index: int = 0


def _get_selected() -> int:
    with _state_lock:
        return _selected_index


def _set_selected(idx: int) -> None:
    global _selected_index
    with _state_lock:
        _selected_index = idx


# ---------------------------------------------------------------------------
# Transcript metadata cache
# ---------------------------------------------------------------------------

@dataclass
class _TranscriptMeta:
    context_used: int = 0
    context_max: int = DEFAULT_CONTEXT_MAX
    last_read: float = 0.0


_meta_lock = threading.Lock()
_meta_cache: dict[str, _TranscriptMeta] = {}


def _read_transcript_meta(session_id: str, transcript_path: str) -> _TranscriptMeta:
    with _meta_lock:
        cached = _meta_cache.get(session_id)
        if cached and (time.monotonic() - cached.last_read) < TRANSCRIPT_READ_INTERVAL:
            return cached

    meta = _TranscriptMeta(last_read=time.monotonic())
    if cached:
        meta.context_used = cached.context_used
        meta.context_max = cached.context_max

    if not transcript_path or not os.path.isfile(transcript_path):
        with _meta_lock:
            _meta_cache[session_id] = meta
        return meta

    try:
        size = os.path.getsize(transcript_path)
        read_bytes = min(size, 64 * 1024)
        with open(transcript_path, "rb") as f:
            f.seek(max(0, size - read_bytes))
            tail = f.read().decode("utf-8", "replace")
        for line in reversed(tail.strip().split("\n")):
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if d.get("type") == "assistant" and meta.context_used == 0:
                usage = (d.get("message") or {}).get("usage")
                if usage:
                    meta.context_used = (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )
                    break
    except Exception as e:
        print(f"[session_monitor] transcript read error: {e}", flush=True)

    with _meta_lock:
        _meta_cache[session_id] = meta
    return meta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _workspace_name(cwd: str) -> str:
    if not cwd:
        return "unknown"
    name = cwd.rstrip("/\\").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name or "unknown"


def _sorted_sessions() -> list[SessionInfo]:
    now = time.monotonic()
    cutoff = now - STALE_AFTER_SECONDS
    done_cutoff = now - DONE_STALE_SECONDS
    alive: list[SessionInfo] = []
    for s in SESSIONS.snapshot():
        if not s.hwnd:
            continue
        if s.last_seen < cutoff:
            SESSIONS.drop(s.session_id)
            continue
        if s.last_hook in WAITING_HOOKS and s.last_seen < done_cutoff:
            SESSIONS.drop(s.session_id)
            continue
        if not is_window(s.hwnd):
            SESSIONS.drop(s.session_id)
            continue
        alive.append(s)
    alive.sort(key=lambda s: (_workspace_name(s.cwd).lower(), s.session_id))
    return alive


def _clamped_index(n: int) -> int:
    if n == 0:
        return 0
    idx = _get_selected()
    return max(0, min(idx, n - 1))


def _status_color(hook: str) -> tuple[int, int, int]:
    if hook in THINKING_HOOKS:
        return CLAUDE_ORANGE
    if hook == "Notification":
        return STATUS_GREEN
    if hook == "Stop":
        return STATUS_GREEN
    return DIM_GREY


def _status_text(session: SessionInfo) -> str:
    if session.last_hook in THINKING_HOOKS:
        return "Working..."
    if session.last_hook == "Stop":
        return "Done"
    if session.last_hook == "Notification":
        return "Needs attention"
    return session.last_hook or "unknown"



def _truncate(draw: ImageDraw.ImageDraw, text: str, f, max_w: int) -> str:
    if draw.textlength(text, font=f) <= max_w:
        return text
    for end in range(len(text), 0, -1):
        candidate = text[:end] + "..."
        if draw.textlength(candidate, font=f) <= max_w:
            return candidate
    return "..."



# ---------------------------------------------------------------------------
# Carousel (strip)
# ---------------------------------------------------------------------------

ROW_H = 22
VISIBLE_ROWS = 4
ROW_START_Y = 6


@register
class ClaudeSessionCarousel(Behavior):
    type_id = "claude_session_carousel"
    display_name = "Claude sessions"
    targets = {TargetKind.STRIP_REGION}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        self._prev_key: tuple = ()

    def tick(self) -> bool:
        sessions = _sorted_sessions()
        idx = _clamped_index(len(sessions))
        key = tuple(
            (s.session_id, s.last_hook, s.cwd) for s in sessions
        ) + (idx,)
        if key != self._prev_key:
            self._prev_key = key
            return True
        return False

    def render(self) -> Image.Image | None:
        w, h = self.size()
        sessions = _sorted_sessions()
        n = len(sessions)
        idx = _clamped_index(n)

        img = strip_bg(w, h)
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, w, 2), fill=CLAUDE_ORANGE)

        if n == 0:
            return img

        scroll_top = max(0, min(idx - 1, n - VISIBLE_ROWS))
        row_font = font_semibold(13)

        for row_i in range(VISIBLE_ROWS):
            si = scroll_top + row_i
            if si >= n:
                break
            s = sessions[si]
            y = ROW_START_Y + row_i * ROW_H
            selected = si == idx

            if selected:
                draw.rounded_rectangle((4, y, w - 4, y + ROW_H - 2), 4, fill=CLAUDE_ORANGE)

            dot_color = (255, 255, 255) if selected else _status_color(s.last_hook)
            dot_y = y + ROW_H // 2
            draw.ellipse((10, dot_y - 4, 18, dot_y + 4), fill=dot_color)

            text_color = (255, 255, 255) if selected else (170, 170, 170)
            name = _workspace_name(s.cwd)
            name = _truncate(draw, name, row_font, w - 34)
            draw.text((24, dot_y - 1), name, fill=text_color, font=row_font, anchor="lm")

        return img


# ---------------------------------------------------------------------------
# Detail (strip)
# ---------------------------------------------------------------------------

@register
class ClaudeSessionDetail(Behavior):
    type_id = "claude_session_detail"
    display_name = "Claude session detail"
    targets = {TargetKind.STRIP_REGION}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        self._prev_key: tuple = ()

    def tick(self) -> bool:
        sessions = _sorted_sessions()
        idx = _clamped_index(len(sessions))
        if sessions:
            s = sessions[idx]
            meta = _read_transcript_meta(s.session_id, s.transcript_path)
            title = get_console_title(s.hwnd)
            key = (s.session_id, s.last_hook, s.cwd, s.tool_name, idx,
                   title, meta.context_used)
        else:
            key = ()
        if key != self._prev_key:
            self._prev_key = key
            return True
        return False

    def render(self) -> Image.Image | None:
        w, h = self.size()
        sessions = _sorted_sessions()
        n = len(sessions)
        idx = _clamped_index(n)

        img = strip_bg(w, h)
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, w, 2), fill=CLAUDE_ORANGE)

        if n == 0:
            return img

        s = sessions[idx]
        meta = _read_transcript_meta(s.session_id, s.transcript_path)
        color = _status_color(s.last_hook)

        title_text = get_console_title(s.hwnd) or _workspace_name(s.cwd)
        tf = font_semibold(14)
        title_text = _truncate(draw, title_text, tf, w - 20)
        draw.text((10, 22), title_text, fill=(255, 255, 255), font=tf, anchor="lm")

        draw.text((10, 42), _status_text(s), fill=color, font=font(11), anchor="lm")

        if s.tool_name and s.last_hook in THINKING_HOOKS:
            draw.text((10, 60), f"Tool: {s.tool_name}",
                       fill=(70, 70, 70), font=font_semilight(10), anchor="lm")

        if meta.context_used > 0:
            pct = min(1.0, meta.context_used / meta.context_max)
            bar_fg = CLAUDE_ORANGE if pct < 0.8 else STATUS_RED
            bx1, bx2, by, bh = 10, w - 10, 82, 8
            bar_r = bh // 2
            draw.rounded_rectangle((bx1, by, bx2, by + bh), bar_r, fill=(30, 30, 32))
            fill_x = bx1 + int((bx2 - bx1) * pct)
            if fill_x > bx1 + bar_r:
                draw.rounded_rectangle(
                    (bx1, by, fill_x, by + bh), bar_r, fill=bar_fg,
                )

        return img


# ---------------------------------------------------------------------------
# Scroll (dial rotate)
# ---------------------------------------------------------------------------

@register
class ClaudeSessionScroll(Behavior):
    type_id = "claude_session_scroll"
    display_name = "Claude session scroll"
    targets = {TargetKind.DIAL_ROTATE}
    config_schema = {"type": "object", "properties": {}}

    def on_rotate(self, delta: int) -> None:
        sessions = _sorted_sessions()
        n = len(sessions)
        if n == 0:
            return
        idx = _get_selected() + delta
        _set_selected(max(0, min(idx, n - 1)))


# ---------------------------------------------------------------------------
# Focus (dial press)
# ---------------------------------------------------------------------------

@register
class ClaudeSessionFocus(Behavior):
    type_id = "claude_session_focus"
    display_name = "Claude session focus"
    targets = {TargetKind.DIAL_PRESS}
    config_schema = {"type": "object", "properties": {}}

    def on_press(self) -> None:
        sessions = _sorted_sessions()
        n = len(sessions)
        if n == 0:
            print("[session_monitor] focus: no sessions", flush=True)
            return
        idx = _clamped_index(n)
        s = sessions[idx]
        ok = focus_window(s.hwnd)
        print(
            f"[session_monitor] focus: session={s.session_id[:8]} "
            f"hwnd={s.hwnd} workspace={_workspace_name(s.cwd)} "
            f"focus={'ok' if ok else 'fail'}",
            flush=True,
        )
