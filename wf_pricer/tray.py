"""System tray icon: cyan diamond when idle, red while scan mode is on."""
from __future__ import annotations

from PIL import Image, ImageDraw

_IDLE_COLOR = (77, 219, 234, 255)   # Warframe-platinum cyan
_ACTIVE_COLOR = (230, 60, 60, 255)  # red = recording


def make_icon_image(active: bool) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = _ACTIVE_COLOR if active else _IDLE_COLOR
    margin = 3
    draw.polygon(
        [(size / 2, margin), (size - margin, size / 2), (size / 2, size - margin), (margin, size / 2)],
        fill=color,
        outline=(20, 20, 20, 255),
    )
    return img
