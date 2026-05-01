"""Axl (Roaster) agent inspector — launcher, aggregate status, and session browser.

Five behaviors sharing module-level state from a background HTTP poller
against the Roaster status server (http://localhost:47338):

- axl_status_key     (key)         — Axl logo + thinking animation + count badge
- axl_aggregate      (key)         — compact session count indicator for key:5
- axl_session_strip  (strip)       — agent session detail card; tap cycles column profile
- axl_session_scroll (dial rotate) — scroll through agent sessions
- axl_session_focus  (dial press)  — focus Axl WPF window
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from behaviors.base import Behavior, EventBus, Target, TargetKind
from gfx import STRIP_BG, SWIPE_ANIM_DURATION, ease_back_out, font, font_semibold, font_semilight, strip_bg
from registry import register
from win_focus import find_window_by_title, focus_window


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AXL_BASE = "http://localhost:47338"
POLL_INTERVAL = 3.0
POLL_TIMEOUT = 2.0

AXL_GOLD     = (212, 175, 55)   # warm gold accent
STATUS_BLUE  = (100, 181, 246)  # running (non-thinking)
STATUS_GREEN = (100, 180, 100)
STATUS_RED   = (220, 70, 70)
PENDING_GREY = (120, 120, 120)
IDLE_GREY    = (96, 96, 96)
DIM_GREY     = (90, 90, 90)

FRAMES: tuple[str, ...] = ("✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳")

PILL_SIZE = 8
PILL_GAP  = 6
PILL_R    = 4
MAX_PILLS = 9

AXL_WINDOW_TITLE = "Axl"

_DAEMON_DIR = os.path.dirname(os.path.dirname(__file__))
_AXL_LOGO_PATH = Path(r"T:\Projects\axl\resources\axl.png")


# ---------------------------------------------------------------------------
# Symbol font (shared cache, same approach as redmatter_cms.py)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Logo cache
# ---------------------------------------------------------------------------

_logo_raw: Image.Image | None = None
_logo_loaded: bool = False


def _load_logo_raw() -> Image.Image | None:
    global _logo_raw, _logo_loaded
    if _logo_loaded:
        return _logo_raw
    _logo_loaded = True
    if not _AXL_LOGO_PATH.is_file():
        return None
    try:
        _logo_raw = Image.open(str(_AXL_LOGO_PATH)).convert("RGBA")
    except Exception:
        pass
    return _logo_raw


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class AxlSession:
    id: str
    provider: str
    model: str
    status: str          # "Running" | "Completed" | "Error"
    is_thinking: bool
    title: str
    duration_ms: int
    start_time: str


@dataclass
class _AxlState:
    sessions: list[AxlSession] = field(default_factory=list)
    thinking_count: int = 0
    running_count: int = 0


_lock = threading.Lock()
_state = _AxlState()


def _snap() -> _AxlState:
    with _lock:
        return _AxlState(
            sessions=list(_state.sessions),
            thinking_count=_state.thinking_count,
            running_count=_state.running_count,
        )


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

_poller_started = False
_poller_start_lock = threading.Lock()


def _parse_session(raw: dict) -> AxlSession:
    return AxlSession(
        id=str(raw.get("id", "")),
        provider=raw.get("provider") or "",
        model=raw.get("model") or "",
        status=raw.get("status") or "",
        is_thinking=bool(raw.get("is_thinking", False)),
        title=raw.get("title") or "",
        duration_ms=int(raw.get("duration_ms") or 0),
        start_time=raw.get("start_time") or "",
    )


def _poll_once(client) -> None:
    try:
        resp = client.get(f"{AXL_BASE}/api/sessions", timeout=POLL_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            sessions = [_parse_session(s) for s in data.get("sessions", [])]
            with _lock:
                _state.sessions = sessions
                _state.thinking_count = int(data.get("thinking_count", 0))
                _state.running_count = int(data.get("running_count", 0))
        else:
            with _lock:
                _state.sessions = []
                _state.thinking_count = 0
                _state.running_count = 0
    except Exception:
        with _lock:
            _state.sessions = []
            _state.thinking_count = 0
            _state.running_count = 0


def _poller_loop() -> None:
    import httpx
    client = httpx.Client()
    try:
        while True:
            _poll_once(client)
            time.sleep(POLL_INTERVAL)
    finally:
        client.close()


def _ensure_poller() -> None:
    global _poller_started
    with _poller_start_lock:
        if _poller_started:
            return
        _poller_started = True
        threading.Thread(target=_poller_loop, daemon=True, name="axl-poller").start()


# ---------------------------------------------------------------------------
# Shared animation state (strip scroll) — same pattern as redmatter_cms.py
# ---------------------------------------------------------------------------

_anim_lock = threading.Lock()
_selected_index: int = 0
_anim_from: int = 0
_anim_to: int = 0
_anim_start: float = 0.0
_anim_direction: int = 0


def _get_selected() -> int:
    with _anim_lock:
        return _selected_index


def _set_selected(idx: int) -> None:
    global _selected_index
    with _anim_lock:
        _selected_index = idx


def _start_anim(from_idx: int, to_idx: int, direction: int) -> None:
    global _anim_from, _anim_to, _anim_start, _anim_direction
    with _anim_lock:
        _anim_from = from_idx
        _anim_to = to_idx
        _anim_start = time.monotonic()
        _anim_direction = direction


def _is_animating() -> bool:
    with _anim_lock:
        if _anim_start == 0.0:
            return False
        return (time.monotonic() - _anim_start) < SWIPE_ANIM_DURATION


def _get_anim() -> tuple[int, int, float, int]:
    with _anim_lock:
        return _anim_from, _anim_to, _anim_start, _anim_direction


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _status_color(session: AxlSession) -> tuple[int, int, int]:
    if session.status == "Running":
        return AXL_GOLD if session.is_thinking else STATUS_BLUE
    if session.status == "Completed":
        return STATUS_GREEN
    if session.status == "Error":
        return STATUS_RED
    return PENDING_GREY


def _format_duration(ms: int) -> str:
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


def _truncate(draw: ImageDraw.ImageDraw, text: str, f, max_w: int) -> str:
    if draw.textlength(text, font=f) <= max_w:
        return text
    for end in range(len(text), 0, -1):
        candidate = text[:end] + "..."
        if draw.textlength(candidate, font=f) <= max_w:
            return candidate
    return "..."


def _blend(color: tuple[int, int, int], alpha: float) -> tuple[int, int, int]:
    bg_r, bg_g, bg_b = STRIP_BG
    r, g, b = color
    return (
        int(r * alpha + bg_r * (1 - alpha)),
        int(g * alpha + bg_g * (1 - alpha)),
        int(b * alpha + bg_b * (1 - alpha)),
    )


def _draw_pills(
    draw: ImageDraw.ImageDraw,
    w: int,
    n: int,
    selected_idx: int,
    sessions: list[AxlSession],
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
        base = _status_color(s)

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


def _clamped_index(n: int) -> int:
    if n == 0:
        return 0
    return max(0, min(_get_selected(), n - 1))


def _focus_axl() -> bool:
    hwnd = find_window_by_title(AXL_WINDOW_TITLE)
    if hwnd:
        focus_window(hwnd)
        return True
    print("[axl] focus: Axl window not found", flush=True)
    return False


# ---------------------------------------------------------------------------
# Detail frame rendering
# ---------------------------------------------------------------------------

def _render_detail_frame(
    w: int, h: int, sessions: list[AxlSession], idx: int, n: int,
) -> Image.Image:
    img = strip_bg(w, h)
    draw = ImageDraw.Draw(img)

    if n == 0:
        draw.rectangle((0, 0, w, 2), fill=AXL_GOLD)
        draw.text((w // 2, h // 2), "No sessions", fill=IDLE_GREY, font=font(11), anchor="mm")
        return img

    idx = max(0, min(idx, n - 1))
    s = sessions[idx]
    color = _status_color(s)

    draw.rectangle((0, 0, w, 2), fill=color)

    provider_text = s.provider or "Agent"
    tf = font_semibold(14)
    provider_text = _truncate(draw, provider_text, tf, w - 20)
    draw.text((10, 22), provider_text, fill=(255, 255, 255), font=tf, anchor="lm")

    status_label = "Thinking..." if s.is_thinking else (
        f"Running {_format_duration(s.duration_ms)}" if s.status == "Running"
        else f"Done {_format_duration(s.duration_ms)}" if s.status == "Completed"
        else s.status
    )
    draw.text((10, 42), status_label, fill=color, font=font(11), anchor="lm")

    if s.model:
        draw.text(
            (10, 60), s.model,
            fill=(70, 70, 70), font=font_semilight(10), anchor="lm",
        )

    return img


# ---------------------------------------------------------------------------
# Behavior: status key (key:1)
# ---------------------------------------------------------------------------

@register
class AxlStatusKey(Behavior):
    type_id = "axl_status_key"
    display_name = "Axl status"
    targets = {TargetKind.KEY}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()
        self._frame = 0
        self._prev_key: tuple = ()
        self._base: Image.Image | None = None

    def _build_base(self, w: int, h: int) -> Image.Image:
        pad = 20
        circ = w - 2 * pad
        ss = 4
        big = Image.new("L", (circ * ss, circ * ss), 0)
        ImageDraw.Draw(big).ellipse((0, 0, circ * ss - 1, circ * ss - 1), fill=255)
        circle_mask = big.resize((circ, circ), Image.LANCZOS)

        img = Image.new("RGB", (w, h), (0, 0, 0))
        logo = _load_logo_raw()
        if logo is not None:
            # Cover-scale: fill the circle, center-crop, clip with mask
            src_w, src_h = logo.size
            scale = max(circ / src_w, circ / src_h)
            new_w = max(circ, int(src_w * scale))
            new_h = max(circ, int(src_h * scale))
            scaled = logo.resize((new_w, new_h), Image.LANCZOS)
            cx = (new_w - circ) // 2
            cy = (new_h - circ) // 2
            cropped = scaled.crop((cx, cy, cx + circ, cy + circ)).convert("RGB")
            img.paste(cropped, (pad, pad), mask=circle_mask)

        return img

    def tick(self) -> bool:
        s = _snap()
        if s.thinking_count > 0:
            self._frame = (self._frame + 1) % len(FRAMES)
            return True
        key = (s.thinking_count, s.running_count)
        if key != self._prev_key:
            self._prev_key = key
            return True
        return False

    def render(self) -> Image.Image | None:
        w, h = self.size()
        s = _snap()

        if self._base is None:
            self._base = self._build_base(w, h)
        img = self._base.copy()

        draw = ImageDraw.Draw(img)
        if s.thinking_count > 0:
            glyph = FRAMES[self._frame]
            gf = _symbol_font(40)
            b = draw.textbbox((0, 0), glyph, font=gf)
            gx = w - (b[2] - b[0]) - 8 - b[0]
            gy = h - (b[3] - b[1]) - 8 - b[1]
            draw.text((gx, gy), glyph, fill=AXL_GOLD, font=gf)
            _draw_count_pill(draw, w, s.thinking_count)
        elif s.running_count > 0:
            _draw_count_pill(draw, w, s.running_count)

        return img

    def on_press(self) -> None:
        _focus_axl()


def _draw_count_pill(draw: ImageDraw.ImageDraw, key_w: int, count: int) -> None:
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


# ---------------------------------------------------------------------------
# Behavior: aggregate status key (key:5)
# ---------------------------------------------------------------------------

@register
class AxlAggregate(Behavior):
    type_id = "axl_aggregate"
    display_name = "Axl aggregate"
    targets = {TargetKind.KEY}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()
        self._frame = 0
        self._prev_key: tuple = ()

    def tick(self) -> bool:
        s = _snap()
        if s.thinking_count > 0:
            self._frame = (self._frame + 1) % len(FRAMES)
            return True
        key = (s.thinking_count, s.running_count)
        if key != self._prev_key:
            self._prev_key = key
            return True
        return False

    def render(self) -> Image.Image | None:
        w, h = self.size()
        s = _snap()
        img = Image.new("RGB", (w, h), (0, 0, 0))

        if s.running_count == 0 and s.thinking_count == 0:
            return img

        draw = ImageDraw.Draw(img)
        glyph_size = 72

        if s.thinking_count > 0:
            glyph = FRAMES[self._frame]
            color = AXL_GOLD
        else:
            glyph = FRAMES[0]
            color = STATUS_BLUE

        gf = _symbol_font(glyph_size)
        b = draw.textbbox((0, 0), glyph, font=gf)
        gx = (w - (b[2] - b[0])) // 2 - b[0]
        gy = (h - (b[3] - b[1])) // 2 - b[1]
        draw.text((gx, gy), glyph, fill=color, font=gf)

        count = s.thinking_count if s.thinking_count > 0 else s.running_count
        _draw_count_pill(draw, w, count)

        return img

    def on_press(self) -> None:
        _focus_axl()


# ---------------------------------------------------------------------------
# Behavior: session strip (strip:1)
# ---------------------------------------------------------------------------

@register
class AxlSessionStrip(Behavior):
    type_id = "axl_session_strip"
    display_name = "Axl session strip"
    targets = {TargetKind.STRIP_REGION}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()
        self._prev_key: tuple = ()

    def tick(self) -> bool:
        if _is_animating():
            return True
        s = _snap()
        n = len(s.sessions)
        idx = _clamped_index(n)
        if s.sessions:
            cur = s.sessions[idx]
            key = (cur.id, cur.status, cur.is_thinking, cur.duration_ms, n, idx)
        else:
            key = ()
        if key != self._prev_key:
            self._prev_key = key
            return True
        return False

    def render(self) -> Image.Image | None:
        w, h = self.size()
        s = _snap()
        sessions = s.sessions
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
            to_frame   = _render_detail_frame(w, h, sessions, anim_to,   n)

            canvas = Image.new("RGB", (w, h), STRIP_BG)
            if anim_dir < 0:
                canvas.paste(from_frame, (-x_off, 0))
                canvas.paste(to_frame,   (w - x_off, 0))
            else:
                canvas.paste(from_frame, (x_off, 0))
                canvas.paste(to_frame,   (-w + x_off, 0))
            _draw_pills(ImageDraw.Draw(canvas), w, n, anim_to, sessions)
            return canvas

        img = _render_detail_frame(w, h, sessions, idx, n)
        _draw_pills(ImageDraw.Draw(img), w, n, idx, sessions)
        return img

    def on_press(self) -> None:
        import column_mode
        profile = column_mode.cycle_col1()
        self.bus.publish("column:swap", {"column": 1, "profile": profile})


# ---------------------------------------------------------------------------
# Behavior: session scroll (dial:1 rotate)
# ---------------------------------------------------------------------------

@register
class AxlSessionScroll(Behavior):
    type_id = "axl_session_scroll"
    display_name = "Axl session scroll"
    targets = {TargetKind.DIAL_ROTATE}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()

    def on_rotate(self, delta: int) -> None:
        s = _snap()
        n = len(s.sessions)
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
# Behavior: session focus (dial:1 press)
# ---------------------------------------------------------------------------

@register
class AxlSessionFocus(Behavior):
    type_id = "axl_session_focus"
    display_name = "Axl session focus"
    targets = {TargetKind.DIAL_PRESS}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()

    def on_press(self) -> None:
        _focus_axl()
