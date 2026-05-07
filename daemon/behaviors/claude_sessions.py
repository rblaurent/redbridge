"""CodeRed — Claude session launcher, status indicator, and browser.

Five behaviors that share module-level state via a background HTTP poller:

- codered_launcher        (key)         — launch/focus CodeRed PWA
- claude_session_status   (key)         — MorphSpinner session indicator
- codered_session_strip   (strip)       — session card browser
- codered_session_scroll  (dial rotate) — scroll through sessions
- codered_session_focus   (dial press)  — focus CodeRed PWA
"""

from __future__ import annotations

import math
import os
import random
import subprocess
import threading
import time
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFilter

from behaviors.base import Behavior, EventBus, Target, TargetKind
from gfx import (
    STRIP_BG,
    SWIPE_ANIM_DURATION,
    ease_back_out,
    font,
    font_semibold,
    font_semilight,
    strip_bg,
)
from registry import register
from win_focus import find_window_by_title, focus_window


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REDCOMPUTE_BASE = "http://localhost:18800"
CODERED_DASHBOARD_URL = "http://localhost:18801"
CODERED_WINDOW_TITLE = "CodeRed"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

POLL_INTERVAL = 3.0
POLL_TIMEOUT = 2.0

# MorphSpinner colors (from CodeRed getSpinnerColor)
COLOR_ACTIVE = (124, 77, 255)
COLOR_IDLE = (38, 166, 154)
COLOR_STARTING = (212, 170, 79)
COLOR_STOPPED = (90, 90, 90)
COLOR_ERROR = (229, 91, 91)
COLOR_OFFLINE = (50, 50, 50)

DIM_GREY = (70, 70, 70)

PILL_SIZE = 8
PILL_GAP = 6
PILL_R = 4
MAX_PILLS = 9
MAX_STRIP_SESSIONS = 8

_ASSETS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")

# MorphSpinner geometry (pre-computed for 120×120 key)
MORPH_R = 35.0
MORPH_PTS = 18
MORPH_CX, MORPH_CY = 60.0, 60.0

JIGGLE_PERIOD = 0.6
MORPH_DURATION = 0.5
BOUNCE_DURATION = 0.6
COLOR_TRANSITION = 0.4
JIGGLE_BOOST_HZ = 20.0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ClaudeSession:
    id: str
    project_name: str
    project_path: str
    status: str
    model: str
    title: str
    message_count: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    started_at: str | None


@dataclass
class _SessionState:
    online: bool = False
    sessions: list[ClaudeSession] = field(default_factory=list)
    active_count: int = 0
    idle_count: int = 0
    starting_count: int = 0


_lock = threading.Lock()
_state = _SessionState()


def _snap() -> _SessionState:
    with _lock:
        return _SessionState(
            online=_state.online,
            sessions=list(_state.sessions),
            active_count=_state.active_count,
            idle_count=_state.idle_count,
            starting_count=_state.starting_count,
        )


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

_poller_started = False
_poller_start_lock = threading.Lock()
_poll_failures = 0
OFFLINE_THRESHOLD = 3


def _parse_session(raw: dict) -> ClaudeSession:
    return ClaudeSession(
        id=str(raw.get("id", "")),
        project_name=raw.get("projectName") or "",
        project_path=raw.get("projectPath") or "",
        status=raw.get("status") or "",
        model=raw.get("model") or "",
        title=raw.get("title") or "",
        message_count=raw.get("messageCount") or 0,
        cost_usd=raw.get("costUsd") or 0.0,
        input_tokens=raw.get("inputTokens") or 0,
        output_tokens=raw.get("outputTokens") or 0,
        started_at=raw.get("startedAt"),
    )


def _poll_once(client) -> None:
    global _poll_failures
    try:
        resp = client.get(f"{REDCOMPUTE_BASE}/status", timeout=POLL_TIMEOUT)
        online = resp.status_code == 200
    except Exception:
        online = False

    if not online:
        _poll_failures += 1
        if _poll_failures < OFFLINE_THRESHOLD:
            return
        with _lock:
            _state.online = False
            _state.sessions = []
            _state.active_count = 0
            _state.idle_count = 0
            _state.starting_count = 0
        return

    _poll_failures = 0

    sessions: list[ClaudeSession] = []
    active = idle = starting = 0

    try:
        resp2 = client.get(
            f"{REDCOMPUTE_BASE}/claude/sessions",
            timeout=POLL_TIMEOUT,
        )
        if resp2.status_code == 200:
            for raw in resp2.json():
                s = _parse_session(raw)
                sessions.append(s)
                if s.status == "Active":
                    active += 1
                elif s.status == "Idle":
                    idle += 1
                elif s.status == "Starting":
                    starting += 1
    except Exception:
        pass

    sessions.sort(key=lambda s: (s.project_name.lower(), s.id))

    with _lock:
        _state.online = True
        _state.sessions = sessions
        _state.active_count = active
        _state.idle_count = idle
        _state.starting_count = starting


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
        threading.Thread(
            target=_poller_loop, daemon=True, name="claude-session-poller",
        ).start()


# ---------------------------------------------------------------------------
# MorphSpinner shape generation (ported from morph-spinner.tsx)
# ---------------------------------------------------------------------------

def _ngon_r(n: int, start_deg: float, angle: float) -> float:
    start = math.radians(start_deg)
    sector = 2 * math.pi / n
    half = sector / 2
    adj = ((angle - start) % (2 * math.pi) + 2 * math.pi) % (2 * math.pi)
    return (MORPH_R * math.cos(half)) / math.cos((adj % sector) - half)


def _star_r(angle: float) -> float:
    inner_r = MORPH_R * 0.55
    sector = math.pi / 5
    adj = (angle % (2 * math.pi) + 2 * math.pi) % (2 * math.pi)
    idx = int(adj / sector)
    r1 = MORPH_R if idx % 2 == 0 else inner_r
    r2 = inner_r if idx % 2 == 0 else MORPH_R
    a1 = idx * sector
    a2 = (idx + 1) * sector
    x1 = r1 * math.sin(a1)
    y1 = -r1 * math.cos(a1)
    x2 = r2 * math.sin(a2)
    y2 = -r2 * math.cos(a2)
    dx = math.sin(angle)
    dy = -math.cos(angle)
    denom = (x2 - x1) * dy - (y2 - y1) * dx
    if abs(denom) < 1e-10:
        return (r1 + r2) / 2
    t = (y1 * dx - x1 * dy) / denom
    ix = x1 + t * (x2 - x1)
    iy = y1 + t * (y2 - y1)
    return math.sqrt(ix * ix + iy * iy)


def _build_shape(radius_fn) -> list[tuple[float, float]]:
    pts = []
    for i in range(MORPH_PTS):
        a = i * 2 * math.pi / MORPH_PTS
        r = radius_fn(a)
        pts.append((MORPH_CX + r * math.sin(a), MORPH_CY - r * math.cos(a)))
    return pts


SHAPES: dict[str, list[tuple[float, float]]] = {
    "triangle": _build_shape(lambda a: _ngon_r(3, 0, a)),
    "square": _build_shape(lambda a: _ngon_r(4, 45, a)),
    "pentagon": _build_shape(lambda a: _ngon_r(5, 0, a)),
    "hexagon": _build_shape(lambda a: _ngon_r(6, 0, a)),
    "diamond": _build_shape(lambda a: _ngon_r(4, 0, a)),
    "star": _build_shape(_star_r),
    "circle": _build_shape(lambda _a: MORPH_R),
}
SHAPE_NAMES = list(SHAPES.keys())


# ---------------------------------------------------------------------------
# Shape transform helpers
# ---------------------------------------------------------------------------

def _lerp_points(
    old: list[tuple[float, float]],
    new: list[tuple[float, float]],
    t: float,
) -> list[tuple[float, float]]:
    return [
        (ox + (nx - ox) * t, oy + (ny - oy) * t)
        for (ox, oy), (nx, ny) in zip(old, new)
    ]


def _lerp_color(
    c1: tuple[int, int, int],
    c2: tuple[int, int, int],
    t: float,
) -> tuple[int, int, int]:
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _transform_pts(
    pts: list[tuple[float, float]],
    sx: float, sy: float,
    rot_rad: float,
    tx: float, ty: float,
    cx: float = MORPH_CX, cy: float = MORPH_CY,
) -> list[tuple[float, float]]:
    cos_r = math.cos(rot_rad)
    sin_r = math.sin(rot_rad)
    result = []
    for x, y in pts:
        dx, dy = (x - cx) * sx, (y - cy) * sy
        result.append((
            cx + dx * cos_r - dy * sin_r + tx,
            cy + dx * sin_r + dy * cos_r + ty,
        ))
    return result


def _apply_jiggle(
    pts: list[tuple[float, float]], phase: float,
    cx: float = MORPH_CX, cy: float = MORPH_CY,
) -> list[tuple[float, float]]:
    if phase < 0.25:
        f = phase / 0.25
        scale = 1.0 + f * 0.06
        rot = f * (-4.0)
    elif phase < 0.5:
        f = (phase - 0.25) / 0.25
        scale = 1.06 - f * 0.09
        rot = -4.0 + f * 4.0
    elif phase < 0.75:
        f = (phase - 0.5) / 0.25
        scale = 0.97 + f * 0.07
        rot = f * 4.0
    else:
        f = (phase - 0.75) / 0.25
        scale = 1.04 - f * 0.04
        rot = 4.0 - f * 4.0
    return _transform_pts(pts, scale, scale, math.radians(rot), 0, 0, cx, cy)


_BOUNCE_KF = [
    (0.00,   0.0,    0, 1.0,  1.0),
    (0.12,   1.5,    0, 1.15, 0.8),
    (0.30, -15.0,  100, 0.9,  1.1),
    (0.50, -19.5,  200, 1.0,  1.0),
    (0.70,  -7.5,  310, 1.05, 0.95),
    (0.85,   0.0,  360, 1.2,  0.75),
    (0.93,   0.0,  360, 0.92, 1.08),
    (1.00,   0.0,  360, 1.0,  1.0),
]


def _apply_bounce(
    pts: list[tuple[float, float]], progress: float,
    cx: float = MORPH_CX, cy: float = MORPH_CY,
) -> list[tuple[float, float]]:
    prev = _BOUNCE_KF[0]
    nxt = _BOUNCE_KF[-1]
    for i in range(len(_BOUNCE_KF) - 1):
        if _BOUNCE_KF[i][0] <= progress <= _BOUNCE_KF[i + 1][0]:
            prev = _BOUNCE_KF[i]
            nxt = _BOUNCE_KF[i + 1]
            break

    span = nxt[0] - prev[0]
    f = (progress - prev[0]) / span if span > 0 else 1.0

    ty = prev[1] + (nxt[1] - prev[1]) * f
    rot = prev[2] + (nxt[2] - prev[2]) * f
    sx = prev[3] + (nxt[3] - prev[3]) * f
    sy = prev[4] + (nxt[4] - prev[4]) * f

    return _transform_pts(pts, sx, sy, math.radians(rot), 0, ty, cx, cy)


# ---------------------------------------------------------------------------
# Shared animation state (strip scroll)
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


def _start_strip_anim(from_idx: int, to_idx: int, direction: int) -> None:
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

def _status_color(status: str) -> tuple[int, int, int]:
    if status == "Active":
        return COLOR_ACTIVE
    if status == "Starting":
        return COLOR_STARTING
    if status == "Idle":
        return COLOR_IDLE
    if status == "Error":
        return COLOR_ERROR
    return COLOR_STOPPED


def _status_text(session: ClaudeSession) -> str:
    if session.status == "Active":
        return "Working..."
    if session.status == "Starting":
        return "Starting..."
    if session.status == "Idle":
        return "Idle"
    if session.status == "Stopped":
        return "Stopped"
    if session.status == "Error":
        return "Error"
    return session.status


def _format_cost(usd: float) -> str:
    if usd <= 0:
        return ""
    if usd < 0.01:
        return "<$0.01"
    return f"${usd:.2f}"


def _format_tokens(n: int) -> str:
    if n <= 0:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _truncate(draw: ImageDraw.ImageDraw, text: str, f, max_w: int) -> str:
    if draw.textlength(text, font=f) <= max_w:
        return text
    for end in range(len(text), 0, -1):
        candidate = text[:end] + "…"
        if draw.textlength(candidate, font=f) <= max_w:
            return candidate
    return "…"


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
    sessions: list[ClaudeSession],
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
        base = _status_color(s.status)

        selected = actual_idx == selected_idx
        alpha = 1.0 if selected else 0.5
        if (i == 0 and fade_left) or (i == vis_count - 1 and fade_right):
            alpha *= 0.4

        draw.rounded_rectangle(
            (px, y, px + PILL_SIZE, y + PILL_SIZE),
            PILL_R,
            fill=_blend(base, alpha),
        )


def _clamped_index(n: int) -> int:
    if n == 0:
        return 0
    return max(0, min(_get_selected(), n - 1))


def _draw_key_pill(draw: ImageDraw.ImageDraw, key_w: int, count: int) -> None:
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
# Focus / launch helpers
# ---------------------------------------------------------------------------

def _focus_codered() -> bool:
    hwnd = find_window_by_title(
        CODERED_WINDOW_TITLE, window_class="Chrome_WidgetWin_1",
    )
    if hwnd:
        focus_window(hwnd)
        return True
    return False


def _navigate_codered(session_id: str) -> None:
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", 18801), timeout=0.3)
        req = f"POST /api/navigate?session={session_id} HTTP/1.0\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n"
        s.sendall(req.encode())
        s.close()
    except Exception:
        pass


def _is_codered_open() -> int:
    return find_window_by_title(
        CODERED_WINDOW_TITLE, window_class="Chrome_WidgetWin_1",
    )


def _open_codered(session_id: str | None = None) -> None:
    hwnd = _is_codered_open()
    if session_id and hwnd:
        threading.Thread(target=focus_window, args=(hwnd,), daemon=True).start()
        _navigate_codered(session_id)
        return
    if session_id:
        subprocess.Popen([
            CHROME_PATH, f"--app={CODERED_DASHBOARD_URL}/?session={session_id}",
        ])
    elif not _focus_codered():
        subprocess.Popen([CHROME_PATH, f"--app={CODERED_DASHBOARD_URL}"])


# ---------------------------------------------------------------------------
# Detail frame rendering (strip card)
# ---------------------------------------------------------------------------

def _render_detail_frame(
    w: int, h: int, sessions: list[ClaudeSession], idx: int, n: int,
) -> Image.Image:
    img = strip_bg(w, h)
    draw = ImageDraw.Draw(img)

    if n == 0:
        draw.rectangle((0, 0, w, 2), fill=COLOR_STOPPED)
        draw.text(
            (w // 2, h // 2), "No sessions",
            fill=DIM_GREY, font=font(12), anchor="mm",
        )
        return img

    idx = max(0, min(idx, n - 1))
    s = sessions[idx]
    color = _status_color(s.status)

    draw.rectangle((0, 0, w, 2), fill=color)

    model_w = 0
    if s.model:
        mf = font_semilight(10)
        draw.text((w - 10, 22), s.model, fill=DIM_GREY, font=mf, anchor="rm")
        model_w = int(draw.textlength(s.model, font=mf)) + 14

    title_text = s.project_name or s.title or "Session"
    tf = font_semibold(14)
    title_text = _truncate(draw, title_text, tf, w - 20 - model_w)
    draw.text((10, 22), title_text, fill=(255, 255, 255), font=tf, anchor="lm")

    draw.text((10, 42), _status_text(s), fill=color, font=font(11), anchor="lm")

    parts = []
    cost_str = _format_cost(s.cost_usd)
    if cost_str:
        parts.append(cost_str)
    total_tokens = s.input_tokens + s.output_tokens
    token_str = _format_tokens(total_tokens)
    if token_str:
        parts.append(f"{token_str} tokens")
    if parts:
        draw.text(
            (10, 60), " · ".join(parts),
            fill=DIM_GREY, font=font_semilight(10), anchor="lm",
        )

    return img


# ---------------------------------------------------------------------------
# Behavior: CodeRed launcher (key:0)
# ---------------------------------------------------------------------------

@register
class CodeRedLauncher(Behavior):
    type_id = "codered_launcher"
    display_name = "CodeRed launcher"
    targets = {TargetKind.KEY}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()
        self._rendered: Image.Image | None = None
        self._prev_online: bool | None = None

    def tick(self) -> bool:
        s = _snap()
        if s.online != self._prev_online:
            self._prev_online = s.online
            self._rendered = None
            return True
        return False

    def render(self) -> Image.Image | None:
        if self._rendered:
            return self._rendered.copy()

        w, h = self.size()
        pad = 20
        circ = w - 2 * pad

        icon_path = os.path.join(_ASSETS, "codered_icon.png")
        try:
            src = Image.open(icon_path).convert("RGBA").resize(
                (circ, circ), Image.LANCZOS,
            )
        except Exception:
            src = Image.new("RGBA", (circ, circ), (229, 91, 91, 255))

        s = _snap()
        if not s.online:
            r, g, b, a = src.split()
            rgb = Image.merge("RGB", (r, g, b))
            rgb = rgb.point(lambda p: int(p * 0.3))
            src = rgb.convert("RGBA")
            src.putalpha(a)

        img = Image.new("RGB", (w, h), (0, 0, 0))
        img.paste(src, (pad, pad), mask=src)

        self._rendered = img
        return self._rendered.copy()

    def on_press(self) -> None:
        threading.Thread(target=_open_codered, daemon=True).start()


# ---------------------------------------------------------------------------
# Behavior: Claude session status (key:4) — MorphSpinner
# ---------------------------------------------------------------------------

@register
class ClaudeSessionStatus(Behavior):
    type_id = "claude_session_status"
    display_name = "Claude session status"
    targets = {TargetKind.KEY}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()
        self._shape = "square"
        self._prev_shape = "square"
        self._morph_start = 0.0

        self._dominant: tuple[int, int, int] | None = None
        self._prev_dominant: tuple[int, int, int] | None = None
        self._color_start = 0.0
        self._bounce_start = 0.0

        self._idle_count = 0
        self._active_count = 0
        self._has_sessions = False
        self._prev_key: tuple = ()

    def tick(self) -> bool:
        snap = _snap()
        active = snap.active_count
        idle = snap.idle_count
        starting = snap.starting_count
        has = bool(snap.sessions)

        if active > 0:
            new_color = COLOR_ACTIVE
        elif starting > 0:
            new_color = COLOR_STARTING
        elif idle > 0:
            new_color = COLOR_IDLE
        else:
            new_color = None

        if (
            new_color is not None
            and self._dominant is not None
            and new_color != self._dominant
        ):
            now = time.monotonic()
            self._prev_dominant = self._dominant
            self._color_start = now
            others = [s for s in SHAPE_NAMES if s != self._shape]
            self._prev_shape = self._shape
            self._shape = random.choice(others)
            self._morph_start = now
            self._bounce_start = now
            self.bus.publish("tick:boost", {
                "hz": 60.0, "until": now + BOUNCE_DURATION + 0.05,
            })

        self._dominant = new_color
        self._idle_count = idle
        self._active_count = active
        self._has_sessions = has

        if active > 0:
            self.bus.publish("tick:boost", {
                "hz": JIGGLE_BOOST_HZ,
                "until": time.monotonic() + 1.0,
            })
            return True

        now = time.monotonic()
        if (
            now - self._morph_start < MORPH_DURATION
            or now - self._bounce_start < BOUNCE_DURATION
            or now - self._color_start < COLOR_TRANSITION
        ):
            return True

        key = (new_color, idle, active, starting, has)
        if key != self._prev_key:
            self._prev_key = key
            return True
        return False

    def render(self) -> Image.Image | None:
        w, h = self.size()

        if not self._has_sessions or self._dominant is None:
            return Image.new("RGB", (w, h), (0, 0, 0))

        now = time.monotonic()
        cx, cy = w / 2, h / 2

        color = self._dominant
        if self._prev_dominant and now - self._color_start < COLOR_TRANSITION:
            t = (now - self._color_start) / COLOR_TRANSITION
            color = _lerp_color(self._prev_dominant, self._dominant, t)

        pts = list(SHAPES[self._shape])
        if now - self._morph_start < MORPH_DURATION:
            t = min(1.0, (now - self._morph_start) / MORPH_DURATION)
            pts = _lerp_points(SHAPES[self._prev_shape], pts, ease_back_out(t))

        if cx != MORPH_CX or cy != MORPH_CY:
            dx, dy = cx - MORPH_CX, cy - MORPH_CY
            pts = [(x + dx, y + dy) for x, y in pts]

        if self._active_count > 0:
            phase = (now % JIGGLE_PERIOD) / JIGGLE_PERIOD
            pts = _apply_jiggle(pts, phase, cx, cy)

        if now - self._bounce_start < BOUNCE_DURATION:
            t = (now - self._bounce_start) / BOUNCE_DURATION
            pts = _apply_bounce(pts, t, cx, cy)

        int_pts = [(int(x), int(y)) for x, y in pts]

        # Glow layer
        glow = Image.new("RGB", (w, h), (0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        expanded = [(int(cx + (x - cx) * 1.2), int(cy + (y - cy) * 1.2)) for x, y in pts]
        glow_draw.polygon(expanded, fill=color)
        glow = glow.filter(ImageFilter.GaussianBlur(radius=8))

        img = Image.new("RGB", (w, h), (0, 0, 0))
        img = Image.blend(img, glow, 0.45)

        draw = ImageDraw.Draw(img)
        draw.polygon(int_pts, fill=color)

        if self._idle_count > 0:
            _draw_key_pill(draw, w, self._idle_count)

        return img

    def on_press(self) -> None:
        threading.Thread(target=_open_codered, daemon=True).start()


# ---------------------------------------------------------------------------
# Behavior: session strip (strip:0)
# ---------------------------------------------------------------------------

@register
class CodeRedSessionStrip(Behavior):
    type_id = "codered_session_strip"
    display_name = "CodeRed sessions"
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
        sessions = s.sessions[:MAX_STRIP_SESSIONS]
        n = len(sessions)
        idx = _clamped_index(n)
        if not s.online:
            key = ("offline",)
        elif sessions:
            cur = sessions[idx]
            key = (
                cur.id, cur.status, cur.cost_usd,
                cur.input_tokens + cur.output_tokens, n, idx,
            )
        else:
            key = ("empty",)
        if key != self._prev_key:
            self._prev_key = key
            return True
        return False

    def render(self) -> Image.Image | None:
        w, h = self.size()
        s = _snap()

        if not s.online:
            img = strip_bg(w, h)
            draw = ImageDraw.Draw(img)
            draw.rectangle((0, 0, w, 2), fill=COLOR_ERROR)
            draw.text(
                (w // 2, h // 2), "RedCompute offline",
                fill=DIM_GREY, font=font(12), anchor="mm",
            )
            return img

        sessions = s.sessions[:MAX_STRIP_SESSIONS]
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
# Behavior: session scroll (dial:0 rotate)
# ---------------------------------------------------------------------------

@register
class CodeRedSessionScroll(Behavior):
    type_id = "codered_session_scroll"
    display_name = "CodeRed session scroll"
    targets = {TargetKind.DIAL_ROTATE}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()

    def on_rotate(self, delta: int) -> None:
        s = _snap()
        n = min(len(s.sessions), MAX_STRIP_SESSIONS)
        if n == 0:
            return
        old_idx = _get_selected()
        new_idx = max(0, min(old_idx + delta, n - 1))
        if new_idx == old_idx:
            return
        direction = -1 if delta > 0 else 1
        _set_selected(new_idx)
        _start_strip_anim(old_idx, new_idx, direction)
        self.bus.publish("tick:boost", {
            "hz": 60.0,
            "until": time.monotonic() + SWIPE_ANIM_DURATION + 0.05,
        })


# ---------------------------------------------------------------------------
# Behavior: session focus (dial:0 press)
# ---------------------------------------------------------------------------

@register
class CodeRedSessionFocus(Behavior):
    type_id = "codered_session_focus"
    display_name = "CodeRed session focus"
    targets = {TargetKind.DIAL_PRESS}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()

    def on_press(self) -> None:
        s = _snap()
        sessions = s.sessions[:MAX_STRIP_SESSIONS]
        n = len(sessions)
        session_id = None
        if n > 0:
            idx = _clamped_index(n)
            session_id = sessions[idx].id
        threading.Thread(
            target=_open_codered, args=(session_id,), daemon=True,
        ).start()
