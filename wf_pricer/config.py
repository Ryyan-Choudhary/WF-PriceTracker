"""Central configuration for WF-PriceTracker.

Tweak the values below to change hotkeys, folders, or matching thresholds.
Nothing here talks to the network or the filesystem at import time except
creating the data folders, so it's safe to import from anywhere.
"""
from __future__ import annotations

import json
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
GRID_CALIBRATION_FILE = CACHE_DIR / "grid_calibration.json"
OCR_ENGINE_FILE = CACHE_DIR / "ocr_engine.json"
HOTKEYS_FILE = CACHE_DIR / "hotkeys.json"
MATCH_TOLERANCE_FILE = CACHE_DIR / "match_tolerance.json"
COLOR_FILTER_FILE = CACHE_DIR / "color_filter.json"
RELIC_FILE = CACHE_DIR / "relic.json"
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
HOTKEY_SEARCH = "<f8>"         # bring the window forward and open the manual item search


def load_hotkeys() -> None:
    """Loads any rebindings from hotkeys.json over the defaults above."""
    global HOTKEY_SCAN, HOTKEY_TOGGLE_SCAN, HOTKEY_QUIT, HOTKEY_SEARCH
    if not HOTKEYS_FILE.exists():
        return
    try:
        data = json.loads(HOTKEYS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    HOTKEY_SCAN = data.get("scan") or HOTKEY_SCAN
    HOTKEY_TOGGLE_SCAN = data.get("toggle") or HOTKEY_TOGGLE_SCAN
    HOTKEY_QUIT = data.get("quit") or HOTKEY_QUIT
    HOTKEY_SEARCH = data.get("search") or HOTKEY_SEARCH


def save_hotkeys(scan: str, toggle: str, quit_: str, search: str) -> None:
    global HOTKEY_SCAN, HOTKEY_TOGGLE_SCAN, HOTKEY_QUIT, HOTKEY_SEARCH
    HOTKEY_SCAN, HOTKEY_TOGGLE_SCAN, HOTKEY_QUIT, HOTKEY_SEARCH = scan, toggle, quit_, search
    HOTKEYS_FILE.write_text(
        json.dumps({"scan": scan, "toggle": toggle, "quit": quit_, "search": search}),
        encoding="utf-8",
    )


load_hotkeys()

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

# --- Relic reward scanner (WFInfo-style) --------------------------------
# On the Void Fissure reward-selection screen the up-to-4 reward names sit in
# a band across the top-centre. WFInfo locates that band with these reference
# constants (measured at 1920x1080, in-game UI scale 100%) and scales them to
# the actual resolution; we do the same in pipeline.relic_reward_rect. If your
# HUD is offset (unusual UI scale, ultrawide letterboxing, custom overlays),
# calibrate an explicit rectangle instead - RELIC_REGION overrides the auto rect.
RELIC_PIXEL_REWARD_WIDTH = 968
RELIC_PIXEL_REWARD_HEIGHT = 235
RELIC_PIXEL_REWARD_Y_DISPLAY = 316

# The in-game Interface "size" setting (Options > Interface). 1.0 = 100%.
# Only matters for the auto rectangle; a calibrated RELIC_REGION ignores it.
RELIC_UI_SCALE = 1.0
RELIC_UI_SCALE_MIN = 0.5
RELIC_UI_SCALE_MAX = 2.0

# Optional explicit capture rectangle (screen coords) overriding the auto one.
# {"left","top","right","bottom"} or None.
RELIC_REGION: dict | None = None


def load_relic_settings() -> None:
    global RELIC_UI_SCALE, RELIC_REGION
    if not RELIC_FILE.exists():
        return
    try:
        data = json.loads(RELIC_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    try:
        RELIC_UI_SCALE = _clamp_ui_scale(float(data["ui_scale"]))
    except (KeyError, TypeError, ValueError):
        pass
    region = data.get("region")
    if isinstance(region, dict) and all(k in region for k in ("left", "top", "right", "bottom")):
        RELIC_REGION = {k: int(region[k]) for k in ("left", "top", "right", "bottom")}


def save_relic_ui_scale(scale: float) -> None:
    global RELIC_UI_SCALE
    RELIC_UI_SCALE = _clamp_ui_scale(scale)
    _write_relic_settings()


def save_relic_region(left: int, top: int, right: int, bottom: int) -> None:
    global RELIC_REGION
    RELIC_REGION = {"left": int(left), "top": int(top), "right": int(right), "bottom": int(bottom)}
    _write_relic_settings()


def clear_relic_region() -> None:
    global RELIC_REGION
    RELIC_REGION = None
    _write_relic_settings()


def _write_relic_settings() -> None:
    payload: dict = {"ui_scale": RELIC_UI_SCALE}
    if RELIC_REGION is not None:
        payload["region"] = RELIC_REGION
    RELIC_FILE.write_text(json.dumps(payload), encoding="utf-8")


def _clamp_ui_scale(v: float) -> float:
    return max(RELIC_UI_SCALE_MIN, min(RELIC_UI_SCALE_MAX, v))


load_relic_settings()

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
# "multi"  - drag a box around any number of items; on release, that whole
#            region is captured and scanned for every item inside it,
#            labeling each one in place with its name and price.
# "grid"   - calibrate a fixed R x C slot grid once, then the scan hotkey
#            OCRs every slot's name band (with multi-frame voting) and labels
#            each slot with its price.
# "relic"  - on the Void Fissure reward-selection screen, press the scan
#            hotkey: the up-to-4 reward names across the top-centre are read,
#            priced, and the most valuable is starred. The capture rectangle is
#            derived from the reward-screen geometry scaled to your resolution
#            (see relic_reward_rect / RELIC_UI_SCALE).
SELECTION_MODE = "multi"  # "multi" | "grid" | "relic"
_VALID_SELECTION_MODES = ("multi", "grid", "relic")
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
# "easyocr"   - local deep-learning OCR. Most accurate on messy/stylized game
#               text, but the slowest (a real neural network) and has a
#               one-time model download.
# "tesseract" - local classical OCR. Much faster (near-instant on a small,
#               tightly-cropped band), fully offline.
# Changeable at runtime via the window's "OCR Engine" dropdown; persisted to
# data/cache/ocr_engine.json so your choice survives a restart. Default is
# Tesseract - fast with no warm-up, which matters more day-to-day than
# EasyOCR's extra accuracy for most scans; switch to EasyOCR from the
# dropdown if you're getting more misreads than you'd like.
OCR_ENGINE = "tesseract"  # "easyocr" | "tesseract"
_VALID_ENGINES = ("easyocr", "tesseract")


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
# Tesseract reads most reliably when glyphs are roughly this tall. Rather than
# a single fixed multiplier (which leaves small labels too small and needlessly
# blows up already-large ones), the preprocessing upscales just enough to bring
# the estimated text height up to this target - never below TESSERACT_UPSCALE_
# FACTOR, and capped so a tiny crop can't be scaled into a huge image. A WFInfo
# trick: it upsizes every reward/word zone to a minimum height before OCR.
TESSERACT_TARGET_LINE_PX = 40
TESSERACT_MAX_UPSCALE_FACTOR = 4.0
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

# --- Text colour filter (WFInfo-style, optional) ------------------------
# When enabled, preprocessing keeps only pixels close to a chosen UI text
# colour and blacks out everything else (see segment.isolate_text_color),
# instead of the default brightness threshold. This is the big lever against
# Warframe's animated card art and off-theme UI chrome bleeding into the OCR -
# but it only works if the colour is set to YOUR interface theme's text colour,
# so it ships OFF and is toggled + configured from the Settings tab.
#
# TEXT_COLOR_RGB is that text colour; the default is the near-white of the
# stock theme. TEXT_COLOR_TOLERANCE is how far a pixel may sit from it and
# still count as text, on the weighted-distance scale used by
# isolate_text_color (0-765; ~120-180 is a sensible band). Raise it if real
# text is being dropped, lower it if background art is leaking through.
TEXT_COLOR_FILTER_ENABLED = False
TEXT_COLOR_RGB = (232, 230, 214)
TEXT_COLOR_TOLERANCE = 140
TEXT_COLOR_TOLERANCE_MIN = 40
TEXT_COLOR_TOLERANCE_MAX = 400


def load_color_filter() -> None:
    global TEXT_COLOR_FILTER_ENABLED, TEXT_COLOR_RGB, TEXT_COLOR_TOLERANCE
    if not COLOR_FILTER_FILE.exists():
        return
    try:
        data = json.loads(COLOR_FILTER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    TEXT_COLOR_FILTER_ENABLED = bool(data.get("enabled", TEXT_COLOR_FILTER_ENABLED))
    rgb = data.get("rgb")
    if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
        try:
            TEXT_COLOR_RGB = tuple(max(0, min(255, int(c))) for c in rgb)
        except (TypeError, ValueError):
            pass
    try:
        TEXT_COLOR_TOLERANCE = _clamp_color_tolerance(int(data["tolerance"]))
    except (KeyError, TypeError, ValueError):
        pass


def save_color_filter(enabled: bool, rgb: tuple[int, int, int], tolerance: int) -> None:
    global TEXT_COLOR_FILTER_ENABLED, TEXT_COLOR_RGB, TEXT_COLOR_TOLERANCE
    TEXT_COLOR_FILTER_ENABLED = bool(enabled)
    TEXT_COLOR_RGB = tuple(max(0, min(255, int(c))) for c in rgb)
    TEXT_COLOR_TOLERANCE = _clamp_color_tolerance(tolerance)
    COLOR_FILTER_FILE.write_text(
        json.dumps({
            "enabled": TEXT_COLOR_FILTER_ENABLED,
            "rgb": list(TEXT_COLOR_RGB),
            "tolerance": TEXT_COLOR_TOLERANCE,
        }),
        encoding="utf-8",
    )


def _clamp_color_tolerance(v: int) -> int:
    return max(TEXT_COLOR_TOLERANCE_MIN, min(TEXT_COLOR_TOLERANCE_MAX, v))


load_color_filter()

# --- Item matching -----------------------------------------------------
# The minimum similarity (0-100) a candidate must reach to be reported at all.
# This IS the "guess vs. unmatched" line: a read scoring below it is declared
# unmatched instead of being forced onto the nearest item. Higher = stricter
# (fewer wrong guesses, more "unmatched"); lower = more tolerant of messy OCR
# (more guesses, more chance of a wrong one). User-tunable via the Settings
# tab (persisted to match_tolerance.json); the bounds keep it in a sane range
# - below ~70 the catalog starts matching noise, above ~96 real reads with a
# little OCR damage get rejected.
FUZZY_MATCH_SCORE_CUTOFF = 86
FUZZY_MATCH_SCORE_CUTOFF_MIN = 72
FUZZY_MATCH_SCORE_CUTOFF_MAX = 95


def _clamp_match_cutoff(n: int) -> int:
    return max(FUZZY_MATCH_SCORE_CUTOFF_MIN, min(FUZZY_MATCH_SCORE_CUTOFF_MAX, n))


def load_match_cutoff() -> None:
    global FUZZY_MATCH_SCORE_CUTOFF
    if not MATCH_TOLERANCE_FILE.exists():
        return
    try:
        data = json.loads(MATCH_TOLERANCE_FILE.read_text(encoding="utf-8"))
        FUZZY_MATCH_SCORE_CUTOFF = _clamp_match_cutoff(int(data["cutoff"]))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass


def save_match_cutoff(cutoff: int) -> None:
    global FUZZY_MATCH_SCORE_CUTOFF
    FUZZY_MATCH_SCORE_CUTOFF = _clamp_match_cutoff(cutoff)
    MATCH_TOLERANCE_FILE.write_text(json.dumps({"cutoff": FUZZY_MATCH_SCORE_CUTOFF}), encoding="utf-8")


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

load_match_cutoff()  # apply any persisted tolerance override over the default

# --- Pricing -----------------------------------------------------------
# How many of the cheapest current sell orders to average together.
PRICE_SAMPLE_SIZE = 5
# Prefer orders from sellers who are online/in-game; fall back to everyone
# if nobody suitable is currently online.
PREFERRED_USER_STATUSES = ("ingame", "online")
