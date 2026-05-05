"""RedCompute — launcher, job status timeline, and job browser.

Five behaviors that share module-level state via a background HTTP poller:

- redcompute_launcher     (key)         — launch/focus RedCompute window
- redcompute_job_status   (key)         — 4×4 activity timeline frieze
- redcompute_job_strip    (strip)       — job card browser (same layout as other profiles)
- redcompute_job_scroll   (dial rotate) — scroll through jobs
- redcompute_job_focus    (dial press)  — focus RedCompute window
"""

from __future__ import annotations

import io
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

REDCOMPUTE_BASE = "http://localhost:18800"
POLL_INTERVAL = 3.0
POLL_TIMEOUT = 2.0

# Frieze colors (matching RedCompute FriezeColors.cs)
COLOR_QUEUED = (0xFF, 0xB7, 0x4D)
COLOR_RUNNING = (0x43, 0xA2, 0x5A)
COLOR_COMPLETED = (0x26, 0xA6, 0x9A)
COLOR_FAILED = (0xFF, 0x52, 0x52)
COLOR_CANCELLED = (0x72, 0x76, 0x7D)
COLOR_IDLE = (0x2A, 0x2A, 0x2A)
COLOR_EMPTY = (0x2A, 0x2A, 0x2A)

IDLE_GREY = (96, 96, 96)
DIM_GREY = (70, 70, 70)

PILL_SIZE = 8
PILL_GAP = 6
PILL_R = 4
MAX_PILLS = 9

MAX_STRIP_JOBS = 8

CAP_ICON_SIZE = 14

REDCOMPUTE_EXE = r"T:\Projects\RedCompute\src\RedCompute.App\bin\Debug\net9.0-windows\RedCompute.exe"
REDCOMPUTE_DASHBOARD_URL = "http://localhost:18800"
REDCOMPUTE_DASHBOARD_TITLE = "RedCompute"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

_DAEMON_DIR = os.path.dirname(os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RedComputeJob:
    id: str
    name: str | None
    capability: str
    status: str
    queued_at: str | None
    started_at: str | None
    completed_at: str | None
    duration_ms: int | None


@dataclass
class _RedComputeState:
    online: bool = False
    jobs: list[RedComputeJob] = field(default_factory=list)
    running_count: int = 0
    queued_count: int = 0


_lock = threading.Lock()
_state = _RedComputeState()


def _snap() -> _RedComputeState:
    with _lock:
        return _RedComputeState(
            online=_state.online,
            jobs=list(_state.jobs),
            running_count=_state.running_count,
            queued_count=_state.queued_count,
        )


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

_poller_started = False
_poller_start_lock = threading.Lock()


def _parse_job(raw: dict) -> RedComputeJob:
    return RedComputeJob(
        id=str(raw.get("id", "")),
        name=raw.get("name"),
        capability=raw.get("capability") or "",
        status=raw.get("status") or "",
        queued_at=raw.get("queuedAt"),
        started_at=raw.get("startedAt"),
        completed_at=raw.get("completedAt"),
        duration_ms=raw.get("durationMs"),
    )


def _poll_once(client) -> None:
    try:
        resp = client.get(f"{REDCOMPUTE_BASE}/status", timeout=POLL_TIMEOUT)
        online = resp.status_code == 200
    except Exception:
        online = False

    if not online:
        with _lock:
            _state.online = False
            _state.jobs = []
            _state.running_count = 0
            _state.queued_count = 0
        return

    jobs: list[RedComputeJob] = []
    running = 0
    queued = 0

    try:
        resp2 = client.get(
            f"{REDCOMPUTE_BASE}/jobs",
            params={"limit": "50"},
            timeout=POLL_TIMEOUT,
        )
        if resp2.status_code == 200:
            for raw in resp2.json():
                job = _parse_job(raw)
                jobs.append(job)
                if job.status == "Running":
                    running += 1
                elif job.status == "Queued":
                    queued += 1
    except Exception:
        pass

    with _lock:
        _state.online = True
        _state.jobs = jobs
        _state.running_count = running
        _state.queued_count = queued


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
        threading.Thread(target=_poller_loop, daemon=True, name="redcompute-poller").start()


# ---------------------------------------------------------------------------
# Thumbnail cache for completed image-gen jobs
# ---------------------------------------------------------------------------

_thumb_cache: dict[str, Image.Image | None] = {}
_thumb_lock = threading.Lock()
_thumb_pending: set[str] = set()


def _fetch_thumb(job_id: str) -> None:
    import httpx
    try:
        resp = httpx.get(
            f"{REDCOMPUTE_BASE}/image-gen/jobs/{job_id}/output",
            timeout=5.0,
        )
        if resp.status_code == 200:
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            img.thumbnail((80, 80), Image.LANCZOS)
            with _thumb_lock:
                _thumb_cache[job_id] = img
        else:
            with _thumb_lock:
                _thumb_cache[job_id] = None
    except Exception:
        with _thumb_lock:
            _thumb_cache[job_id] = None
    finally:
        with _thumb_lock:
            _thumb_pending.discard(job_id)


def _get_thumb(job_id: str) -> Image.Image | None:
    with _thumb_lock:
        if job_id in _thumb_cache:
            return _thumb_cache[job_id]
        if job_id not in _thumb_pending:
            _thumb_pending.add(job_id)
            threading.Thread(target=_fetch_thumb, args=(job_id,), daemon=True).start()
    return None


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
    if status == "Running":
        return COLOR_RUNNING
    if status == "Queued":
        return COLOR_QUEUED
    if status == "Completed":
        return COLOR_COMPLETED
    if status == "Failed":
        return COLOR_FAILED
    if status == "Cancelled":
        return COLOR_CANCELLED
    return COLOR_IDLE


def _format_duration(ms: int | None) -> str:
    if ms is None:
        return ""
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


def _status_text(job: RedComputeJob) -> str:
    if job.status == "Running":
        dur = _format_duration(job.duration_ms)
        return f"Running · {dur}".rstrip(" ·") if dur else "Running..."
    if job.status == "Queued":
        return "Queued"
    if job.status == "Completed":
        dur = _format_duration(job.duration_ms)
        return f"Completed {dur}".rstrip()
    if job.status == "Failed":
        return "Failed"
    if job.status == "Cancelled":
        return "Cancelled"
    return job.status


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
    jobs: list[RedComputeJob],
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
        j = jobs[max(0, min(actual_idx, n - 1))]
        base = _status_color(j.status)

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


def _focus_redcompute() -> bool:
    hwnd = find_window_by_title(REDCOMPUTE_DASHBOARD_TITLE, window_class="Chrome_WidgetWin_1")
    if hwnd:
        focus_window(hwnd)
        return True
    return False


def _is_backend_up() -> bool:
    import httpx
    try:
        resp = httpx.get(f"{REDCOMPUTE_BASE}/status", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


def _start_and_open_dashboard() -> None:
    if not _is_backend_up():
        print("[redcompute] starting backend", flush=True)
        subprocess.Popen(
            [REDCOMPUTE_EXE],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(15):
            time.sleep(1)
            if _is_backend_up():
                break

    subprocess.Popen([CHROME_PATH, f"--app={REDCOMPUTE_DASHBOARD_URL}"])


# ---------------------------------------------------------------------------
# Capability icons (drawn as simple vector shapes)
# ---------------------------------------------------------------------------

_icon_cache: dict[str, Image.Image] = {}


def _draw_icon_volume(sz: int) -> Image.Image:
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Speaker body
    m = sz // 6
    d.rectangle((m, sz // 3, sz // 3, sz * 2 // 3), fill=(255, 255, 255, 180))
    # Speaker cone
    d.polygon([(sz // 3, sz // 3), (sz * 3 // 5, m), (sz * 3 // 5, sz - m), (sz // 3, sz * 2 // 3)],
              fill=(255, 255, 255, 180))
    # Sound waves
    d.arc((sz // 2, sz // 4, sz - m, sz * 3 // 4), -60, 60, fill=(255, 255, 255, 140), width=1)
    return img


def _draw_icon_image(sz: int) -> Image.Image:
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = sz // 6
    # Frame
    d.rounded_rectangle((m, m, sz - m, sz - m), 2, outline=(255, 255, 255, 180), width=1)
    # Mountain
    d.polygon([(m + 2, sz - m - 1), (sz // 2, sz // 2), (sz - m - 1, sz - m - 1)],
              fill=(255, 255, 255, 140))
    # Sun
    d.ellipse((sz * 2 // 3, m + 2, sz * 2 // 3 + 3, m + 5), fill=(255, 255, 255, 160))
    return img


def _draw_icon_music(sz: int) -> Image.Image:
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = sz // 5
    # Note stem
    d.line((sz * 2 // 3, m, sz * 2 // 3, sz * 3 // 4), fill=(255, 255, 255, 180), width=1)
    # Note head
    d.ellipse((sz // 3, sz * 5 // 8, sz * 2 // 3 + 1, sz - m), fill=(255, 255, 255, 180))
    # Flag
    d.arc((sz // 2, m, sz - m + 1, sz // 2), -90, 30, fill=(255, 255, 255, 160), width=1)
    return img


def _draw_icon_default(sz: int) -> Image.Image:
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = sz // 4
    # Cog circle
    d.ellipse((m, m, sz - m, sz - m), outline=(255, 255, 255, 140), width=1)
    d.ellipse((m + 2, m + 2, sz - m - 2, sz - m - 2), outline=(255, 255, 255, 100), width=1)
    return img


def _get_cap_icon(capability: str) -> Image.Image:
    if capability in _icon_cache:
        return _icon_cache[capability]
    sz = CAP_ICON_SIZE
    if capability == "tts":
        icon = _draw_icon_volume(sz)
    elif capability == "image-gen":
        icon = _draw_icon_image(sz)
    elif capability == "music-gen":
        icon = _draw_icon_music(sz)
    else:
        icon = _draw_icon_default(sz)
    _icon_cache[capability] = icon
    return icon


# ---------------------------------------------------------------------------
# Detail frame rendering (strip card)
# ---------------------------------------------------------------------------

def _render_detail_frame(
    w: int, h: int, jobs: list[RedComputeJob], idx: int, n: int,
) -> Image.Image:
    img = strip_bg(w, h)
    draw = ImageDraw.Draw(img)

    if n == 0:
        draw.rectangle((0, 0, w, 2), fill=COLOR_IDLE)
        draw.text(
            (w // 2, h // 2), "No active jobs",
            fill=IDLE_GREY, font=font(12), anchor="mm",
        )
        return img

    idx = max(0, min(idx, n - 1))
    job = jobs[idx]
    color = _status_color(job.status)

    # Show image thumbnail as 30% opacity background for completed image-gen jobs
    if job.status == "Completed" and job.capability == "image-gen":
        thumb = _get_thumb(job.id)
        if thumb is not None:
            tw = w
            th = int(thumb.height * w / thumb.width)
            if th < h:
                th = h
                tw = int(thumb.width * h / thumb.height)
            resized = thumb.resize((tw, th), Image.LANCZOS)
            cx = (w - tw) // 2
            cy = (h - th) // 2
            bg = Image.new("RGB", (w, h), STRIP_BG)
            bg.paste(resized, (cx, cy))
            img = Image.blend(img, bg, 0.3)
            draw = ImageDraw.Draw(img)

    draw.rectangle((0, 0, w, 2), fill=color)

    if job.capability:
        icon_img = _get_cap_icon(job.capability)
        icon_x = w - 10 - icon_img.width
        icon_y = 22 - icon_img.height // 2
        img.paste(icon_img, (icon_x, icon_y), icon_img)

    title_text = job.name or job.capability or "Job"
    tf = font_semibold(14)
    title_text = _truncate(draw, title_text, tf, w - 40)
    draw.text((10, 22), title_text, fill=(255, 255, 255), font=tf, anchor="lm")

    draw.text((10, 42), _status_text(job), fill=color, font=font(11), anchor="lm")

    if job.capability:
        draw.text(
            (10, 60), job.capability,
            fill=DIM_GREY, font=font_semilight(10), anchor="lm",
        )

    return img


# ---------------------------------------------------------------------------
# Timeline frieze for key:5 (4×4 grid)
# ---------------------------------------------------------------------------

TIMELINE_CELLS = 16
TIMELINE_ROWS = 4
TIMELINE_COLS = 4
QUANTUM_MS = 500


def _build_timeline(jobs: list[RedComputeJob]) -> list[tuple[int, int, int]]:
    """Build the last 16 cells of the unified activity timeline.

    Uses 500ms quanta (same as RedCompute's Job Inspector Activity frieze),
    and returns only the most recent 16 quanta.
    """
    if not jobs:
        return [COLOR_EMPTY] * TIMELINE_CELLS

    from datetime import datetime

    def _parse_ts(ts: str | None) -> float | None:
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return None

    now = time.time()

    intervals: list[tuple[float, float, str]] = []
    earliest = now

    for job in jobs:
        q_ts = _parse_ts(job.queued_at)
        s_ts = _parse_ts(job.started_at)
        c_ts = _parse_ts(job.completed_at)

        if q_ts is None:
            continue

        earliest = min(earliest, q_ts)

        if job.status == "Queued":
            intervals.append((q_ts, now, "Queued"))
            continue

        if s_ts and q_ts < s_ts:
            intervals.append((q_ts, s_ts, "Queued"))

        exec_start = s_ts or q_ts
        exec_end = c_ts or now
        intervals.append((exec_start, exec_end, job.status))

    total_span = now - earliest
    if total_span <= 0:
        return [COLOR_EMPTY] * TIMELINE_CELLS

    quantum_s = QUANTUM_MS / 1000.0
    total_quanta = max(1, int(total_span / quantum_s) + 1)

    # Take only the last 16 quanta
    skip = max(0, total_quanta - TIMELINE_CELLS)

    priority = {"Failed": 5, "Running": 4, "Queued": 3, "Completed": 2, "Cancelled": 1}

    cells: list[tuple[int, int, int]] = []
    for q in range(TIMELINE_CELLS):
        actual_q = skip + q
        q_start = earliest + actual_q * quantum_s
        q_end = q_start + quantum_s

        best_status = ""
        best_prio = 0

        for iv_start, iv_end, status in intervals:
            if iv_start < q_end and iv_end > q_start:
                p = priority.get(status, 0)
                if p > best_prio:
                    best_prio = p
                    best_status = status

        cells.append(_status_color(best_status) if best_status else COLOR_IDLE)

    return cells


# ---------------------------------------------------------------------------
# Behavior: launcher (key:1)
# ---------------------------------------------------------------------------

@register
class RedComputeLauncher(Behavior):
    type_id = "redcompute_launcher"
    display_name = "RedCompute launcher"
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
        ss = 4

        big = Image.new("L", (circ * ss, circ * ss), 0)
        ImageDraw.Draw(big).ellipse((0, 0, circ * ss - 1, circ * ss - 1), fill=255)
        circle_mask = big.resize((circ, circ), Image.LANCZOS)

        s = _snap()
        disc_color = COLOR_RUNNING if s.online else (50, 50, 50)
        disc = Image.new("RGB", (circ, circ), disc_color)

        icon_path = Path(_DAEMON_DIR) / "assets" / "redcompute.ico"
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
        if _focus_redcompute():
            return
        threading.Thread(
            target=_start_and_open_dashboard, daemon=True,
        ).start()


# ---------------------------------------------------------------------------
# Behavior: job status timeline (key:5) — 4×4 frieze
# ---------------------------------------------------------------------------

@register
class RedComputeJobStatus(Behavior):
    type_id = "redcompute_job_status"
    display_name = "RedCompute timeline"
    targets = {TargetKind.KEY}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()
        self._prev_key: tuple = ()

    def tick(self) -> bool:
        s = _snap()
        if not s.online:
            key = ("offline",)
        else:
            key = (time.monotonic() // 3, tuple((j.id, j.status) for j in s.jobs))
        if key != self._prev_key:
            self._prev_key = key
            return True
        return False

    def render(self) -> Image.Image | None:
        w, h = self.size()
        s = _snap()
        img = Image.new("RGB", (w, h), (0, 0, 0))

        if not s.online:
            return img

        draw = ImageDraw.Draw(img)
        cells = _build_timeline(s.jobs)

        cell_size = 14
        gap = 3
        grid_w = TIMELINE_COLS * cell_size + (TIMELINE_COLS - 1) * gap
        grid_h = TIMELINE_ROWS * cell_size + (TIMELINE_ROWS - 1) * gap
        x_off = (w - grid_w) // 2
        y_off = (h - grid_h) // 2

        for i, color in enumerate(cells):
            col = i % TIMELINE_COLS
            row = i // TIMELINE_COLS
            x = x_off + col * (cell_size + gap)
            y = y_off + row * (cell_size + gap)
            draw.rounded_rectangle(
                (x, y, x + cell_size, y + cell_size),
                2, fill=color,
            )

        return img

    def on_press(self) -> None:
        _focus_redcompute()


# ---------------------------------------------------------------------------
# Behavior: job strip (strip:1) — card browser
# ---------------------------------------------------------------------------

@register
class RedComputeJobStrip(Behavior):
    type_id = "redcompute_job_strip"
    display_name = "RedCompute jobs"
    targets = {TargetKind.STRIP_REGION}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()
        self._prev_key: tuple = ()

    def on_press(self) -> None:
        import column_mode
        profile = column_mode.cycle_col1()
        self.bus.publish("column:swap", {"column": 1, "profile": profile})

    def tick(self) -> bool:
        if _is_animating():
            return True
        s = _snap()
        jobs = s.jobs[:MAX_STRIP_JOBS]
        n = len(jobs)
        idx = _clamped_index(n)
        if not s.online:
            key = ("offline",)
        elif jobs:
            cur = jobs[idx]
            has_thumb = cur.id in _thumb_cache if (cur.status == "Completed" and cur.capability == "image-gen") else False
            key = (cur.id, cur.status, cur.duration_ms, n, idx, has_thumb)
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
            draw.rectangle((0, 0, w, 2), fill=COLOR_FAILED)
            draw.text(
                (w // 2, h // 2), "RedCompute offline",
                fill=DIM_GREY, font=font(12), anchor="mm",
            )
            return img

        jobs = s.jobs[:MAX_STRIP_JOBS]
        n = len(jobs)
        idx = _clamped_index(n)

        anim_from, anim_to, anim_start, anim_dir = _get_anim()
        now = time.monotonic()
        elapsed = now - anim_start if anim_start > 0 else SWIPE_ANIM_DURATION + 1

        if elapsed < SWIPE_ANIM_DURATION and n > 0:
            t = min(1.0, elapsed / SWIPE_ANIM_DURATION)
            eased = ease_back_out(t)
            x_off = int(w * eased)

            from_frame = _render_detail_frame(w, h, jobs, anim_from, n)
            to_frame = _render_detail_frame(w, h, jobs, anim_to, n)

            canvas = Image.new("RGB", (w, h), STRIP_BG)
            if anim_dir < 0:
                canvas.paste(from_frame, (-x_off, 0))
                canvas.paste(to_frame, (w - x_off, 0))
            else:
                canvas.paste(from_frame, (x_off, 0))
                canvas.paste(to_frame, (-w + x_off, 0))
            _draw_pills(ImageDraw.Draw(canvas), w, n, anim_to, jobs)
            return canvas

        img = _render_detail_frame(w, h, jobs, idx, n)
        _draw_pills(ImageDraw.Draw(img), w, n, idx, jobs)
        return img


# ---------------------------------------------------------------------------
# Behavior: job scroll (dial:1 rotate)
# ---------------------------------------------------------------------------

@register
class RedComputeJobScroll(Behavior):
    type_id = "redcompute_job_scroll"
    display_name = "RedCompute job scroll"
    targets = {TargetKind.DIAL_ROTATE}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()

    def on_rotate(self, delta: int) -> None:
        s = _snap()
        n = min(len(s.jobs), MAX_STRIP_JOBS)
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
# Behavior: job focus (dial:1 press)
# ---------------------------------------------------------------------------

@register
class RedComputeJobFocus(Behavior):
    type_id = "redcompute_job_focus"
    display_name = "RedCompute job focus"
    targets = {TargetKind.DIAL_PRESS}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()

    def on_press(self) -> None:
        _focus_redcompute()
