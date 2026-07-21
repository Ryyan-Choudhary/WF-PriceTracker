"""Central configuration for WF-PriceTracker.

Tweak the values below to change hotkeys, folders, or matching thresholds.
Nothing here talks to the network or the filesystem at import time except
creating the data folders, so it's safe to import from anywhere.
"""
from __future__ import annotations

import shutil
from pathlib import Path

# --- Folders -----------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
CAPTURES_DIR = DATA_DIR / "captures"
OUTPUT_DIR = DATA_DIR / "output"
CACHE_DIR = DATA_DIR / "cache"
LOGS_DIR = DATA_DIR / "logs"

for _d in (CAPTURES_DIR, OUTPUT_DIR, CACHE_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

ITEMS_CACHE_FILE = CACHE_DIR / "items.json"
PRICE_CACHE_FILE = CACHE_DIR / "prices.json"
LOG_FILE = LOGS_DIR / "app.log"

# --- Hotkeys (pynput <...> syntax) --------------------------------------
# Chosen because Warframe doesn't use F9/F10 for anything by default.
# Change these if they clash with something on your system.
HOTKEY_CAPTURE = "<f9>"        # take one screenshot (only while capture mode is on)
HOTKEY_TOGGLE = "<f10>"        # turn capture mode on / off (turning off triggers processing)
HOTKEY_QUIT = "<ctrl>+<f10>"   # quit the app entirely

# --- Screen capture ------------------------------------------------------
# False = grab only the primary monitor. True = grab the full virtual desktop
# (all monitors stitched together). Most people run Warframe fullscreen on
# one monitor, so primary-only is the sane default.
CAPTURE_ALL_MONITORS = False

# --- warframe.market API --------------------------------------------------
WFM_API_BASE = "https://api.warframe.market/v2"
WFM_PLATFORM = "pc"
HTTP_TIMEOUT_SECONDS = 15
REQUEST_DELAY_SECONDS = 0.3  # be polite between uncached order lookups

# TTLs
ITEMS_CACHE_TTL_SECONDS = 3 * 24 * 3600   # item catalog barely changes; 3 days
PRICE_CACHE_TTL_SECONDS = 10 * 60         # prices move; 10 minutes

# --- OCR -------------------------------------------------------------------
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
OCR_UPSCALE_FACTOR = 2.0
OCR_MIN_CONFIDENCE = 40
OCR_MIN_TEXT_LEN = 3
OCR_TESSERACT_CONFIG = "--oem 3 --psm 11"

# --- Item matching -----------------------------------------------------
FUZZY_MATCH_SCORE_CUTOFF = 84  # 0-100, higher = stricter (fewer false positives)

# --- Pricing -----------------------------------------------------------
# How many of the cheapest current sell orders to average together.
PRICE_SAMPLE_SIZE = 5
# Prefer orders from sellers who are online/in-game; fall back to everyone
# if nobody suitable is currently online.
PREFERRED_USER_STATUSES = ("ingame", "online")
