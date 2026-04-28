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
from gfx import font, font_semibold, strip_bg
from registry import register

PROJECTS_ROOT = r"T:\Projects"
_ASSETS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")

CLAUDE_ORANGE = (193, 95, 60)
DIM_GREY = (90, 90, 90)
ROW_H = 22
VISIBLE_ROWS = 4
ROW_START_Y = 6
_TIMEOUT_S: float = 5.0

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_picker_active: bool = False
_selected_index: int = 0
_workspaces: list[str] = []
_overlay_refs: dict | None = None
_last_interaction: float = 0.0


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


def _close_picker(bus: EventBus) -> None:
    global _picker_active, _overlay_refs
    with _state_lock:
        _picker_active = False
        _overlay_refs = None
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

        carousel = _WorkspaceCarousel(
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
                "carousel": carousel,
                "scroll": scroll,
                "launch": launch,
            }

        self.bus.publish("overlay:set", {
            "strip": {0: carousel},
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
# Carousel (overlay strip:0)
# ---------------------------------------------------------------------------

class _WorkspaceCarousel(Behavior):
    type_id = "_ws_carousel"
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

        img = strip_bg(w, h)
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, w, 2), fill=CLAUDE_ORANGE)

        if n == 0:
            draw.text((w // 2, h // 2), "No projects", fill=(70, 70, 70),
                       font=font(14), anchor="mm")
            return img

        scroll_top = max(0, min(idx - 1, n - VISIBLE_ROWS))
        row_font = font_semibold(13)

        for row_i in range(VISIBLE_ROWS):
            si = scroll_top + row_i
            if si >= n:
                break
            y = ROW_START_Y + row_i * ROW_H
            selected = si == idx

            if selected:
                draw.rounded_rectangle((4, y, w - 4, y + ROW_H - 2), 4, fill=CLAUDE_ORANGE)

            dot_color = (255, 255, 255) if selected else DIM_GREY
            dot_y = y + ROW_H // 2
            draw.ellipse((10, dot_y - 4, 18, dot_y + 4), fill=dot_color)

            text_color = (255, 255, 255) if selected else (170, 170, 170)
            name = _truncate(draw, ws[si], row_font, w - 34)
            draw.text((24, dot_y - 1), name, fill=text_color,
                       font=row_font, anchor="lm")

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
        idx = _get_selected() + delta
        _set_selected(max(0, min(idx, n - 1)))
        _touch_interaction()


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
