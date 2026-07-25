"""System tray icon: a cyan diamond (Warframe-platinum)."""
from __future__ import annotations

from PIL import Image, ImageDraw

_ICON_COLOR = (77, 219, 234, 255)   # Warframe-platinum cyan


def make_icon_image() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 3
    draw.polygon(
        [(size / 2, margin), (size - margin, size / 2), (size / 2, size - margin), (margin, size / 2)],
        fill=_ICON_COLOR,
        outline=(20, 20, 20, 255),
    )
    return img
