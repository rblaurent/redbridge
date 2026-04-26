"""Quick preview of the Spotify strip rendering."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from PIL import Image, ImageDraw
from gfx import STRIP_BG, font, font_semibold, font_semilight

SPOTIFY_GREEN = (30, 185, 84)
BAR_BG = (30, 30, 32)

W, H = 200, 100

def _truncate(draw, text, f, max_w):
    if draw.textlength(text, font=f) <= max_w:
        return text
    for end in range(len(text), 0, -1):
        if draw.textlength(text[:end] + "…", font=f) <= max_w:
            return text[:end] + "…"
    return "…"

img = Image.new("RGB", (W, H), STRIP_BG)
draw = ImageDraw.Draw(img)

draw.rectangle((0, 0, W, 2), fill=SPOTIFY_GREEN)

tf = font_semibold(15)
draw.text((10, 22), _truncate(draw, "Bohemian Rhapsody", tf, W - 20),
          fill=(255, 255, 255), font=tf, anchor="lm")

af = font(11)
time_str = "2:34 / 5:55"
time_w = draw.textlength(time_str, font=font_semilight(10))
draw.text((10, 42), _truncate(draw, "Queen", af, W - 24 - time_w),
          fill=(130, 130, 130), font=af, anchor="lm")
draw.text((W - 10, 42), time_str,
          fill=(70, 70, 70), font=font_semilight(10), anchor="rm")

bx1, bx2, by, bh = 10, W - 10, 82, 8
bar_r = bh // 2
draw.rounded_rectangle((bx1, by, bx2, by + bh), bar_r, fill=BAR_BG)
pct = 0.43
fill_x = bx1 + int((bx2 - bx1) * pct)
draw.rounded_rectangle((bx1, by, fill_x, by + bh), bar_r, fill=SPOTIFY_GREEN)

out = os.path.join(os.path.dirname(__file__), "preview_card.png")
scale = 4
big = img.resize((W * scale, H * scale), Image.NEAREST)
big.save(out)
print(f"Saved {out} ({W*scale}x{H*scale})")
