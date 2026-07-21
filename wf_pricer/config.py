"""Central configuration for WF-PriceTracker.

Tweak the values below to change hotkeys, folders, or matching thresholds.
Nothing here talks to the network or the filesystem at import time except
creating the data folders, so it's safe to import from anywhere.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

# --- Folders -----------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
LOGS_DIR = DATA_DIR / "logs"

for _d in (CACHE_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

ITEMS_CACHE_FILE = CACHE_DIR / "items.json"
PRICE_CACHE_FILE = CACHE_DIR / "prices.json"
BOX_CALIBRATION_FILE = CACHE_DIR / "box_calibration.json"
OCR_ENGINE_FILE = CACHE_DIR / "ocr_engine.json"
ANTHROPIC_API_KEY_FILE = CACHE_DIR / "anthropic_api_key.json"
GOOGLE_API_KEY_FILE = CACHE_DIR / "google_api_key.json"
LOG_FILE = LOGS_DIR / "app.log"
SCAN_LOG_FILE = LOGS_DIR / "scans.txt"

# --- Hotkeys (pynput <...> syntax) --------------------------------------
# Chosen because Warframe doesn't use F9/F10 for anything by default.
# Change these if they clash with something on your system.
HOTKEY_SCAN = "<f9>"           # scan a box centered on the cursor (only while scan mode is on)
HOTKEY_TOGGLE_SCAN = "<f10>"   # turn scan mode on / off
HOTKEY_QUIT = "<ctrl>+<f10>"   # quit the app entirely

# --- Item box size (set once via the "Set Item Box Size..." button) --------
# The pixel size of the box grabbed around the cursor on each scan, centered
# on wherever the mouse is when HOTKEY_SCAN is pressed. None until the user
# calibrates it (drag a box around one item's icon+name once).
BOX_WIDTH_PX: int | None = None
BOX_HEIGHT_PX: int | None = None


def load_box_calibration() -> None:
    """Loads data/cache/box_calibration.json (if present) into the
    BOX_*_PX globals above.
    """
    global BOX_WIDTH_PX, BOX_HEIGHT_PX
    if not BOX_CALIBRATION_FILE.exists():
        return
    try:
        data = json.loads(BOX_CALIBRATION_FILE.read_text(encoding="utf-8"))
        BOX_WIDTH_PX = int(data["width_px"])
        BOX_HEIGHT_PX = int(data["height_px"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass


def save_box_calibration(width_px: int, height_px: int) -> None:
    global BOX_WIDTH_PX, BOX_HEIGHT_PX
    BOX_WIDTH_PX = width_px
    BOX_HEIGHT_PX = height_px
    BOX_CALIBRATION_FILE.write_text(
        json.dumps({"width_px": width_px, "height_px": height_px}), encoding="utf-8"
    )


def clear_box_calibration() -> None:
    global BOX_WIDTH_PX, BOX_HEIGHT_PX
    BOX_WIDTH_PX = None
    BOX_HEIGHT_PX = None
    BOX_CALIBRATION_FILE.unlink(missing_ok=True)


load_box_calibration()

# --- Selection mode ------------------------------------------------------
# "single" - hover an item, press the scan hotkey; grabs a fixed-size box
#            (BOX_WIDTH_PX x BOX_HEIGHT_PX) centered on the cursor.
# "multi"  - drag a box around any number of items; on release, that whole
#            region is captured and scanned for every item inside it,
#            labeling each one in place with its name and price.
SELECTION_MODE = "single"  # "single" | "multi"
SELECTION_MODE_FILE = CACHE_DIR / "selection_mode.json"


def load_selection_mode() -> None:
    global SELECTION_MODE
    if not SELECTION_MODE_FILE.exists():
        return
    try:
        data = json.loads(SELECTION_MODE_FILE.read_text(encoding="utf-8"))
        mode = data.get("mode")
        if mode in ("single", "multi"):
            SELECTION_MODE = mode
    except (OSError, json.JSONDecodeError):
        pass


def save_selection_mode(mode: str) -> None:
    global SELECTION_MODE
    SELECTION_MODE = mode
    SELECTION_MODE_FILE.write_text(json.dumps({"mode": mode}), encoding="utf-8")


load_selection_mode()

# --- warframe.market API --------------------------------------------------
WFM_API_BASE = "https://api.warframe.market/v2"
WFM_PLATFORM = "pc"
HTTP_TIMEOUT_SECONDS = 15
REQUEST_DELAY_SECONDS = 0.3  # be polite between uncached order lookups

# TTLs
ITEMS_CACHE_TTL_SECONDS = 3 * 24 * 3600   # item catalog barely changes; 3 days
PRICE_CACHE_TTL_SECONDS = 10 * 60         # prices move; 10 minutes

# --- OCR engine choice ------------------------------------------------------
# "easyocr"       - local deep-learning OCR. Most accurate on messy/stylized
#                   game text, but the slowest (a real neural network) and
#                   has a one-time model download.
# "tesseract"     - local classical OCR. Much faster (near-instant on a
#                   small, tightly-cropped single-item box), fully offline,
#                   but was prone to misreading decorative icon art as text
#                   on full screenshots - less of a concern now that scans
#                   are just one small crop at a time.
# "claude_vision" - sends the crop to Anthropic's Claude API to read
#                   directly. Needs your own Anthropic API key, an internet
#                   connection per scan, and costs a small amount of money
#                   per scan. Currently disabled in the UI (in development -
#                   see DISABLED_ENGINES below).
# "gemini_vision" - sends the crop to Google's Gemini API (AI Studio) to
#                   read directly. Same trade-offs as claude_vision, needs
#                   your own Google AI Studio API key instead. Also
#                   currently disabled in the UI.
# Changeable at runtime via the window's "OCR Engine" dropdown; persisted to
# data/cache/ocr_engine.json so your choice survives a restart. Default is
# Tesseract - fast with no warm-up, which matters more day-to-day than
# EasyOCR's extra accuracy for most scans; switch to EasyOCR from the
# dropdown if you're getting more misreads than you'd like.
OCR_ENGINE = "tesseract"  # "easyocr" | "tesseract" | "claude_vision" | "gemini_vision"
_VALID_ENGINES = ("easyocr", "tesseract", "claude_vision", "gemini_vision")
# Not selectable from the UI yet (still being tested) - the dropdown shows
# them labeled "(in development)" and refuses to switch to them, and their
# "Set ... Key" buttons are disabled. The engines themselves still work if
# you set OCR_ENGINE here directly, or via ocr_engine.json.
DISABLED_ENGINES = ("claude_vision", "gemini_vision")


def load_ocr_engine() -> None:
    global OCR_ENGINE
    if not OCR_ENGINE_FILE.exists():
        return
    try:
        data = json.loads(OCR_ENGINE_FILE.read_text(encoding="utf-8"))
        engine = data.get("engine")
        if engine in _VALID_ENGINES:
            OCR_ENGINE = engine
    except (OSError, json.JSONDecodeError):
        pass


def save_ocr_engine(engine: str) -> None:
    global OCR_ENGINE
    OCR_ENGINE = engine
    OCR_ENGINE_FILE.write_text(json.dumps({"engine": engine}), encoding="utf-8")


load_ocr_engine()

# --- Shared OCR settings -----------------------------------------------
OCR_MIN_TEXT_LEN = 3

# --- EasyOCR -------------------------------------------------------------
OCR_LANGUAGES = ["en"]
# No NVIDIA/CUDA GPU assumed - set True only if you actually have one, it's
# much faster with GPU acceleration.
OCR_USE_GPU = False
# EasyOCR reports confidence as 0.0-1.0 (unlike Tesseract's 0-100). Genuine
# text sometimes scores as low as ~0.6-0.7, so keep this fairly permissive.
OCR_MIN_CONFIDENCE = 0.35

# --- Tesseract -----------------------------------------------------------
def _find_tesseract() -> str | None:
    exe = shutil.which("tesseract")
    if exe:
        return exe
    for candidate in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


TESSERACT_PATH = _find_tesseract()
TESSERACT_UPSCALE_FACTOR = 2.0
TESSERACT_MIN_CONFIDENCE = 40  # 0-100 scale (different from EasyOCR's 0.0-1.0)
# psm 6 = "a single uniform block of text" - a good fit for Single Item
# mode's one small, tightly-cropped box.
TESSERACT_CONFIG = "--oem 3 --psm 6"
# psm 11 = "sparse text, no assumed layout" - needed for Multi-Select mode's
# larger regions spanning several tiles. psm 6 assumes one column of text
# and merges anything at the same height into one line REGARDLESS of how
# far apart it is horizontally (verified: it silently concatenated 3
# different tiles' names into one string), which silently breaks per-item
# separation. EasyOCR doesn't need this distinction - its detector already
# treats spatially separate text as separate regions on its own.
TESSERACT_SPARSE_CONFIG = "--oem 3 --psm 11"

# --- AI vision (Claude / Gemini) --------------------------------------------
# Both providers' API keys resolve the same way: a gitignored data/cache/
# file (set via the save_*_api_key() functions, never hand-edit these files
# into being tracked), falling back to the provider's standard environment
# variable. Deliberately NOT constants in this file - config.py is tracked
# by git, so a literal key written here would end up committed.
CLAUDE_VISION_MODEL = "claude-haiku-4-5-20251001"
GEMINI_VISION_MODEL = "gemini-2.5-flash"


def get_anthropic_api_key() -> str | None:
    if ANTHROPIC_API_KEY_FILE.exists():
        try:
            data = json.loads(ANTHROPIC_API_KEY_FILE.read_text(encoding="utf-8"))
            key = data.get("anthropic_api_key")
            if key:
                return key
        except (OSError, json.JSONDecodeError):
            pass
    return os.environ.get("ANTHROPIC_API_KEY")


def save_anthropic_api_key(key: str) -> None:
    ANTHROPIC_API_KEY_FILE.write_text(json.dumps({"anthropic_api_key": key}), encoding="utf-8")


def clear_anthropic_api_key() -> None:
    ANTHROPIC_API_KEY_FILE.unlink(missing_ok=True)


def get_google_api_key() -> str | None:
    if GOOGLE_API_KEY_FILE.exists():
        try:
            data = json.loads(GOOGLE_API_KEY_FILE.read_text(encoding="utf-8"))
            key = data.get("google_api_key")
            if key:
                return key
        except (OSError, json.JSONDecodeError):
            pass
    return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")


def save_google_api_key(key: str) -> None:
    GOOGLE_API_KEY_FILE.write_text(json.dumps({"google_api_key": key}), encoding="utf-8")


def clear_google_api_key() -> None:
    GOOGLE_API_KEY_FILE.unlink(missing_ok=True)

# --- Item matching -----------------------------------------------------
FUZZY_MATCH_SCORE_CUTOFF = 84  # 0-100, higher = stricter (fewer false positives)
# If the best match doesn't beat the second-best by at least this many
# points, treat it as ambiguous and refuse to match at all, rather than
# arbitrarily picking one. This matters a lot for Prime parts: OCR text
# like "Titania Prime" (missing the part-specific last word - Blueprint /
# Chassis Blueprint / Systems Blueprint / Neuroptics Blueprint - because a
# scan box cut it off) scores an exact tie against ALL of that frame's
# parts, and picking one anyway means silently reporting the wrong item.
FUZZY_MATCH_MIN_MARGIN = 3

# --- Pricing -----------------------------------------------------------
# How many of the cheapest current sell orders to average together.
PRICE_SAMPLE_SIZE = 5
# Prefer orders from sellers who are online/in-game; fall back to everyone
# if nobody suitable is currently online.
PREFERRED_USER_STATUSES = ("ingame", "online")
