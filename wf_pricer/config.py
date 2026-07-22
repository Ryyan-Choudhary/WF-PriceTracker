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
GRID_CALIBRATION_FILE = CACHE_DIR / "grid_calibration.json"
OCR_ENGINE_FILE = CACHE_DIR / "ocr_engine.json"
HOTKEYS_FILE = CACHE_DIR / "hotkeys.json"
ANTHROPIC_API_KEY_FILE = CACHE_DIR / "anthropic_api_key.json"
GOOGLE_API_KEY_FILE = CACHE_DIR / "google_api_key.json"
LOG_FILE = LOGS_DIR / "app.log"
SCAN_LOG_FILE = LOGS_DIR / "scans.txt"

# --- Hotkeys (pynput <...> syntax) --------------------------------------
# Chosen because Warframe doesn't use F9/F10 for anything by default.
# These are the DEFAULTS; the user can rebind them in the Settings tab, which
# persists overrides to hotkeys.json (loaded below). Stored in pynput's
# GlobalHotKeys syntax so they can be handed straight to the listener.
HOTKEY_SCAN = "<f9>"           # scan a box centered on the cursor (only while scan mode is on)
HOTKEY_TOGGLE_SCAN = "<f10>"   # turn scan mode on / off
HOTKEY_QUIT = "<ctrl>+<f10>"   # quit the app entirely


def load_hotkeys() -> None:
    """Loads any rebindings from hotkeys.json over the defaults above."""
    global HOTKEY_SCAN, HOTKEY_TOGGLE_SCAN, HOTKEY_QUIT
    if not HOTKEYS_FILE.exists():
        return
    try:
        data = json.loads(HOTKEYS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    HOTKEY_SCAN = data.get("scan") or HOTKEY_SCAN
    HOTKEY_TOGGLE_SCAN = data.get("toggle") or HOTKEY_TOGGLE_SCAN
    HOTKEY_QUIT = data.get("quit") or HOTKEY_QUIT


def save_hotkeys(scan: str, toggle: str, quit_: str) -> None:
    global HOTKEY_SCAN, HOTKEY_TOGGLE_SCAN, HOTKEY_QUIT
    HOTKEY_SCAN, HOTKEY_TOGGLE_SCAN, HOTKEY_QUIT = scan, toggle, quit_
    HOTKEYS_FILE.write_text(
        json.dumps({"scan": scan, "toggle": toggle, "quit": quit_}), encoding="utf-8"
    )


load_hotkeys()

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

# --- Inventory grid calibration (for Grid Scan mode) -----------------------
# Describes a fixed R x C grid of item slots by the position/size of each
# slot's NAME BAND (the small text label), derived from boxing the first
# (top-left) and last (bottom-right) slot's name + entering rows/cols.
# None until calibrated. GRID is a dict with keys:
#   first_x, first_y   top-left of the first slot's name band (screen coords)
#   band_w, band_h     name band size
#   col_pitch, row_pitch  spacing between adjacent columns / rows
#   rows, cols         grid dimensions
GRID: dict | None = None
_GRID_KEYS = ("first_x", "first_y", "band_w", "band_h", "col_pitch", "row_pitch", "rows", "cols")


def load_grid_calibration() -> None:
    global GRID
    if not GRID_CALIBRATION_FILE.exists():
        return
    try:
        data = json.loads(GRID_CALIBRATION_FILE.read_text(encoding="utf-8"))
        GRID = {k: (int(data[k]) if k not in ("col_pitch", "row_pitch") else float(data[k])) for k in _GRID_KEYS}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        GRID = None


def save_grid_calibration(grid: dict) -> None:
    global GRID
    GRID = dict(grid)
    GRID_CALIBRATION_FILE.write_text(json.dumps(GRID), encoding="utf-8")


def clear_grid_calibration() -> None:
    global GRID
    GRID = None
    GRID_CALIBRATION_FILE.unlink(missing_ok=True)


load_grid_calibration()

# How many rapid frames to capture and vote across per grid scan, and the
# delay between them. Voting across frames beats OCR errors caused by
# Warframe's animated item-card backgrounds (the same slot renders slightly
# differently frame to frame).
GRID_SCAN_FRAMES = 3
GRID_SCAN_FRAME_DELAY_S = 0.12

# When a slot can't be identified on the first pass, re-read just that slot
# using alternative preprocessing (see ocr.NAME_BAND_PROFILES): no
# binarization, no upscaling, stricter/looser contrast, etc. Costs nothing
# extra to capture (the frames we already grabbed get re-processed), and
# rescues slots where one particular threshold mangled the text. Slots still
# unidentified after every attempt get flagged UNREADABLE rather than
# silently left blank, so you can tell "couldn't read it" from "empty slot".
GRID_SCAN_MAX_RETRY_PROFILES = 3  # extra profiles beyond the default first pass

# --- Selection mode ------------------------------------------------------
# "single" - hover an item, press the scan hotkey; grabs a fixed-size box
#            (BOX_WIDTH_PX x BOX_HEIGHT_PX) centered on the cursor.
# "multi"  - drag a box around any number of items; on release, that whole
#            region is captured and scanned for every item inside it,
#            labeling each one in place with its name and price.
# "grid"   - WFInfo-style. Calibrate a fixed R x C slot grid once, then the
#            scan hotkey OCRs every slot's name band (with multi-frame
#            voting) and labels each slot with its price.
SELECTION_MODE = "single"  # "single" | "multi" | "grid"
_VALID_SELECTION_MODES = ("single", "multi", "grid")
SELECTION_MODE_FILE = CACHE_DIR / "selection_mode.json"


def load_selection_mode() -> None:
    global SELECTION_MODE
    if not SELECTION_MODE_FILE.exists():
        return
    try:
        data = json.loads(SELECTION_MODE_FILE.read_text(encoding="utf-8"))
        mode = data.get("mode")
        if mode in _VALID_SELECTION_MODES:
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
REQUEST_DELAY_SECONDS = 0.3  # be polite: each price-fetch worker waits this long per request

# TTLs
ITEMS_CACHE_TTL_SECONDS = 3 * 24 * 3600   # item catalog barely changes; 3 days
PRICE_CACHE_TTL_SECONDS = 10 * 60         # prices move; 10 minutes
# Quiet period before the price cache is written to disk. A grid scan resolves
# dozens of prices in a burst, and rewriting the whole JSON file per price is
# pure overhead; the write is deferred until the burst stops (and forced on
# exit, see market._flush_on_shutdown).
PRICE_CACHE_WRITE_DELAY_S = 0.3

# How many item prices to fetch from warframe.market at once. 1 = fully
# sequential (the original, safest behavior - roughly matches their ~3 req/s
# etiquette given REQUEST_DELAY_SECONDS). Higher overlaps network latency for
# faster scans, but issues requests faster too: warframe.market may return
# HTTP 429 / temporarily rate-limit your IP if you push it too high. Exposed
# as a slider in the window; persisted here.
PRICE_FETCH_WORKERS = 3
PRICE_FETCH_WORKERS_MIN = 1
PRICE_FETCH_WORKERS_MAX = 8
PRICE_FETCH_FILE = CACHE_DIR / "price_fetch.json"


def load_price_fetch_workers() -> None:
    global PRICE_FETCH_WORKERS
    if not PRICE_FETCH_FILE.exists():
        return
    try:
        data = json.loads(PRICE_FETCH_FILE.read_text(encoding="utf-8"))
        PRICE_FETCH_WORKERS = _clamp_workers(int(data["workers"]))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass


def save_price_fetch_workers(workers: int) -> None:
    global PRICE_FETCH_WORKERS
    PRICE_FETCH_WORKERS = _clamp_workers(workers)
    PRICE_FETCH_FILE.write_text(json.dumps({"workers": PRICE_FETCH_WORKERS}), encoding="utf-8")


def _clamp_workers(n: int) -> int:
    return max(PRICE_FETCH_WORKERS_MIN, min(PRICE_FETCH_WORKERS_MAX, n))


load_price_fetch_workers()

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
# Grid Scan mode montages each slot's name band into one stacked image and
# reads it as a uniform block (one line per band). psm 6 fits the montage
# (each band is one line, stacked in a single column).
#
# A restricted character whitelist (a WFInfo trick to cut false positives on
# decorative backgrounds) is deliberately NOT applied here: passing
# `tessedit_char_whitelist` inline through pytesseract breaks, because
# pytesseract splits the config string with shlex and Warframe names need a
# space in the whitelist (multi-word names) plus an apostrophe/ampersand,
# which shlex mis-parses ("No closing quotation"). The downstream fuzzy match
# against the fixed catalog + multi-frame voting already clean up the OCR
# junk the whitelist would have suppressed, so the accuracy cost is minimal.
TESSERACT_GRID_CONFIG = "--oem 3 --psm 6"

# --- Name-band preprocessing (Grid Scan) --------------------------------
# Grid Scan applies a stronger contrast + binarization step than the other
# modes to isolate the bright name text from the animated card art. Otsu's
# method picks a threshold automatically; GRID_BINARIZE_CUTOFF is a floor -
# if Otsu picks something lower, this value is used instead, so faint
# background gradients don't drag the threshold down into the artwork. Raise
# it if backgrounds still bleed through, lower it if thin strokes vanish.
GRID_BINARIZE_CUTOFF = 140  # 0-255

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
FUZZY_MATCH_SCORE_CUTOFF = 86  # 0-100, higher = stricter (fewer false positives)
# If the best match doesn't beat the second-best by at least this many
# points, treat it as ambiguous and refuse to match at all, rather than
# arbitrarily picking one. This matters a lot for Prime parts: OCR text
# like "Titania Prime" (missing the part-specific last word - Blueprint /
# Chassis Blueprint / Systems Blueprint / Neuroptics Blueprint - because a
# scan box cut it off) scores an exact tie against ALL of that frame's
# parts, and picking one anyway means silently reporting the wrong item.
FUZZY_MATCH_MIN_MARGIN = 3
# How many top-scoring candidates to pull back for the ambiguity check. Needs
# to be more than 2 so a genuine tie between several parts of the same frame
# is seen as the whole group, not just its first two members - the word runoff
# below can only separate candidates it was actually shown.
FUZZY_MATCH_CANDIDATES = 5

# --- Word runoff (tie-break) -------------------------------------------
# When the overall scores above can't separate the top candidates, compare
# them one word at a time instead of giving up: first word against first word,
# and only if THAT draws, second, then third. A frame's parts are identical
# until the part-specific word ("Volt Prime NEUROPTICS Blueprint" vs "Volt
# Prime SYSTEMS Blueprint"), so the whole-string scores tie while the third
# word separates them cleanly.
# The runoff deliberately stops at the query's own last word, which is what
# keeps a genuinely incomplete read refused: "Titania Prime" draws on both of
# its words against every Titania part and simply runs out of evidence.
FUZZY_TIEBREAK_MAX_WORDS = 3   # positions examined - "first, second, or third"
FUZZY_TIEBREAK_MIN_MARGIN = 8  # per-word points needed to call a position decided
# A candidate has to account for most of the text to win a runoff. Without
# this, OCR that smears two adjacent tiles into one line ("Paris Prime Upper
# Limb Perigale Prime Receiver") would let whichever item owns the first words
# win outright, silently reporting one item and dropping the other.
FUZZY_TIEBREAK_MAX_EXTRA_WORDS = 2

# Matching runs in two stages (see items_db.ItemsIndex.match):
#   1. ANCHOR - find which item "families" the text could belong to, by
#      fuzzy-matching each word of the OCR text against the set of base
#      names (the first word of every item, e.g. "atlas", "bronco").
#   2. RANK   - score the full text only against that family's items.
# Anchoring on the distinctive base name is what stops a garbled middle
# ("Atlas Pfime'thassis Blueprint") from being dragged off to an unrelated
# item that happens to share generic words like "Prime Blueprint".
FUZZY_ANCHOR_SCORE_CUTOFF = 65  # how close a word must be to a base name to pull in that family
FUZZY_ANCHOR_MIN_TOKEN_LEN = 3  # ignore tiny OCR specks ("O2", "a", "4") when anchoring
FUZZY_ANCHOR_MAX_FAMILIES = 10   # families pulled in per word of the text

# --- Pricing -----------------------------------------------------------
# How many of the cheapest current sell orders to average together.
PRICE_SAMPLE_SIZE = 5
# Prefer orders from sellers who are online/in-game; fall back to everyone
# if nobody suitable is currently online.
PREFERRED_USER_STATUSES = ("ingame", "online")
