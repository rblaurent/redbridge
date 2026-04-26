"""Shared PIL drawing helpers."""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

GLOW_HEIGHT_FRAC = 0.45

_glow_cache: dict[tuple, Image.Image] = {}


def glow_bg(w: int, h: int, peak: tuple[int, int, int]) -> Image.Image:
    key = (w, h, peak)
    cached = _glow_cache.get(key)
    if cached is not None:
        return cached.copy()

    img = Image.new("RGB", (w, h), (0, 0, 0))
    glow_rows = int(h * GLOW_HEIGHT_FRAC)
    draw = ImageDraw.Draw(img)
    for y in range(glow_rows):
        t = y / max(glow_rows - 1, 1)
        t2 = t * t
        r = int(peak[0] * t2)
        g = int(peak[1] * t2)
        b = int(peak[2] * t2)
        row_y = h - glow_rows + y
        draw.line([(0, row_y), (w - 1, row_y)], fill=(r, g, b))
    _glow_cache[key] = img.copy()
    return img

_FONT_CACHE: dict[int, ImageFont.ImageFont] = {}
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]


def font(size: int) -> ImageFont.ImageFont:
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
