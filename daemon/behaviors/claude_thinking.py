"""Animated Claude Code 'thinking' spinner — rotating star glyph in orange."""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from behaviors.base import Behavior, EventBus, Target, TargetKind
from gfx import font
from registry import register


FRAMES: tuple[str, ...] = (
    "\u2722",
    "\u2733",
    "\u2736",
    "\u273b",
    "\u273d",
    "\u273b",
    "\u2736",
    "\u2733",
)

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


def _parse_color(s: str | None, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if not s:
        return default
    s = str(s).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return default
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return default


@register
class ClaudeThinkingBehavior(Behavior):
    type_id = "claude_thinking"
    display_name = "Claude thinking"
    targets = {TargetKind.KEY}
    config_schema = {
        "type": "object",
        "properties": {
            "fg_color": {"type": "string", "default": "#d97757"},
            "bg_color": {"type": "string", "default": "#1a1a1a"},
            "label": {"type": "string", "default": "thinking"},
            "glyph_size": {"type": "integer", "default": 72, "minimum": 16, "maximum": 120},
        },
    }

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        self._frame = 0

    def tick(self) -> bool:
        self._frame = (self._frame + 1) % len(FRAMES)
        return True

    def render(self) -> Image.Image | None:
        w, h = self.size()
        if w == 0 or h == 0:
            return None
        fg = _parse_color(self.config.get("fg_color"), (217, 119, 87))
        bg = _parse_color(self.config.get("bg_color"), (26, 26, 26))
        label = (self.config.get("label") or "").strip()
        gsize = int(self.config.get("glyph_size") or 72)

        img = Image.new("RGB", (w, h), bg)
        draw = ImageDraw.Draw(img)

        glyph = FRAMES[self._frame]
        gf = _symbol_font(gsize)
        b = draw.textbbox((0, 0), glyph, font=gf)
        gw = b[2] - b[0]
        gh = b[3] - b[1]
        gx = (w - gw) // 2 - b[0]
        gy = (h - gh) // 2 - b[1] - (8 if label else 0)
        draw.text((gx, gy), glyph, fill=fg, font=gf)

        if label:
            lf = font(14)
            lb = draw.textbbox((0, 0), label, font=lf)
            tw = lb[2] - lb[0]
            th = lb[3] - lb[1]
            tx = (w - tw) // 2 - lb[0]
            ty = h - th - 8 - lb[1]
            draw.text((tx, ty), label, fill=fg, font=lf)

        return img
