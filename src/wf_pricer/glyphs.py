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


# Spider-leg geometry for the custom orb icon, as (root, knee, foot) offsets in
# fractions of the drawing canvas from its centre. x is mirrored for each side;
# knees peak up, feet splay out and down - a spider-leg silhouette. Three legs
# per side.
_LEG_GEO = [
    ((0.12, 0.00), (0.28, -0.13), (0.38, -0.03)),
    ((0.14, 0.05), (0.32, -0.02), (0.42, 0.12)),
    ((0.13, 0.10), (0.27, 0.09), (0.36, 0.22)),
]


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _shade(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)  # type: ignore[return-value]


def _draw_orb_big(color_hex: str, box: int = 200) -> Image.Image:
    """Draw the Fass/Vome icon: an oblong, snake-like orb (a slit 'pupil' down
    its middle) with three spider legs per side, in `color_hex`. Rendered large
    here; icon_image crops-to-content and scales it down for smooth edges."""
    img = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = box / 2
    base = _hex_to_rgb(color_hex)
    leg = _shade(base, 0.72)
    dark = _shade(base, 0.5)
    light = _shade(base, 1.4)
    bw, bh = 0.56 * box, 0.34 * box          # oblong body
    lw = max(2, int(box * 0.03))             # leg stroke width

    # Legs first, so the body sits over their roots.
    for side in (-1, 1):
        for (rx, ry), (kx, ky), (fx, fy) in _LEG_GEO:
            root = (int(cx + side * rx * box), int(cy + ry * box))
            knee = (int(cx + side * kx * box), int(cy + ky * box))
            foot = (int(cx + side * fx * box), int(cy + fy * box))
            d.line([root, knee, foot], fill=leg + (255,), width=lw, joint="curve")
            d.ellipse([foot[0] - lw * 0.7, foot[1] - lw * 0.7,
                       foot[0] + lw * 0.7, foot[1] + lw * 0.7], fill=leg + (255,))

    # Body, with a darker rim.
    d.ellipse([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2],
              fill=base + (255,), outline=dark + (255,), width=max(1, int(box * 0.012)))
    # Vertical slit 'pupil' - the snake-like touch.
    pw, ph = 0.07 * box, 0.20 * box
    d.ellipse([cx - pw / 2, cy - ph / 2, cx + pw / 2, cy + ph / 2], fill=dark + (255,))
    # Glossy sheen, upper-left, to read as an orb.
    hlw, hlh = 0.18 * box, 0.10 * box
    hcx, hcy = cx - 0.14 * box, cy - 0.09 * box
    d.ellipse([hcx - hlw / 2, hcy - hlh / 2, hcx + hlw / 2, hcy + hlh / 2], fill=light + (210,))
    return img


def icon_image(emoji: str, size: int = 40) -> Image.Image:
    """A `size`x`size` RGBA image of `emoji`, centred and (where the font
    supports it) in full colour. Cached.

    An `emoji` of the form "orb:<hexcolour>" is drawn as the custom Fass/Vome
    orb-with-legs shape instead of a font glyph (see _draw_orb_big).

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
    if emoji.startswith("orb:"):
        big = _draw_orb_big(emoji[len("orb:"):])
        _fit_centered(big, out, size)
        _cache[cache_key] = out
        return out

    render_px = max(32, size * 3)
    font = _get_font(render_px)
    if font is not None:
        big = Image.new("RGBA", (render_px * 2, render_px * 2), (0, 0, 0, 0))
        draw = ImageDraw.Draw(big)
        try:
            draw.text((render_px // 2, render_px // 2), emoji, font=font, embedded_color=True)
        except (OSError, ValueError, TypeError):
            draw.text((render_px // 2, render_px // 2), emoji, font=font, fill=(230, 233, 239, 255))
        _fit_centered(big, out, size)

    _cache[cache_key] = out
    return out


def _fit_centered(big: Image.Image, out: Image.Image, size: int) -> None:
    """Crop `big` to its painted pixels, scale to ~98% of `size`, and composite
    it centred onto `out` (in place). Shared by the emoji and orb paths."""
    bbox = big.getbbox()
    if bbox is None:
        return
    glyph = big.crop(bbox)
    target = int(size * 0.98)
    scale = min(target / glyph.width, target / glyph.height)
    nw, nh = max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale))
    glyph = glyph.resize((nw, nh), Image.LANCZOS)
    out.alpha_composite(glyph, ((size - nw) // 2, (size - nh) // 2))
