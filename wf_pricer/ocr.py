"""Turns a screenshot into a list of text lines with bounding boxes, using
local Tesseract OCR. Warframe's UI is mostly light text on a dark background,
which Tesseract is bad at out of the box, so we preprocess (upscale,
autocontrast, invert) before handing the image over.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pytesseract
from PIL import Image, ImageOps, ImageStat
from pytesseract import Output

from . import config

log = logging.getLogger(__name__)

_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return
    if config.TESSERACT_PATH:
        pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_PATH
    _configured = True


@dataclass(frozen=True)
class OcrLine:
    text: str
    bbox: tuple[int, int, int, int]  # x, y, w, h in ORIGINAL image coordinates
    conf: float


def preprocess(image: Image.Image) -> Image.Image:
    """Grayscale + upscale + autocontrast, inverting if the image is
    predominantly dark (light-on-dark UI text reads much better to Tesseract
    as dark-on-light).
    """
    gray = image.convert("L")
    factor = config.OCR_UPSCALE_FACTOR
    if factor != 1.0:
        w, h = gray.size
        gray = gray.resize((int(w * factor), int(h * factor)), Image.LANCZOS)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    if ImageStat.Stat(gray).mean[0] < 128:
        gray = ImageOps.invert(gray)
    return gray


def extract_lines(image: Image.Image) -> list[OcrLine]:
    """Run OCR and group individual words into lines (Tesseract's own
    line grouping), filtering out low-confidence / too-short noise.
    Bounding boxes are rescaled back to the original image's coordinates.
    """
    _configure()
    pre = preprocess(image)
    data = pytesseract.image_to_data(
        pre, config=config.OCR_TESSERACT_CONFIG, output_type=Output.DICT
    )

    scale = config.OCR_UPSCALE_FACTOR
    grouped: dict[tuple[int, int, int], list[tuple[str, int, int, int, int, float]]] = {}
    n = len(data["text"])
    for i in range(n):
        text = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not text or conf < config.OCR_MIN_CONFIDENCE:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        grouped.setdefault(key, []).append(
            (text, data["left"][i], data["top"][i], data["width"][i], data["height"][i], conf)
        )

    lines: list[OcrLine] = []
    for words in grouped.values():
        words.sort(key=lambda w: w[1])
        text = " ".join(w[0] for w in words)
        if len(text) < config.OCR_MIN_TEXT_LEN:
            continue
        x0 = min(w[1] for w in words)
        y0 = min(w[2] for w in words)
        x1 = max(w[1] + w[3] for w in words)
        y1 = max(w[2] + w[4] for w in words)
        avg_conf = sum(w[5] for w in words) / len(words)
        bbox = (int(x0 / scale), int(y0 / scale), int((x1 - x0) / scale), int((y1 - y0) / scale))
        lines.append(OcrLine(text=text, bbox=bbox, conf=avg_conf))
    return lines
