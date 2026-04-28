"""Shared PIL drawing helpers."""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

STRIP_BG = (10, 10, 12)


def strip_bg(w: int, h: int) -> Image.Image:
    return Image.new("RGB", (w, h), STRIP_BG)

_FONT_CACHE: dict[tuple[str, int], ImageFont.ImageFont] = {}

_FONT_FAMILIES: dict[str, list[str]] = {
    "regular": [r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"],
    "semibold": [r"C:\Windows\Fonts\seguisb.ttf", r"C:\Windows\Fonts\segoeui.ttf"],
    "semilight": [r"C:\Windows\Fonts\segoeuisl.ttf", r"C:\Windows\Fonts\segoeui.ttf"],
}


def _load_font(weight: str, size: int) -> ImageFont.ImageFont:
    key = (weight, size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    for path in _FONT_FAMILIES.get(weight, _FONT_FAMILIES["regular"]):
        try:
            f = ImageFont.truetype(path, size)
            _FONT_CACHE[key] = f
            return f
        except OSError:
            continue
    try:
        f = ImageFont.load_default(size=size)
    except TypeError:
        f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def font(size: int) -> ImageFont.ImageFont:
    return _load_font("regular", size)


def font_semibold(size: int) -> ImageFont.ImageFont:
    return _load_font("semibold", size)


def font_semilight(size: int) -> ImageFont.ImageFont:
    return _load_font("semilight", size)


SWIPE_ANIM_DURATION = 0.25


def ease_back_out(t: float) -> float:
    t = t - 1
    return 1 + t * t * (2.7 * t + 1.7)
