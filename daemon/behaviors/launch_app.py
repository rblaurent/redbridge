"""Launch an executable, or focus its window if already running."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from PIL import Image, ImageDraw

from behaviors.base import Behavior, TargetKind
from registry import register
from win_focus import find_window_by_title, focus_window

_DAEMON_DIR = os.path.dirname(os.path.dirname(__file__))


@register
class LaunchAppBehavior(Behavior):
    type_id = "launch_app"
    display_name = "Launch app"
    targets = {TargetKind.KEY}
    config_schema: dict = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}, "default": []},
            "icon_path": {"type": "string", "default": ""},
            "window_title": {"type": "string", "default": ""},
            "window_class": {"type": "string", "default": ""},
            "label": {"type": "string", "default": ""},
        },
        "required": ["path"],
    }

    def __init__(self, target, config, bus):
        super().__init__(target, config, bus)
        self._rendered: Image.Image | None = None

    def _resolve_icon(self) -> Path | None:
        raw = (self.config.get("icon_path") or "").strip()
        if not raw:
            return None
        p = Path(raw)
        if not p.is_absolute():
            p = Path(_DAEMON_DIR) / p
        return p if p.is_file() else None

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

        icon_path = self._resolve_icon()
        if icon_path:
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
        window_title = (self.config.get("window_title") or "").strip()
        window_class = (self.config.get("window_class") or "").strip()
        if window_title:
            hwnd = find_window_by_title(window_title, window_class=window_class)
            if hwnd:
                focus_window(hwnd)
                return

        path = self.config.get("path", "")
        args = self.config.get("args") or []
        threading.Thread(
            target=self._launch, args=(path, args), daemon=True,
        ).start()

    @staticmethod
    def _launch(path: str, args: list[str]) -> None:
        try:
            subprocess.Popen([path] + args)
        except Exception as e:
            print(f"[launch_app] launch failed: {e}", flush=True)
