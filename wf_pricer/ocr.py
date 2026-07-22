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

    try:
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
    except Exception as exc:
        # A rate limit or dropped connection on one band shouldn't abort the
        # whole scan - report no text and let the other bands through.
        log.warning("Claude Vision failed (%s), treating as no text", exc)
        return []
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
    try:
        response = client.models.generate_content(
            model=config.GEMINI_VISION_MODEL,
            contents=[image.convert("RGB"), _VISION_PROMPT],
        )
    except Exception as exc:
        log.warning("Gemini Vision failed (%s), treating as no text", exc)
        return []
    return _parse_vision_text(response.text or "", image)


_ENGINES = {
    "easyocr": _extract_lines_easyocr,
    "tesseract": _extract_lines_tesseract,
    "claude_vision": _extract_lines_claude_vision,
    "gemini_vision": _extract_lines_gemini_vision,
}


# --- Grid Scan name-band reading -------------------------------------------
# A stronger preprocessing + one-OCR-call-per-frame path aimed at Warframe's
# decorative item-card backgrounds, used only by Grid Scan mode.

_BAND_MARGIN = 8  # blank rows between stacked bands in the montage


# Preprocessing variants tried, in order, when reading a slot's name band.
# Profile 0 is the normal path; the rest are retries for slots the first pass
# couldn't resolve. They deliberately pull in different directions - no
# binarization (keeps anti-aliased strokes that thresholding can eat), no
# upscaling (helps when interpolation smears an already-crisp label), and
# stricter/looser thresholds (for backgrounds that bleed through, or thin
# glyphs that vanish) - so a band that one variant mangles, another often
# reads cleanly.
# Ordered by how often they rescue a slot, since the retry loop stops at the
# first profile that resolves it.
NAME_BAND_PROFILES = [
    {"label": "default",       "upscale": None, "binarize": True,  "cutoff_delta": 0},
    {"label": "dim-text",      "upscale": None, "binarize": True,  "cutoff_delta": -55},
    {"label": "no-binarize",   "upscale": None, "binarize": False, "cutoff_delta": 0},
    {"label": "very-dim-text", "upscale": None, "binarize": True,  "cutoff_delta": -90},
    {"label": "high-contrast", "upscale": None, "binarize": True,  "cutoff_delta": 40},
    {"label": "no-upscale",    "upscale": 1.0,  "binarize": True,  "cutoff_delta": -55},
    {"label": "big-upscale",   "upscale": 3.0,  "binarize": False, "cutoff_delta": 0},
]


def _preprocess_name_band(image: Image.Image, profile: int = 0) -> Image.Image:
    """Contrast-boost (and usually binarize) a slot's name band to isolate the
    bright name text from the animated background art. Goes further than
    _preprocess_for_tesseract (which the Single/Multi paths still use).

    `profile` indexes NAME_BAND_PROFILES - retries use a different variant so
    a band that thresholds badly under one setting can still be read.
    """
    prof = NAME_BAND_PROFILES[profile % len(NAME_BAND_PROFILES)]

    gray = image.convert("L")
    factor = prof["upscale"] if prof["upscale"] is not None else config.TESSERACT_UPSCALE_FACTOR
    if factor != 1.0:
        w, h = gray.size
        gray = gray.resize((max(1, int(w * factor)), max(1, int(h * factor))), Image.LANCZOS)
    gray = ImageOps.autocontrast(gray, cutoff=2)

    if not prof["binarize"]:
        # Keep the greyscale ramp; just make sure it's dark-text-on-light.
        if ImageStat.Stat(gray).mean[0] < 128:
            gray = ImageOps.invert(gray)
        return gray

    arr = np.asarray(gray, dtype=np.uint8)
    # Otsu picks the split automatically; GRID_BINARIZE_CUTOFF is a floor so a
    # flat, textless band doesn't get a meaninglessly low threshold. The
    # profile's delta is applied AFTER that floor, so a "low-contrast" retry
    # can genuinely go below Otsu's pick - which is what rescues dim text that
    # a bright badge in the same band would otherwise push out of range.
    threshold = max(_otsu_threshold(arr), config.GRID_BINARIZE_CUTOFF) + prof["cutoff_delta"]
    threshold = max(1, min(255, threshold))
    # The name text is the BRIGHT part; keep pixels above the threshold as
    # text. Produce dark text on a light background for Tesseract.
    binary = np.where(arr >= threshold, 0, 255).astype(np.uint8)
    return Image.fromarray(binary, mode="L")


def _otsu_threshold(arr: np.ndarray) -> int:
    """Otsu's method: the grayscale cutoff that best separates the histogram
    into two classes (here, dark background vs. bright text)."""
    hist = np.bincount(arr.ravel(), minlength=256).astype(np.float64)
    total = arr.size
    if total == 0:
        return 128
    sum_total = np.dot(np.arange(256), hist)
    sum_b = 0.0
    weight_b = 0.0
    best_var = -1.0
    best_t = 128
    for t in range(256):
        weight_b += hist[t]
        if weight_b == 0:
            continue
        weight_f = total - weight_b
        if weight_f == 0:
            break
        sum_b += t * hist[t]
        mean_b = sum_b / weight_b
        mean_f = (sum_total - sum_b) / weight_f
        between = weight_b * weight_f * (mean_b - mean_f) ** 2
        if between > best_var:
            best_var = between
            best_t = t
    return best_t


def read_name_bands(bands: list[Image.Image], profile: int = 0) -> list[str]:
    """Reads a list of pre-cropped slot name bands, returning one text string
    per band (in input order, "" for a band with no legible text). Preprocesses
    each band with _preprocess_name_band using the given `profile` (see
    NAME_BAND_PROFILES - retries pass a different one).

    For Tesseract this stacks the bands into ONE tall montage and OCRs it in a
    single call (pytesseract spawns tesseract.exe per call, so per-band OCR of
    a whole grid would be dozens of spawns), then maps each detected word back
    to its band by vertical position. EasyOCR reads the montage in one call
    too. The cloud vision engines have no batch path, so they fall back to one
    call per band - slow and money per band, flagged as a known gap for grid
    mode, same as multi-select.
    """
    if not bands:
        return []
    processed = [_preprocess_name_band(b, profile) for b in bands]

    engine = config.OCR_ENGINE
    if engine == "tesseract":
        return _read_name_bands_tesseract(processed)
    if engine == "easyocr":
        return _read_name_bands_easyocr(processed)
    # claude_vision / gemini_vision: no batch API - one call per band.
    reader = _ENGINES.get(engine, _extract_lines_easyocr)
    results: list[str] = []
    for band in processed:
        lines = reader(band.convert("RGB"))
        results.append(" ".join(l.text for l in lines).strip())
    return results


def _montage(bands: list[Image.Image]) -> tuple[Image.Image, list[tuple[int, int]]]:
    """Stacks bands vertically on a white background with blank separators.
    Returns (montage, band_y_spans) where band_y_spans[i] = (top, bottom) of
    band i within the montage, used to map OCR results back to bands."""
    width = max(b.width for b in bands)
    height = sum(b.height for b in bands) + _BAND_MARGIN * (len(bands) + 1)
    montage = Image.new("L", (width, height), 255)
    spans: list[tuple[int, int]] = []
    y = _BAND_MARGIN
    for band in bands:
        montage.paste(band, (0, y))
        spans.append((y, y + band.height))
        y += band.height + _BAND_MARGIN
    return montage, spans


def _read_name_bands_tesseract(bands: list[Image.Image]) -> list[str]:
    import pytesseract
    from pytesseract import Output

    _configure_tesseract()
    montage, spans = _montage(bands)
    data = pytesseract.image_to_data(montage, config=config.TESSERACT_GRID_CONFIG, output_type=Output.DICT)

    # (x, y, h, text) per band so we can reconstruct correct reading order.
    words_per_band: list[list[tuple[float, float, float, str]]] = [[] for _ in bands]
    n = len(data["text"])
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < config.TESSERACT_MIN_CONFIDENCE:
            continue
        top, height = data["top"][i], data["height"][i]
        band_idx = _band_for_y(top + height / 2, spans)
        if band_idx is not None:
            words_per_band[band_idx].append((data["left"][i], top, height, text))

    return [_words_to_text(words) for words in words_per_band]


def _read_name_bands_easyocr(bands: list[Image.Image]) -> list[str]:
    reader = _get_easyocr_reader()
    montage, spans = _montage(bands)
    array = np.array(montage.convert("RGB"))
    detections = reader.readtext(array)

    words_per_band: list[list[tuple[float, float, float, str]]] = [[] for _ in bands]
    for points, text, conf in detections:
        text = text.strip()
        if not text or conf < config.OCR_MIN_CONFIDENCE:
            continue
        ys = [p[1] for p in points]
        xs = [p[0] for p in points]
        top, bottom = min(ys), max(ys)
        band_idx = _band_for_y((top + bottom) / 2, spans)
        if band_idx is not None:
            words_per_band[band_idx].append((min(xs), top, bottom - top, text))

    return [_words_to_text(words) for words in words_per_band]


def _words_to_text(words: list[tuple[float, float, float, str]]) -> str:
    """Reconstruct a band's text in true reading order from its detected
    words (each (x, y, height, text)).

    Warframe wraps a long item name onto two lines within a tile AND centers
    each line, so a flat left-to-right sort scrambles the words (e.g.
    "Caliban Prime" over a centered "Blueprint" would sort to "Caliban
    Blueprint Prime"). Instead: group words into lines by vertical position,
    order lines top-to-bottom, and order words within each line
    left-to-right. This is what lets a wrapped name like "Caliban Prime
    Blueprint" reassemble correctly and match cleanly instead of tying with
    its "Caliban Prime Chassis Blueprint" sibling.
    """
    if not words:
        return ""
    words = sorted(words, key=lambda w: (w[1], w[0]))  # by y, then x
    heights = sorted(w[2] for w in words)
    line_h = heights[len(heights) // 2] or 1  # median word height

    lines: list[list[tuple[float, float, float, str]]] = [[words[0]]]
    line_top = words[0][1]
    for w in words[1:]:
        if w[1] - line_top > 0.6 * line_h:  # dropped to the next line
            lines.append([w])
            line_top = w[1]
        else:
            lines[-1].append(w)
    parts = []
    for line in lines:
        line.sort(key=lambda w: w[0])
        parts.append(" ".join(w[3] for w in line))
    return " ".join(parts).strip()


def _band_for_y(y: float, spans: list[tuple[int, int]]) -> int | None:
    for idx, (top, bottom) in enumerate(spans):
        # Allow the montage margin as slack so text sitting right at a band
        # edge still maps to the right band.
        if top - _BAND_MARGIN <= y <= bottom + _BAND_MARGIN:
            return idx
    return None
