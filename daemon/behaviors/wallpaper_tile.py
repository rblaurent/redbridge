"""Show a crop of the Windows desktop wallpaper as the tile image."""

from __future__ import annotations

import ctypes
import threading
from pathlib import Path
from typing import Any

from PIL import Image

from behaviors.base import Behavior, TargetKind
from registry import register


SPI_GETDESKWALLPAPER = 0x0073
_MAX_PATH = 520

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Image.Image]] = {}


def _current_wallpaper_path() -> str | None:
    try:
        buf = ctypes.create_unicode_buffer(_MAX_PATH)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETDESKWALLPAPER, _MAX_PATH, buf, 0
        )
        if not ok:
            return None
        p = (buf.value or "").strip()
        return p or None
    except Exception:
        return None


def _load_wallpaper() -> Image.Image | None:
    path = _current_wallpaper_path()
    if not path:
        return None
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return None
    with _cache_lock:
        entry = _cache.get(path)
        if entry and entry[0] == mtime:
            return entry[1]
    try:
        img = Image.open(path).convert("RGB")
        img.load()
    except Exception:
        return None
    with _cache_lock:
        _cache.clear()
        _cache[path] = (mtime, img)
    return img


def _clamp_pct(v: Any, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(100.0, f))


@register
class WallpaperTileBehavior(Behavior):
    type_id = "wallpaper_tile"
    display_name = "Wallpaper tile"
    targets = {TargetKind.KEY, TargetKind.STRIP_REGION}
    config_schema = {
        "type": "object",
        "properties": {
            "x_pct": {"type": "number", "default": 0, "minimum": 0, "maximum": 100},
            "y_pct": {"type": "number", "default": 0, "minimum": 0, "maximum": 100},
            "w_pct": {"type": "number", "default": 100, "minimum": 1, "maximum": 100},
            "h_pct": {"type": "number", "default": 100, "minimum": 1, "maximum": 100},
        },
    }

    def render(self) -> Image.Image | None:
        w, h = self.size()
        if w == 0 or h == 0:
            return None
        wp = _load_wallpaper()
        if wp is None:
            return None

        x = _clamp_pct(self.config.get("x_pct"), 0.0)
        y = _clamp_pct(self.config.get("y_pct"), 0.0)
        cw = _clamp_pct(self.config.get("w_pct"), 100.0)
        ch = _clamp_pct(self.config.get("h_pct"), 100.0)

        iw, ih = wp.size
        left = int(iw * x / 100)
        top = int(ih * y / 100)
        right = max(left + 1, min(iw, int(iw * (x + cw) / 100)))
        bottom = max(top + 1, min(ih, int(ih * (y + ch) / 100)))

        tile = wp.crop((left, top, right, bottom))
        if tile.size != (w, h):
            tile = tile.resize((w, h), Image.LANCZOS)
        return tile
