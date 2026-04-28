"""Workspace launcher — pick a project folder and open Claude Code.

One registered behavior (key toggle) plus three internal overlay behaviors
that temporarily replace strip:0, dial:0 rotate, and dial:0 press while
the picker is active.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from PIL import Image, ImageDraw

from behaviors.base import Behavior, EventBus, Target, TargetKind
from gfx import STRIP_BG, SWIPE_ANIM_DURATION, ease_back_out, font, font_semibold, font_semilight, strip_bg
from registry import register

PROJECTS_ROOT = r"T:\Projects"
_ASSETS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")

CLAUDE_ORANGE = (193, 95, 60)
DIM_GREY = (90, 90, 90)
_TIMEOUT_S: float = 5.0

PILL_SIZE = 8
PILL_GAP = 6
PILL_R = 4
MAX_PILLS = 9

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_picker_active: bool = False
_selected_index: int = 0
_workspaces: list[str] = []
_overlay_refs: dict | None = None
_last_interaction: float = 0.0

_ws_anim_from: int = 0
_ws_anim_to: int = 0
_ws_anim_start: float = 0.0
_ws_anim_direction: int = 0


def _get_active() -> bool:
    with _state_lock:
        return _picker_active


def _get_selected() -> int:
    with _state_lock:
        return _selected_index


def _set_selected(idx: int) -> None:
    global _selected_index
    with _state_lock:
        _selected_index = idx


def _get_workspaces() -> list[str]:
    with _state_lock:
        return list(_workspaces)


def _touch_interaction() -> None:
    global _last_interaction
    with _state_lock:
        _last_interaction = time.monotonic()


def _start_ws_anim(from_idx: int, to_idx: int, direction: int) -> None:
    global _ws_anim_from, _ws_anim_to, _ws_anim_start, _ws_anim_direction
    with _state_lock:
        _ws_anim_from = from_idx
        _ws_anim_to = to_idx
        _ws_anim_start = time.monotonic()
        _ws_anim_direction = direction


def _is_ws_animating() -> bool:
    with _state_lock:
        if _ws_anim_start == 0.0:
            return False
        return (time.monotonic() - _ws_anim_start) < SWIPE_ANIM_DURATION


def _get_ws_anim() -> tuple[int, int, float, int]:
    with _state_lock:
        return _ws_anim_from, _ws_anim_to, _ws_anim_start, _ws_anim_direction


def _close_picker(bus: EventBus) -> None:
    global _picker_active, _overlay_refs, _ws_anim_start
    with _state_lock:
        _picker_active = False
        _overlay_refs = None
        _ws_anim_start = 0.0
    bus.publish("overlay:clear", {
        "strip": [0],
        "dial_rotate": [0],
        "dial_press": [0],
    })
    bus.publish("tick:boost", {"hz": 0, "until": 0})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scan_workspaces() -> list[str]:
    try:
        entries = os.listdir(PROJECTS_ROOT)
    except OSError:
        return []
    dirs = [e for e in entries if os.path.isdir(os.path.join(PROJECTS_ROOT, e))]
    dirs.sort(key=str.lower)
    return dirs


def _launch_claude_code(folder: str) -> None:
    full_path = os.path.join(PROJECTS_ROOT, folder)
    subprocess.Popen(
        f'cmd /k cd /d "{full_path}" && npx @anthropic-ai/claude-code --dangerously-skip-permissions',
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


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

def _draw_pills(draw: ImageDraw.ImageDraw, w: int, n: int, selected_idx: int) -> None:
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

        if actual_idx == selected_idx:
            color = CLAUDE_ORANGE
        else:
            color = (30, 30, 32)

        if (i == 0 and fade_left) or (i == vis_count - 1 and fade_right):
            r, g, b = color
            bg_r, bg_g, bg_b = STRIP_BG
            a = 0.4
            color = (
                int(r * a + bg_r * (1 - a)),
                int(g * a + bg_g * (1 - a)),
                int(b * a + bg_b * (1 - a)),
            )

        draw.rounded_rectangle(
            (px, y, px + PILL_SIZE, y + PILL_SIZE),
            PILL_R,
            fill=color,
        )


# ---------------------------------------------------------------------------
# Workspace detail frame rendering
# ---------------------------------------------------------------------------

def _render_ws_frame(
    w: int, h: int, workspaces: list[str], idx: int, n: int,
) -> Image.Image:
    img = strip_bg(w, h)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, w, 2), fill=CLAUDE_ORANGE)

    if n == 0:
        draw.text(
            (w // 2, h // 2), "No projects found",
            fill=(70, 70, 70), font=font(14), anchor="mm",
        )
        return img

    idx = max(0, min(idx, n - 1))
    folder = workspaces[idx]

    tf = font_semibold(14)
    name = _truncate(draw, folder, tf, w - 20)
    draw.text((10, 22), name, fill=(255, 255, 255), font=tf, anchor="lm")

    pf = font(11)
    path_text = _truncate(draw, os.path.join(PROJECTS_ROOT, folder), pf, w - 20)
    draw.text((10, 42), path_text, fill=(120, 120, 120), font=pf, anchor="lm")

    draw.text(
        (10, 65), "Press dial to launch",
        fill=(70, 70, 70), font=font_semilight(10), anchor="lm",
    )

    return img


# ---------------------------------------------------------------------------
# Key toggle (registered)
# ---------------------------------------------------------------------------

@register
class WorkspaceLauncherToggle(Behavior):
    type_id = "workspace_launcher_toggle"
    display_name = "Workspace launcher"
    targets = {TargetKind.KEY}
    config_schema = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        self._prev_active: bool = False
        self._logo: Image.Image | None = None
        self._logo_active: Image.Image | None = None

    def _build_logo(self) -> None:
        w, h = self.size()
        pad = 20
        circ = w - 2 * pad
        logo_path = os.path.join(_ASSETS, "claude_logo.webp")
        try:
            src = Image.open(logo_path).convert("RGB").resize((circ, circ), Image.LANCZOS)
        except Exception:
            src = Image.new("RGB", (circ, circ), CLAUDE_ORANGE)

        ss = 4
        big = Image.new("L", (circ * ss, circ * ss), 0)
        ImageDraw.Draw(big).ellipse((0, 0, circ * ss - 1, circ * ss - 1), fill=255)
        mask = big.resize((circ, circ), Image.LANCZOS)

        inactive = Image.new("RGB", (w, h), (0, 0, 0))
        inactive.paste(src, (pad, pad), mask=mask)
        self._logo = inactive

        big_ring = Image.new("L", (circ * ss, circ * ss), 0)
        ImageDraw.Draw(big_ring).ellipse(
            (0, 0, circ * ss - 1, circ * ss - 1),
            outline=255, width=3 * ss,
        )
        ring_mask = big_ring.resize((circ, circ), Image.LANCZOS)

        active = inactive.copy()
        ring = Image.new("RGB", (circ, circ), (255, 255, 255))
        active.paste(ring, (pad, pad), mask=ring_mask)
        self._logo_active = active

    def on_press(self) -> None:
        global _picker_active, _selected_index, _workspaces, _overlay_refs

        if _get_active():
            _close_picker(self.bus)
            return

        ws = _scan_workspaces()
        with _state_lock:
            _workspaces = ws
            _selected_index = 0
            _picker_active = True
        _touch_interaction()
        self.bus.publish("tick:boost", {"hz": 60.0, "until": time.monotonic() + 60})

        strip = _WorkspaceStrip(
            Target(TargetKind.STRIP_REGION, 0), {}, self.bus,
        )
        scroll = _WorkspaceScroll(
            Target(TargetKind.DIAL_ROTATE, 0), {}, self.bus,
        )
        launch = _WorkspaceLaunch(
            Target(TargetKind.DIAL_PRESS, 0), {}, self.bus,
        )

        with _state_lock:
            _overlay_refs = {
                "strip": strip,
                "scroll": scroll,
                "launch": launch,
            }

        self.bus.publish("overlay:set", {
            "strip": {0: strip},
            "dial_rotate": {0: scroll},
            "dial_press": {0: launch},
        })

    def tick(self) -> bool:
        active = _get_active()
        if active != self._prev_active:
            self._prev_active = active
            return True
        return False

    def render(self) -> Image.Image | None:
        if self._logo is None:
            self._build_logo()
        if _get_active():
            return self._logo_active.copy()
        return self._logo.copy()


# ---------------------------------------------------------------------------
# Strip (overlay strip:0 — consolidated)
# ---------------------------------------------------------------------------

class _WorkspaceStrip(Behavior):
    type_id = "_ws_strip"
    targets = {TargetKind.STRIP_REGION}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        self._prev_key: tuple = ()

    def tick(self) -> bool:
        if not _get_active():
            return False
        with _state_lock:
            elapsed = time.monotonic() - _last_interaction
        if elapsed >= _TIMEOUT_S:
            _close_picker(self.bus)
            return False
        if _is_ws_animating():
            return True
        ws = _get_workspaces()
        idx = _get_selected()
        key = (tuple(ws), idx)
        if key != self._prev_key:
            self._prev_key = key
            return True
        return False

    def render(self) -> Image.Image | None:
        w, h = self.size()
        ws = _get_workspaces()
        n = len(ws)
        idx = max(0, min(_get_selected(), n - 1)) if n else 0

        anim_from, anim_to, anim_start, anim_dir = _get_ws_anim()
        now = time.monotonic()
        elapsed = now - anim_start if anim_start > 0 else SWIPE_ANIM_DURATION + 1

        if elapsed < SWIPE_ANIM_DURATION and n > 0:
            t = min(1.0, elapsed / SWIPE_ANIM_DURATION)
            eased = ease_back_out(t)
            x_off = int(w * eased)

            from_frame = _render_ws_frame(w, h, ws, anim_from, n)
            to_frame = _render_ws_frame(w, h, ws, anim_to, n)

            canvas = Image.new("RGB", (w, h), STRIP_BG)
            if anim_dir < 0:
                canvas.paste(from_frame, (-x_off, 0))
                canvas.paste(to_frame, (w - x_off, 0))
            else:
                canvas.paste(from_frame, (x_off, 0))
                canvas.paste(to_frame, (-w + x_off, 0))
            _draw_pills(ImageDraw.Draw(canvas), w, n, anim_to)
            return canvas

        img = _render_ws_frame(w, h, ws, idx, n)
        _draw_pills(ImageDraw.Draw(img), w, n, idx)
        return img


# ---------------------------------------------------------------------------
# Scroll (overlay dial:0 rotate)
# ---------------------------------------------------------------------------

class _WorkspaceScroll(Behavior):
    type_id = "_ws_scroll"
    targets = {TargetKind.DIAL_ROTATE}

    def on_rotate(self, delta: int) -> None:
        ws = _get_workspaces()
        n = len(ws)
        if n == 0:
            return
        old_idx = _get_selected()
        new_idx = max(0, min(old_idx + delta, n - 1))
        if new_idx == old_idx:
            return
        direction = -1 if delta > 0 else 1
        _set_selected(new_idx)
        _start_ws_anim(old_idx, new_idx, direction)
        _touch_interaction()
        self.bus.publish("tick:boost", {
            "hz": 60.0,
            "until": time.monotonic() + SWIPE_ANIM_DURATION + 0.05,
        })


# ---------------------------------------------------------------------------
# Launch (overlay dial:0 press)
# ---------------------------------------------------------------------------

class _WorkspaceLaunch(Behavior):
    type_id = "_ws_launch"
    targets = {TargetKind.DIAL_PRESS}

    def on_press(self) -> None:
        ws = _get_workspaces()
        n = len(ws)
        if n == 0:
            return
        idx = max(0, min(_get_selected(), n - 1))
        folder = ws[idx]

        _launch_claude_code(folder)
        print(f"[workspace_launcher] launched: {folder}", flush=True)

        _close_picker(self.bus)
