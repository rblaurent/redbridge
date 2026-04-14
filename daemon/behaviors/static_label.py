"""Static text + optional icon + bg color. No interaction."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from behaviors.base import Behavior, TargetKind
from registry import register


def _parse_color(s: str | None, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if not s:
        return default
    s = s.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return default
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return default


# Fonts are loaded once and reused across render calls.
_FONT_CACHE: dict[int, ImageFont.ImageFont] = {}
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]


def _font(size: int) -> ImageFont.ImageFont:
    cached = _FONT_CACHE.get(size)
    if cached is not None:
        return cached
    for path in _FONT_CANDIDATES:
        try:
            f = ImageFont.truetype(path, size)
            _FONT_CACHE[size] = f
            return f
        except OSError:
            continue
    try:
        f = ImageFont.load_default(size=size)
    except TypeError:
        f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f


@register
class StaticLabelBehavior(Behavior):
    type_id = "static_label"
    display_name = "Static label"
    targets = {TargetKind.KEY, TargetKind.STRIP_REGION}
    config_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "default": ""},
            "icon_path": {"type": "string", "default": ""},
            "bg_color": {"type": "string", "default": "#000000"},
            "fg_color": {"type": "string", "default": "#ffffff"},
            "font_size": {"type": "integer", "default": 16, "minimum": 8, "maximum": 64},
        },
    }

    def render(self) -> Image.Image | None:
        w, h = self.size()
        if w == 0 or h == 0:
            return None

        bg = _parse_color(self.config.get("bg_color"), (0, 0, 0))
        fg = _parse_color(self.config.get("fg_color"), (255, 255, 255))
        img = Image.new("RGB", (w, h), bg)

        text = (self.config.get("text") or "").strip()
        icon_path = (self.config.get("icon_path") or "").strip()

        has_icon = False
        if icon_path and Path(icon_path).is_file():
            try:
                icon = Image.open(icon_path).convert("RGBA")
                reserve = 24 if text else 8
                max_w = w - 8
                max_h = h - reserve
                icon.thumbnail((max_w, max_h), Image.LANCZOS)
                ix = (w - icon.width) // 2
                iy = 4
                img.paste(icon, (ix, iy), icon)
                has_icon = True
            except Exception:
                pass

        if text:
            size = int(self.config.get("font_size") or 16)
            font = _font(size)
            draw = ImageDraw.Draw(img)
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            tx = (w - tw) // 2 - bbox[0]
            if has_icon:
                ty = h - th - 4 - bbox[1]
            else:
                ty = (h - th) // 2 - bbox[1]
            draw.text((tx, ty), text, fill=fg, font=font)

        return img
