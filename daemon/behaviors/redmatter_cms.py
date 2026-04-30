"""RedMatter CMS — launcher, AI status, and session browser.

Five behaviors that share module-level state via a background HTTP poller:

- redmatter_launcher        (key)         — start backend+frontend, launch/focus Chrome PWA
- redmatter_ai_status       (key)         — orchestrator + session status indicator
- redmatter_session_strip   (strip)       — AI session browser card
- redmatter_session_scroll  (dial rotate) — scroll through AI sessions
- redmatter_session_focus   (dial press)  — focus CMS Chrome window
"""

from __future__ import annotations

import os
import subprocess
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

REDMATTER_BASE = "http://localhost:5001"
POLL_INTERVAL = 3.0
POLL_TIMEOUT = 2.0

ACCENT_RED = (193, 18, 31)
STATUS_GREEN = (100, 180, 100)
STATUS_RED = (220, 70, 70)
PENDING_GREY = (120, 120, 120)
IDLE_GREY = (96, 96, 96)
DIM_GREY = (90, 90, 90)

FRAMES: tuple[str, ...] = ("✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳")

PILL_SIZE = 8
PILL_GAP = 6
PILL_R = 4
MAX_PILLS = 9

CMS_WINDOW_TITLE = "RedMatter CMS"
CMS_WINDOW_CLASS = "Chrome_WidgetWin_1"

CMS_DIR = r"T:\Projects\RedMatter\cms"
BACKEND_DIR = r"T:\Projects\RedMatter\cms\backend"
FRONTEND_DIR = r"T:\Projects\RedMatter\cms\frontend"
BACKEND_CHECK = f"{REDMATTER_BASE}/api/ping"
FRONTEND_CHECK = "http://localhost:5173"
POSTGRES_CHECK = ("localhost", 5432)
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome_proxy.exe"
CHROME_ARGS = ["--profile-directory=Default", "--app-id=idemibpphagihbobmgmaojhjfidlfpdl"]

_DAEMON_DIR = os.path.dirname(os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Symbol font (same approach as claude_code_idle.py)
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
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RedMatterSession:
    id: str
    flow_type: str
    status: str
    model: str
    agent_role_slug: str
    duration_ms: int | None
    started_at: str
    completed_at: str | None


@dataclass
class _RedMatterState:
    orch_online: bool = False
    orch_state: str = ""
    orch_paused: bool = False
    orch_enabled: bool = False
    sessions: list[RedMatterSession] = field(default_factory=list)
    running_count: int = 0


_lock = threading.Lock()
_state = _RedMatterState()


def _snap() -> _RedMatterState:
    with _lock:
        return _RedMatterState(
            orch_online=_state.orch_online,
            orch_state=_state.orch_state,
            orch_paused=_state.orch_paused,
            orch_enabled=_state.orch_enabled,
            sessions=list(_state.sessions),
            running_count=_state.running_count,
        )


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

_poller_started = False
_poller_start_lock = threading.Lock()


def _parse_session(raw: dict) -> RedMatterSession:
    return RedMatterSession(
        id=str(raw.get("id", "")),
        flow_type=raw.get("flow_type") or "",
        status=raw.get("status") or "",
        model=raw.get("model") or "",
        agent_role_slug=raw.get("agent_role_slug") or "",
        duration_ms=raw.get("duration_ms"),
        started_at=raw.get("started_at") or "",
        completed_at=raw.get("completed_at"),
    )


def _poll_once(client) -> None:
    try:
        resp = client.get(
            f"{REDMATTER_BASE}/api/orchestrator/status",
            timeout=POLL_TIMEOUT,
        )
        if resp.status_code == 200:
            status = resp.json().get("status", {})
            with _lock:
                _state.orch_online = True
                _state.orch_state = status.get("state", "")
                _state.orch_paused = status.get("paused", False)
                _state.orch_enabled = status.get("enabled", False)
        else:
            with _lock:
                _state.orch_online = False
    except Exception:
        with _lock:
            _state.orch_online = False

    try:
        resp2 = client.get(
            f"{REDMATTER_BASE}/api/ai/sessions",
            params={"status": "running", "limit": 20},
            timeout=POLL_TIMEOUT,
        )
        if resp2.status_code == 200:
            raw_sessions = resp2.json().get("sessions", [])
            sessions = [_parse_session(s) for s in raw_sessions]
            sessions.sort(key=lambda s: s.started_at)
            with _lock:
                _state.sessions = sessions
                _state.running_count = len(sessions)
        else:
            with _lock:
                _state.sessions = []
                _state.running_count = 0
    except Exception:
        with _lock:
            _state.sessions = []
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
        threading.Thread(target=_poller_loop, daemon=True, name="redmatter-poller").start()


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

def _status_color(status: str) -> tuple[int, int, int]:
    if status == "running":
        return ACCENT_RED
    if status == "completed":
        return STATUS_GREEN
    if status == "failed":
        return STATUS_RED
    return PENDING_GREY


def _format_duration(ms: int | None) -> str:
    if ms is None:
        return ""
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


def _status_text(session: RedMatterSession) -> str:
    dur = _format_duration(session.duration_ms)
    if session.status == "running":
        return f"Running... {dur}".rstrip()
    if session.status == "completed":
        return f"Completed {dur}".rstrip()
    if session.status == "failed":
        return "Failed"
    return "Pending"


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
    draw: ImageDraw.ImageDraw, w: int, n: int, selected_idx: int,
    sessions: list[RedMatterSession],
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

        color = _blend(base, alpha)
        draw.rounded_rectangle(
            (px, y, px + PILL_SIZE, y + PILL_SIZE),
            PILL_R,
            fill=color,
        )


def _clamped_index(n: int) -> int:
    if n == 0:
        return 0
    idx = _get_selected()
    return max(0, min(idx, n - 1))


def _focus_cms() -> bool:
    hwnd = find_window_by_title(CMS_WINDOW_TITLE, window_class=CMS_WINDOW_CLASS)
    if hwnd:
        focus_window(hwnd)
        return True
    print("[redmatter] focus: CMS window not found", flush=True)
    return False


# ---------------------------------------------------------------------------
# Detail frame rendering
# ---------------------------------------------------------------------------

def _render_detail_frame(
    w: int, h: int, sessions: list[RedMatterSession], idx: int, n: int,
) -> Image.Image:
    img = strip_bg(w, h)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, w, 2), fill=ACCENT_RED)

    if n == 0:
        return img

    idx = max(0, min(idx, n - 1))
    s = sessions[idx]
    color = _status_color(s.status)

    title_parts = [s.flow_type]
    if s.agent_role_slug:
        title_parts.append(s.agent_role_slug)
    title_text = " · ".join(title_parts)

    tf = font_semibold(14)
    title_text = _truncate(draw, title_text, tf, w - 20)
    draw.text((10, 22), title_text, fill=(255, 255, 255), font=tf, anchor="lm")

    draw.text((10, 42), _status_text(s), fill=color, font=font(11), anchor="lm")

    if s.model:
        draw.text(
            (10, 60), s.model,
            fill=(70, 70, 70), font=font_semilight(10), anchor="lm",
        )

    return img


# ---------------------------------------------------------------------------
# Behavior: launcher (key:1)
# ---------------------------------------------------------------------------

def _is_http_up(url: str) -> bool:
    import httpx
    try:
        resp = httpx.get(url, timeout=1.0)
        return resp.status_code < 500
    except Exception:
        return False


def _is_port_open(host: str, port: int) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except Exception:
        return False


def _start_services_and_launch() -> None:
    if not _is_port_open(*POSTGRES_CHECK):
        print("[redmatter] starting postgres: docker compose up -d", flush=True)
        subprocess.Popen(
            ["docker", "compose", "up", "-d"],
            cwd=CMS_DIR,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(15):
            time.sleep(1)
            if _is_port_open(*POSTGRES_CHECK):
                break

    if not _is_http_up(BACKEND_CHECK):
        print("[redmatter] starting backend: dotnet run", flush=True)
        log = open(os.path.join(BACKEND_DIR, "backend.log"), "w")
        subprocess.Popen(
            ["dotnet", "run"],
            cwd=BACKEND_DIR,
            stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    if not _is_http_up(FRONTEND_CHECK):
        print("[redmatter] starting frontend: npm run dev", flush=True)
        log = open(os.path.join(FRONTEND_DIR, "frontend.log"), "w")
        subprocess.Popen(
            ["npm", "run", "dev"],
            cwd=FRONTEND_DIR,
            stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW,
            shell=True,
        )

    subprocess.Popen([CHROME_PATH] + CHROME_ARGS)


@register
class RedMatterLauncher(Behavior):
    type_id = "redmatter_launcher"
    display_name = "RedMatter launcher"
    targets = {TargetKind.KEY}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        self._rendered: Image.Image | None = None

    def render(self) -> Image.Image | None:
        if self._rendered:
            return self._rendered.copy()

        w, h = self.size()
        pad = 20
        circ = w - 2 * pad
        ss = 4

        big = Image.new("L", (circ * ss, circ * ss), 0)
        ImageDraw.Draw(big).ellipse((0, 0, circ * ss - 1, circ * ss - 1), fill=255)
        circle_mask = big.resize((circ, circ), Image.LANCZOS)

        disc = Image.new("RGB", (circ, circ), (50, 50, 50))

        icon_path = Path(_DAEMON_DIR) / "assets" / "redmatter_cms.ico"
        if icon_path.is_file():
            try:
                icon = Image.open(str(icon_path)).convert("RGBA")
                icon_sz = int(circ * 0.75)
                icon.thumbnail((icon_sz, icon_sz), Image.LANCZOS)
                ix = (circ - icon.width) // 2
                iy = (circ - icon.height) // 2
                disc.paste(icon, (ix, iy), icon)
            except Exception:
                pass

        img = Image.new("RGB", (w, h), (0, 0, 0))
        img.paste(disc, (pad, pad), mask=circle_mask)

        self._rendered = img
        return self._rendered.copy()

    def on_press(self) -> None:
        hwnd = find_window_by_title(CMS_WINDOW_TITLE, window_class=CMS_WINDOW_CLASS)
        if hwnd:
            focus_window(hwnd)
            return
        threading.Thread(
            target=_start_services_and_launch, daemon=True,
        ).start()


# ---------------------------------------------------------------------------
# Behavior: AI status key (key:5)
# ---------------------------------------------------------------------------

@register
class RedMatterAiStatus(Behavior):
    type_id = "redmatter_ai_status"
    display_name = "RedMatter AI status"
    targets = {TargetKind.KEY}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()
        self._frame = 0
        self._prev_key: tuple = ()

    def tick(self) -> bool:
        s = _snap()
        if not s.orch_online:
            key = ("offline",)
        elif s.running_count > 0:
            self._frame = (self._frame + 1) % len(FRAMES)
            self._prev_key = ("active", s.running_count, self._frame)
            return True
        elif s.orch_paused:
            key = ("paused",)
        else:
            key = ("idle",)
        if key != self._prev_key:
            self._prev_key = key
            return True
        return False

    def render(self) -> Image.Image | None:
        w, h = self.size()
        s = _snap()
        img = Image.new("RGB", (w, h), (0, 0, 0))

        if not s.orch_online:
            return img

        draw = ImageDraw.Draw(img)
        glyph_size = 72

        if s.running_count > 0:
            glyph = FRAMES[self._frame]
            color = ACCENT_RED
        elif s.orch_paused:
            glyph = FRAMES[0]
            color = PENDING_GREY
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

        if s.running_count > 0:
            self._draw_pill(draw, w, s.running_count)

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
        _focus_cms()


# ---------------------------------------------------------------------------
# Behavior: session strip (strip:1)
# ---------------------------------------------------------------------------

@register
class RedMatterSessionStrip(Behavior):
    type_id = "redmatter_session_strip"
    display_name = "RedMatter AI session"
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
        sessions = s.sessions
        n = len(sessions)
        idx = _clamped_index(n)
        if sessions:
            cur = sessions[idx]
            key = (cur.id, cur.status, cur.duration_ms, cur.model, n, idx)
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
# Behavior: session scroll (dial:1 rotate)
# ---------------------------------------------------------------------------

@register
class RedMatterSessionScroll(Behavior):
    type_id = "redmatter_session_scroll"
    display_name = "RedMatter session scroll"
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
class RedMatterSessionFocus(Behavior):
    type_id = "redmatter_session_focus"
    display_name = "RedMatter session focus"
    targets = {TargetKind.DIAL_PRESS}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()

    def on_press(self) -> None:
        _focus_cms()
