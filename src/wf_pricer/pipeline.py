"""Turns a screen crop into priced item(s).

price_region() is for multi-select scans: OCR a larger region that may
contain several different items' tiles, and price every one found.

price_relic() is for Relic Reward mode: read the up-to-4 reward names on the
Void Fissure reward-selection screen and price them (see relic_reward_rect).

price_grid() is for Grid Scan mode: a calibrated R x C grid of slots, each
slot's name band OCR'd and voted across several captured frames.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image

from . import config, market, ocr, segment
from .items_db import ItemsIndex

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegionMatch:
    name: str
    slug: str
    price: market.PriceEstimate
    bbox: tuple[int, int, int, int]  # position within the captured region (not the screen)


def _identify_region_items(
    image: Image.Image, items_index: ItemsIndex
) -> list[tuple[object, tuple[int, int, int, int]]]:
    """OCR a multi-item region, group the lines into per-item clusters, and
    return (item, region-local bbox) for every cluster that matches the catalog.

    The grouping (segment.cluster_lines) is the fix for the old "pair the line
    directly below if the left edges align" heuristic, which broke on
    Warframe's CENTRED wrap lines: "Volt Prime" over a narrower, offset
    "Blueprint" wouldn't pair, so each half was matched alone - "Volt Prime"
    ties across every Volt part (refused, or worse, guessed) and "Blueprint"
    matches nothing, i.e. the two-prices-for-one-slot / no-match symptoms.
    Clustering merges vertically-adjacent, horizontally-overlapping lines (the
    centred-wrap case) while never joining two different columns, so each
    item's full name is assembled once and matched once.
    """
    lines = ocr.extract_lines(image, sparse=True)
    clusters = segment.cluster_lines(lines)

    identified: list[tuple[object, tuple[int, int, int, int]]] = []
    for cluster in clusters:
        item = items_index.match(cluster.text)
        if item is not None:
            identified.append((item, cluster.bbox))

    # Safety net: never emit two items for the same on-screen spot. If two
    # matches' boxes overlap, keep the one with the longer (more specific) name.
    return segment.dedup_by_bbox(
        identified, priority=lambda item, _bbox: len(getattr(item, "name", ""))
    )


def price_region(
    image: Image.Image,
    items_index: ItemsIndex,
    on_match: Optional[Callable[[RegionMatch], None]] = None,
) -> list[RegionMatch]:
    """OCRs a larger region that may contain many different items (a
    user-dragged multi-select box spanning several inventory tiles), and
    prices every one found.

    Lines are grouped into per-item clusters (see _identify_region_items) so a
    name wrapped across two centred lines is assembled into one match rather
    than split into two, and prices are fetched concurrently (see
    market.get_prices) since that's the slow part; on_match(match) fires as
    each item's price resolves, so a caller can update a live on-screen overlay
    incrementally.
    """
    return _price_and_emit(_identify_region_items(image, items_index), on_match)


def price_relic(image: Image.Image, items_index: ItemsIndex) -> list[RegionMatch]:
    """Identify + price the up-to-4 reward names in a captured Void Fissure
    reward-screen band (see relic_reward_rect for the capture geometry).

    Reuses the same cluster-then-match path as Multi-Select - the rewards sit
    in separate columns whose x-spans don't overlap, so clustering keeps each
    one distinct (the "one zone per item" separation WFInfo gets from column
    density projection). Returns the full list at once (no on_match streaming):
    relic mode picks the most valuable reward only after every price resolves.
    """
    return _price_and_emit(_identify_region_items(image, items_index), None)


def relic_reward_rect(
    screen_w: int, screen_h: int, ui_scale: float = 1.0
) -> tuple[int, int, int, int]:
    """Screen rectangle (left, top, right, bottom) of the reward-name band on
    the Void Fissure reward-selection screen, from WFInfo's reference geometry.

    WFInfo measured the band at 1920x1080 with the in-game UI at 100%
    (config.RELIC_PIXEL_REWARD_* constants) and scales it to the running
    resolution: a >=16:9 window scales by height/1080, a narrower one by
    width/1920 (WFInfo's Window.ScreenScaling), times the UI-size setting.
    The band is centred horizontally and sits pixelRewardYDisplay above the
    vertical centre.
    """
    if screen_w * 9 >= screen_h * 16:
        screen_scaling = screen_h / 1080.0
    else:
        screen_scaling = screen_w / 1920.0
    s = max(1e-6, screen_scaling * ui_scale)
    width = int(config.RELIC_PIXEL_REWARD_WIDTH * s)
    height = int(config.RELIC_PIXEL_REWARD_HEIGHT * s)
    left = screen_w // 2 - width // 2
    top = int(screen_h / 2 - config.RELIC_PIXEL_REWARD_Y_DISPLAY * s)
    return (left, top, left + width, top + height)


def _price_and_emit(
    identified: list[tuple[object, tuple[int, int, int, int]]],
    on_match: Optional[Callable[[RegionMatch], None]],
) -> list[RegionMatch]:
    """Shared tail of price_region/price_grid: batch-fetch prices for all the
    identified items concurrently, emitting a RegionMatch (skipping items with
    no live sell orders) via on_match as each item's price resolves, so the
    overlay still fills in incrementally rather than all at once.
    """
    slots_by_slug: dict[str, list[tuple[object, tuple[int, int, int, int]]]] = {}
    for item, bbox in identified:
        slots_by_slug.setdefault(item.slug, []).append((item, bbox))

    matches: list[RegionMatch] = []

    def on_priced(slug: str, price: market.PriceEstimate) -> None:
        if not price.has_data:
            return
        for item, bbox in slots_by_slug.get(slug, []):
            match = RegionMatch(name=item.name, slug=item.slug, price=price, bbox=bbox)
            matches.append(match)
            if on_match:
                on_match(match)

    # get_prices calls on_priced from THIS thread (its as_completed loop), not
    # the worker threads, so appending to `matches` here needs no extra lock.
    market.get_prices([item.slug for item, _bbox in identified], on_result=on_priced)
    return matches


def grid_slot_rects(grid: dict) -> list[tuple[int, int, int, int]]:
    """Expand a grid calibration into per-slot name-band rects in SCREEN
    coordinates, row-major (top-left to bottom-right).
    """
    rects: list[tuple[int, int, int, int]] = []
    for r in range(grid["rows"]):
        for c in range(grid["cols"]):
            x = int(round(grid["first_x"] + c * grid["col_pitch"]))
            y = int(round(grid["first_y"] + r * grid["row_pitch"]))
            rects.append((x, y, int(grid["band_w"]), int(grid["band_h"])))
    return rects


def price_grid(
    frames: list[Image.Image],
    grid: dict,
    region_origin: tuple[int, int],
    items_index: ItemsIndex,
    on_match: Optional[Callable[[RegionMatch], None]] = None,
    on_unreadable: Optional[Callable[[tuple[int, int, int, int], str], None]] = None,
) -> list[RegionMatch]:
    """Prices every slot of a calibrated grid.

    `frames` are captures of the grid's bounding region (all region-local,
    top-left at 0,0); `region_origin` is that region's screen position, so
    each slot's screen rect maps to a frame crop. Each slot's name band is
    OCR'd in every frame (see ocr.read_name_bands) and the matched item is
    VOTED across frames - Warframe's animated card backgrounds make the same
    slot read slightly differently frame to frame, so voting beats
    background-induced errors. A slot is skipped if the vote is tied between
    two different items (ambiguous - don't guess), mirroring the fuzzy
    matcher's own "refuse close calls" rule.

    Slots that stay unresolved are RETRIED with alternative preprocessing
    profiles (see ocr.NAME_BAND_PROFILES) against the same captured frames,
    since a band one threshold mangles another often reads fine. Anything
    still unresolved after every profile - but which did show some text - is
    reported via on_unreadable(bbox, best_text) so the caller can mark it,
    rather than leaving it silently blank like a genuinely empty slot.

    Returns one RegionMatch per confidently-identified slot, with a
    region-local bbox (caller adds region_origin to place it on screen, same
    as price_region).
    """
    ox, oy = region_origin
    slot_screen_rects = grid_slot_rects(grid)
    n_slots = len(slot_screen_rects)

    def crop_bands(frame: Image.Image, slots: list[int]) -> list[Image.Image]:
        fw, fh = frame.size
        bands = []
        for i in slots:
            sx, sy, w, h = slot_screen_rects[i]
            lx, ly = sx - ox, sy - oy
            box = (max(0, lx), max(0, ly), min(fw, lx + w), min(fh, ly + h))
            bands.append(frame.crop(box))
        return bands

    votes: list[list[str]] = [[] for _ in range(n_slots)]
    saw_text: list[bool] = [False] * n_slots
    last_text: list[str] = [""] * n_slots
    slug_to_item: dict[str, object] = {}

    def resolve(i: int) -> Optional[object]:
        """Winning item for slot i, or None if no votes / a tie."""
        if not votes[i]:
            return None
        counts = Counter(votes[i]).most_common()
        if len(counts) > 1 and counts[0][1] == counts[1][1]:
            return None
        return slug_to_item[counts[0][0]]

    pending = list(range(n_slots))
    max_profiles = 1 + max(0, config.GRID_SCAN_MAX_RETRY_PROFILES)
    profiles = min(max_profiles, len(ocr.NAME_BAND_PROFILES))

    # Pass 0 reads every slot with the normal preprocessing. Each later pass
    # re-reads ONLY the slots still unresolved, using a different
    # preprocessing profile (no binarize / no upscale / harder or softer
    # contrast). The captured frames are reused, so a retry costs OCR time
    # but no extra screen grab.
    for profile in range(profiles):
        if not pending:
            break
        if profile:
            log.info(
                "Grid: retrying %d unresolved slot(s) with profile %r",
                len(pending), ocr.NAME_BAND_PROFILES[profile]["label"],
            )
        frame_texts = _read_frames_texts([crop_bands(f, pending) for f in frames], profile)
        for texts in frame_texts:
            for pos, text in enumerate(texts):
                slot = pending[pos]
                if not text:
                    continue
                saw_text[slot] = True
                last_text[slot] = text
                item = items_index.match(text)
                if item is not None:
                    votes[slot].append(item.slug)
                    slug_to_item[item.slug] = item
        pending = [i for i in pending if resolve(i) is None]

    identified: list[tuple[object, tuple[int, int, int, int]]] = []
    for i in range(n_slots):
        item = resolve(i)
        sx, sy, w, h = slot_screen_rects[i]
        bbox = (sx - ox, sy - oy, w, h)
        if item is not None:
            identified.append((item, bbox))
        elif saw_text[i]:
            # Text was visible but never resolved after every profile - tell
            # the caller so it can flag the slot instead of leaving it blank
            # (a blank slot is indistinguishable from an empty inventory slot).
            log.info("Grid slot %d UNREADABLE after %d profile(s); best OCR: %r", i, profiles, last_text[i])
            if on_unreadable:
                on_unreadable(bbox, last_text[i])

    return _price_and_emit(identified, on_match)


def _read_frames_texts(per_frame_bands: list[list[Image.Image]], profile: int = 0) -> list[list[str]]:
    """OCR each frame's slot bands with the given preprocessing profile,
    returning one text-list per frame.

    Runs the frames concurrently for Tesseract (each ocr call spawns its own
    tesseract.exe subprocess, which releases the GIL - safe and a real
    speedup). EasyOCR shares one PyTorch model that is NOT safe to call from
    multiple threads at once, so it stays sequential.
    """
    if len(per_frame_bands) <= 1 or config.OCR_ENGINE != "tesseract":
        return [ocr.read_name_bands(bands, profile) for bands in per_frame_bands]

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(per_frame_bands), thread_name_prefix="wf-ocr") as pool:
        return list(pool.map(lambda bands: ocr.read_name_bands(bands, profile), per_frame_bands))
