"""Renders emoji to small RGBA images for the Tracking HUD.

The HUD's icons are plain Unicode emoji (royalty-free standard glyphs), drawn
from the system emoji font in full colour via PIL's embedded_color path and
handed to the overlay as images - which renders identically everywhere and
sidesteps Tk's patchy native colour-emoji support. icon_image(emoji, size) is
cached per (emoji, size).
"""
from __future__ import annotations

import logging

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# Windows ships Segoe UI Emoji (colour). The others are graceful fallbacks.
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\seguiemj.ttf",
    "seguiemj.ttf",
    "NotoColorEmoji.ttf",
]

_cache: dict[tuple[str, int], Image.Image] = {}
_font_cache: dict[int, ImageFont.FreeTypeFont | None] = {}


def _get_font(px: int) -> ImageFont.FreeTypeFont | None:
    if px in _font_cache:
        return _font_cache[px]
    font: ImageFont.FreeTypeFont | None = None
    for path in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, px)
            break
        except OSError:
            continue
    if font is None:
        log.warning("No emoji font found; HUD icons will fall back to plain text")
    _font_cache[px] = font
    return font


def icon_image(emoji: str, size: int = 40) -> Image.Image:
    """A `size`x`size` RGBA image of `emoji`, centred and (where the font
    supports it) in full colour. Cached.

    Centring is content-based: the glyph is drawn large onto a roomy canvas,
    cropped to the pixels that were actually painted (getbbox), then scaled to
    fit and centred. This is robust to the odd glyph metrics some emoji have -
    notably the variation-selector ones (☀️, ❄️) that font-metric centring
    clips - because it only ever looks at the rendered pixels.
    """
    cache_key = (emoji, size)
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    render_px = max(32, size * 3)
    font = _get_font(render_px)
    if font is not None:
        big = Image.new("RGBA", (render_px * 2, render_px * 2), (0, 0, 0, 0))
        draw = ImageDraw.Draw(big)
        try:
            draw.text((render_px // 2, render_px // 2), emoji, font=font, embedded_color=True)
        except (OSError, ValueError, TypeError):
            draw.text((render_px // 2, render_px // 2), emoji, font=font, fill=(230, 233, 239, 255))
        bbox = big.getbbox()
        if bbox is not None:
            glyph = big.crop(bbox)
            target = int(size * 0.98)
            scale = min(target / glyph.width, target / glyph.height)
            nw, nh = max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale))
            glyph = glyph.resize((nw, nh), Image.LANCZOS)
            out.alpha_composite(glyph, ((size - nw) // 2, (size - nh) // 2))

    _cache[cache_key] = out
    return out
