"""Turns a small scanned crop into a list of text lines, using whichever
engine is configured (config.OCR_ENGINE): local EasyOCR, local Tesseract, or
a cloud AI vision model (Claude or Gemini). All four converge on the same
OcrLine shape so pipeline.py doesn't need to care which one produced it.
"""
from __future__ import annotations

import base64
import io
import logging
import threading
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps, ImageStat

from . import config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OcrLine:
    text: str
    bbox: tuple[int, int, int, int]  # x, y, w, h in image coordinates
    conf: float


def extract_lines(image: Image.Image, sparse: bool = False) -> list[OcrLine]:
    """sparse=True is for a region that may contain several different
    items' tiles (multi-select scans) rather than one tightly-cropped item
    (single-item scans) - see TESSERACT_SPARSE_CONFIG for why this
    matters. EasyOCR and the vision engines ignore it; they don't need the
    distinction.
    """
    engine = _ENGINES.get(config.OCR_ENGINE, _extract_lines_easyocr)
    return engine(image, sparse)


# --- EasyOCR -----------------------------------------------------------
_easyocr_reader = None
_easyocr_lock = threading.Lock()


def _get_easyocr_reader():
    """Lazily creates (and caches) the EasyOCR reader. Loading it imports
    torch and constructs the detection/recognition networks, and downloads
    model weights on the very first run - too slow to do at import time, so
    it only happens the first time this engine is actually used.
    """
    global _easyocr_reader
    if _easyocr_reader is None:
        with _easyocr_lock:
            if _easyocr_reader is None:
                import easyocr  # deferred: heavy import (pulls in torch)

                log.info("Loading EasyOCR model (first run may download weights)...")
                _easyocr_reader = easyocr.Reader(config.OCR_LANGUAGES, gpu=config.OCR_USE_GPU, verbose=False)
    return _easyocr_reader


def _extract_lines_easyocr(image: Image.Image, sparse: bool = False) -> list[OcrLine]:
    reader = _get_easyocr_reader()
    array = np.array(image.convert("RGB"))
    results = reader.readtext(array)

    lines: list[OcrLine] = []
    for points, text, conf in results:
        text = text.strip()
        if len(text) < config.OCR_MIN_TEXT_LEN or conf < config.OCR_MIN_CONFIDENCE:
            continue
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        bbox = (int(x0), int(y0), int(x1 - x0), int(y1 - y0))
        lines.append(OcrLine(text=text, bbox=bbox, conf=float(conf)))
    return lines


# --- Tesseract -----------------------------------------------------------
_tesseract_configured = False


def _configure_tesseract() -> None:
    global _tesseract_configured
    if _tesseract_configured:
        return
    import pytesseract

    if config.TESSERACT_PATH:
        pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_PATH
    _tesseract_configured = True


def _preprocess_for_tesseract(image: Image.Image) -> Image.Image:
    """Grayscale + upscale + autocontrast, inverting if the crop is
    predominantly dark (light-on-dark UI text reads much better to
    Tesseract as dark-on-light).
    """
    gray = image.convert("L")
    factor = config.TESSERACT_UPSCALE_FACTOR
    if factor != 1.0:
        w, h = gray.size
        gray = gray.resize((int(w * factor), int(h * factor)), Image.LANCZOS)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    if ImageStat.Stat(gray).mean[0] < 128:
        gray = ImageOps.invert(gray)
    return gray


def _extract_lines_tesseract(image: Image.Image, sparse: bool = False) -> list[OcrLine]:
    import pytesseract
    from pytesseract import Output

    _configure_tesseract()
    pre = _preprocess_for_tesseract(image)
    tess_config = config.TESSERACT_SPARSE_CONFIG if sparse else config.TESSERACT_CONFIG
    data = pytesseract.image_to_data(pre, config=tess_config, output_type=Output.DICT)

    scale = config.TESSERACT_UPSCALE_FACTOR
    grouped: dict[tuple[int, int, int], list[tuple[str, int, int, int, int, float]]] = {}
    n = len(data["text"])
    for i in range(n):
        text = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not text or conf < config.TESSERACT_MIN_CONFIDENCE:
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


_VISION_PROMPT = (
    "This is a small cropped screenshot from the game Warframe's inventory UI, "
    "showing one item's icon and/or name label. Respond with ONLY the exact "
    "item name text as it appears (for example: Wisp Prime Systems Blueprint), "
    "nothing else - no punctuation, no explanation. If you cannot identify any "
    "item name text in the image, respond with exactly: NONE"
)


def _parse_vision_text(text: str, image: Image.Image) -> list[OcrLine]:
    text = text.strip()
    if not text or text.upper() == "NONE":
        return []
    w, h = image.size
    return [OcrLine(text=text, bbox=(0, 0, w, h), conf=1.0)]


# --- AI vision: Claude (Anthropic) -----------------------------------------
_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic  # deferred: only needed for this engine

        api_key = config.get_anthropic_api_key()
        _anthropic_client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    return _anthropic_client


def _extract_lines_claude_vision(image: Image.Image, sparse: bool = False) -> list[OcrLine]:
    client = _get_anthropic_client()
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")

    response = client.messages.create(
        model=config.CLAUDE_VISION_MODEL,
        max_tokens=64,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": _VISION_PROMPT},
                ],
            }
        ],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return _parse_vision_text(text, image)


# --- AI vision: Gemini (Google AI Studio) -----------------------------------
_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai  # deferred: only needed for this engine

        api_key = config.get_google_api_key()
        _gemini_client = genai.Client(api_key=api_key) if api_key else genai.Client()
    return _gemini_client


def _extract_lines_gemini_vision(image: Image.Image, sparse: bool = False) -> list[OcrLine]:
    client = _get_gemini_client()
    # The SDK accepts a PIL Image directly in `contents` - no manual
    # base64/Part encoding needed, unlike the Anthropic SDK.
    response = client.models.generate_content(
        model=config.GEMINI_VISION_MODEL,
        contents=[image.convert("RGB"), _VISION_PROMPT],
    )
    return _parse_vision_text(response.text or "", image)


_ENGINES = {
    "easyocr": _extract_lines_easyocr,
    "tesseract": _extract_lines_tesseract,
    "claude_vision": _extract_lines_claude_vision,
    "gemini_vision": _extract_lines_gemini_vision,
}
