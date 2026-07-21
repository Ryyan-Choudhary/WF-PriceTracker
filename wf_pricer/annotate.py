"""Draws price labels onto a screenshot near each recognized item."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
)

_LABEL_BG = (18, 18, 20, 215)
_LABEL_BORDER = (77, 219, 234, 255)   # Warframe-platinum cyan
_LABEL_TEXT = (77, 219, 234, 255)


@dataclass(frozen=True)
class Label:
    bbox: tuple[int, int, int, int]  # x, y, w, h of the matched text in the image
    text: str


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def draw_labels(image: Image.Image, labels: list[Label]) -> Image.Image:
    out = image.convert("RGB").copy()
    overlay = Image.new("RGBA", out.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _load_font(max(14, int(out.height * 0.016)))
    img_w, img_h = out.size

    for label in labels:
        x, y, w, h = label.bbox
        tb = draw.textbbox((0, 0), label.text, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        pad = 4

        label_x = min(max(x, 0), img_w - tw - pad * 2)
        label_y = y - th - pad * 2 - 2
        if label_y < 0:
            label_y = y + h + 2  # not enough room above; drop it below instead

        rect = (label_x, label_y, label_x + tw + pad * 2, label_y + th + pad * 2)
        draw.rectangle(rect, fill=_LABEL_BG, outline=_LABEL_BORDER, width=1)
        draw.text((label_x + pad, label_y + pad), label.text, font=font, fill=_LABEL_TEXT)

    return Image.alpha_composite(out.convert("RGBA"), overlay).convert("RGB")
