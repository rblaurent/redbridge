"""Claude Code session monitor — carousel, detail, scroll, focus.

Four behaviors that work together via shared module-level state:

- claude_session_carousel  (strip)       — vertical scrollable session list
- claude_session_detail    (strip)       — detail card for selected session
- claude_session_scroll    (dial rotate) — scroll the carousel
- claude_session_focus     (dial press)  — focus the selected session's terminal
"""

from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw

from behaviors.base import Behavior, EventBus, Target, TargetKind
from gfx import font
from registry import register
from sessions import SESSIONS, SessionInfo
from win_focus import focus_window, is_window


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STALE_AFTER_SECONDS = 30 * 60

THINKING_HOOKS = frozenset({"UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStop"})
WAITING_HOOKS = frozenset({"Notification", "Stop"})

CLAUDE_ORANGE = (193, 95, 60)
STATUS_GREEN = (100, 180, 100)
STATUS_RED = (210, 100, 80)
DIM_GREY = (90, 90, 90)


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
    alive: list[SessionInfo] = []
    for s in SESSIONS.snapshot():
        if s.last_seen < cutoff:
            SESSIONS.drop(s.session_id)
            continue
        if s.hwnd and not is_window(s.hwnd):
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
    if hook == "Stop":
        return STATUS_GREEN
    if hook == "Notification":
        return STATUS_RED
    return DIM_GREY


def _status_text(session: SessionInfo) -> str:
    if session.last_hook in THINKING_HOOKS:
        return "Working..."
    if session.last_hook == "Stop":
        return "Done"
    if session.last_hook == "Notification":
        return "Needs attention"
    return session.last_hook or "unknown"


def _time_ago(last_seen: float) -> str:
    delta = time.monotonic() - last_seen
    if delta < 10:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    return f"{int(delta // 3600)}h ago"


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
FOOTER_H = 12


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

        img = Image.new("RGB", (w, h), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        if n == 0:
            f = font(14)
            draw.text((w // 2, h // 2), "No sessions", fill=(80, 80, 80),
                       font=f, anchor="mm")
            return img

        scroll_top = max(0, min(idx - 1, n - VISIBLE_ROWS))
        row_font = font(15)

        for row_i in range(VISIBLE_ROWS):
            si = scroll_top + row_i
            if si >= n:
                break
            s = sessions[si]
            y = row_i * ROW_H
            selected = si == idx

            if selected:
                draw.rectangle((0, y, w, y + ROW_H - 1), fill=CLAUDE_ORANGE)

            dot_color = (255, 255, 255) if selected else _status_color(s.last_hook)
            dot_y = y + ROW_H // 2
            draw.ellipse((6, dot_y - 3, 12, dot_y + 3), fill=dot_color)

            text_color = (255, 255, 255) if selected else (190, 190, 190)
            name = _workspace_name(s.cwd)
            name = _truncate(draw, name, row_font, w - 22)
            draw.text((18, y + 3), name, fill=text_color, font=row_font)

        # Footer
        footer_y = VISIBLE_ROWS * ROW_H
        draw.line((0, footer_y, w, footer_y), fill=(40, 40, 40))
        ff = font(11)
        label = f"{n} session{'s' if n != 1 else ''}"
        draw.text((6, footer_y + 2), label, fill=(100, 100, 100), font=ff)

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
            key = (s.session_id, s.last_hook, s.cwd, s.tool_name, idx)
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

        img = Image.new("RGB", (w, h), (15, 15, 15))
        draw = ImageDraw.Draw(img)

        if n == 0:
            f = font(14)
            draw.text((w // 2, h // 2), "No session selected", fill=(80, 80, 80),
                       font=f, anchor="mm")
            return img

        s = sessions[idx]
        color = _status_color(s.last_hook)

        # Accent bar
        draw.rectangle((0, 0, 3, h), fill=color)

        # Workspace name
        nf = font(18)
        name = _workspace_name(s.cwd)
        name = _truncate(draw, name, nf, w - 16)
        draw.text((10, 8), name, fill=(255, 255, 255), font=nf)

        # Status
        sf = font(15)
        draw.text((10, 36), _status_text(s), fill=color, font=sf)

        # Tool name
        if s.tool_name and s.last_hook in THINKING_HOOKS:
            tf = font(13)
            draw.text((10, 58), f"Tool: {s.tool_name}", fill=(120, 120, 120), font=tf)

        # Time ago
        af = font(11)
        draw.text((10, 82), _time_ago(s.last_seen), fill=DIM_GREY, font=af)

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
