"""Claude Code session monitor — strip, scroll, focus.

Three behaviors that work together via shared module-level state:

- claude_session_strip   (strip)       — detail card with pill page indicators
- claude_session_scroll  (dial rotate) — page between sessions with swipe animation
- claude_session_focus   (dial press)  — focus the selected session's terminal
"""

from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw

from behaviors.base import Behavior, EventBus, Target, TargetKind
from gfx import STRIP_BG, SWIPE_ANIM_DURATION, ease_back_out, font, font_semibold, font_semilight, strip_bg
from registry import register
from sessions import SESSIONS, SessionInfo
from win_focus import focus_window, get_console_title, is_window


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THINKING_HOOKS = frozenset({"UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStop"})
WAITING_HOOKS = frozenset({"Notification", "Stop"})

CLAUDE_ORANGE = (193, 95, 60)
STATUS_GREEN = (100, 180, 100)
DIM_GREY = (90, 90, 90)

PILL_SIZE = 8
PILL_GAP = 6
PILL_R = 4
MAX_PILLS = 9


# ---------------------------------------------------------------------------
# Shared state — selection + animation
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_selected_index: int = 0
_anim_from: int = 0
_anim_to: int = 0
_anim_start: float = 0.0
_anim_direction: int = 0


def _get_selected() -> int:
    with _state_lock:
        return _selected_index


def _set_selected(idx: int) -> None:
    global _selected_index
    with _state_lock:
        _selected_index = idx


def _start_anim(from_idx: int, to_idx: int, direction: int) -> None:
    global _anim_from, _anim_to, _anim_start, _anim_direction
    with _state_lock:
        _anim_from = from_idx
        _anim_to = to_idx
        _anim_start = time.monotonic()
        _anim_direction = direction


def _is_animating() -> bool:
    with _state_lock:
        if _anim_start == 0.0:
            return False
        return (time.monotonic() - _anim_start) < SWIPE_ANIM_DURATION


def _get_anim() -> tuple[int, int, float, int]:
    with _state_lock:
        return _anim_from, _anim_to, _anim_start, _anim_direction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _workspace_name(cwd: str) -> str:
    if not cwd:
        return "unknown"
    name = cwd.rstrip("/\\").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name or "unknown"


def _sorted_sessions() -> list[SessionInfo]:
    alive: list[SessionInfo] = []
    for s in SESSIONS.snapshot():
        if not s.hwnd:
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
    if hook in WAITING_HOOKS:
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
# Pill rendering
# ---------------------------------------------------------------------------

def _blend(color: tuple[int, int, int], alpha: float) -> tuple[int, int, int]:
    bg_r, bg_g, bg_b = STRIP_BG
    r, g, b = color
    return (
        int(r * alpha + bg_r * (1 - alpha)),
        int(g * alpha + bg_g * (1 - alpha)),
        int(b * alpha + bg_b * (1 - alpha)),
    )


def _draw_pills(
    draw: ImageDraw.ImageDraw, w: int, n: int, selected_idx: int,
    sessions: list[SessionInfo],
) -> None:
    if n <= 1:
        return

    if n <= MAX_PILLS:
        vis_start = 0
        vis_count = n
        fade_left = False
        fade_right = False
    else:
        half = MAX_PILLS // 2
        vis_start = max(0, min(selected_idx - half, n - MAX_PILLS))
        vis_count = MAX_PILLS
        fade_left = vis_start > 0
        fade_right = vis_start + MAX_PILLS < n

    total_w = vis_count * PILL_SIZE + (vis_count - 1) * PILL_GAP
    start_x = (w - total_w) // 2
    y = 82

    for i in range(vis_count):
        actual_idx = vis_start + i
        px = start_x + i * (PILL_SIZE + PILL_GAP)
        s = sessions[max(0, min(actual_idx, n - 1))]
        base = _status_color(s.last_hook)

        selected = actual_idx == selected_idx
        alpha = 1.0 if selected else 0.5

        if (i == 0 and fade_left) or (i == vis_count - 1 and fade_right):
            alpha *= 0.4

        color = _blend(base, alpha)

        draw.rounded_rectangle(
            (px, y, px + PILL_SIZE, y + PILL_SIZE),
            PILL_R,
            fill=color,
        )


# ---------------------------------------------------------------------------
# Detail frame rendering (shared by static + animation paths)
# ---------------------------------------------------------------------------

def _render_detail_frame(
    w: int, h: int, sessions: list[SessionInfo], idx: int, n: int,
) -> Image.Image:
    img = strip_bg(w, h)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, w, 2), fill=CLAUDE_ORANGE)

    if n == 0:
        return img

    idx = max(0, min(idx, n - 1))
    s = sessions[idx]
    color = _status_color(s.last_hook)

    title_text = get_console_title(s.hwnd) or _workspace_name(s.cwd)
    tf = font_semibold(14)
    title_text = _truncate(draw, title_text, tf, w - 20)
    draw.text((10, 22), title_text, fill=(255, 255, 255), font=tf, anchor="lm")

    draw.text((10, 42), _status_text(s), fill=color, font=font(11), anchor="lm")

    if s.tool_name and s.last_hook in THINKING_HOOKS:
        draw.text(
            (10, 60), f"Tool: {s.tool_name}",
            fill=(70, 70, 70), font=font_semilight(10), anchor="lm",
        )

    return img


# ---------------------------------------------------------------------------
# Strip (consolidated)
# ---------------------------------------------------------------------------

@register
class ClaudeSessionStrip(Behavior):
    type_id = "claude_session_strip"
    display_name = "Claude session"
    targets = {TargetKind.STRIP_REGION}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        self._prev_key: tuple = ()

    def tick(self) -> bool:
        if _is_animating():
            return True
        sessions = _sorted_sessions()
        n = len(sessions)
        idx = _clamped_index(n)
        if sessions:
            s = sessions[idx]
            title = get_console_title(s.hwnd)
            key = (s.session_id, s.last_hook, s.cwd, s.tool_name, title, n, idx)
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

        anim_from, anim_to, anim_start, anim_dir = _get_anim()
        now = time.monotonic()
        elapsed = now - anim_start if anim_start > 0 else SWIPE_ANIM_DURATION + 1

        if elapsed < SWIPE_ANIM_DURATION and n > 0:
            t = min(1.0, elapsed / SWIPE_ANIM_DURATION)
            eased = ease_back_out(t)
            x_off = int(w * eased)

            from_frame = _render_detail_frame(w, h, sessions, anim_from, n)
            to_frame = _render_detail_frame(w, h, sessions, anim_to, n)

            canvas = Image.new("RGB", (w, h), STRIP_BG)
            if anim_dir < 0:
                canvas.paste(from_frame, (-x_off, 0))
                canvas.paste(to_frame, (w - x_off, 0))
            else:
                canvas.paste(from_frame, (x_off, 0))
                canvas.paste(to_frame, (-w + x_off, 0))
            _draw_pills(ImageDraw.Draw(canvas), w, n, anim_to, sessions)
            return canvas

        img = _render_detail_frame(w, h, sessions, idx, n)
        _draw_pills(ImageDraw.Draw(img), w, n, idx, sessions)
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
        old_idx = _get_selected()
        new_idx = max(0, min(old_idx + delta, n - 1))
        if new_idx == old_idx:
            return
        direction = -1 if delta > 0 else 1
        _set_selected(new_idx)
        _start_anim(old_idx, new_idx, direction)
        self.bus.publish("tick:boost", {
            "hz": 60.0,
            "until": time.monotonic() + SWIPE_ANIM_DURATION + 0.05,
        })


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
