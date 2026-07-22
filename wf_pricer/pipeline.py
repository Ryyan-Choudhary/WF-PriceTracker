"""Turns a screen crop into priced item(s).

price_crop() is for single-item scans: OCR the crop, fuzzy-match the best
line (or the whole crop's lines joined together, if the name wrapped) and
price it.

price_region() is for multi-select scans: OCR a larger region that may
contain several different items' tiles, and price every one found.

price_grid() is for Grid Scan mode: a calibrated R x C grid of slots, each
slot's name band OCR'd and voted across several captured frames.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image

from . import config, market, ocr
from .items_db import ItemsIndex

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanResult:
    name: str
    slug: str
    price: market.PriceEstimate
    raw_text: str  # the OCR line that produced this match - handy for diagnosing a wrong match


def price_crop(image: Image.Image, items_index: ItemsIndex) -> tuple[ScanResult | None, list[str]]:
    """Returns (result, raw_ocr_lines). raw_ocr_lines is every line of text
    the OCR engine actually saw in the crop, regardless of whether any of
    it matched - if a scan matches the wrong item (or nothing at all),
    seeing exactly what the engine read is what actually explains why.
    """
    lines = ocr.extract_lines(image)
    raw_texts = [line.text for line in lines]

    # Warframe's own UI wraps a long item name across 2+ lines within one
    # tile (e.g. "Titania Prime" / "Blueprint"), so OCR reports them as
    # separate lines. Matching each line alone means the matcher never sees
    # the full name - "Titania Prime" alone is genuinely ambiguous (every
    # part of that frame shares it), while the complete "Titania Prime
    # Blueprint" is not. Try the whole crop's text joined together first (in
    # top-to-bottom reading order), then fall back to each line alone.
    candidates: list[str] = []
    if len(lines) > 1:
        ordered = sorted(lines, key=lambda l: l.bbox[1])
        # Stop joining at the first big vertical gap. Even a single-item scan
        # box can clip the tile below, and gluing that tile's name on turns a
        # readable name into an unmatchable run-on. A gap wider than the line
        # itself is tall means a new tile, not a wrapped continuation.
        joined_parts = [ordered[0].text]
        for prev, line in zip(ordered, ordered[1:]):
            if line.bbox[1] - (prev.bbox[1] + prev.bbox[3]) > prev.bbox[3]:
                break
            joined_parts.append(line.text)
        if len(joined_parts) > 1:
            candidates.append(" ".join(joined_parts))
    candidates.extend(line.text for line in lines)

    # A single-line join is just that line again, and OCR can repeat a line
    # verbatim; matching is the expensive step here, so don't redo it.
    for candidate_text in dict.fromkeys(candidates):
        item = items_index.match(candidate_text)
        if item is None:
            continue
        price = market.get_price(item.slug)
        if not price.has_data:
            continue  # matched a real item name but no live sell orders to price it with
        log.info(
            "Scan matched %s (%s) -> %.1fp avg (raw OCR: %r)", item.name, item.slug, price.avg_platinum, candidate_text
        )
        return ScanResult(name=item.name, slug=item.slug, price=price, raw_text=candidate_text), raw_texts

    return None, raw_texts


@dataclass(frozen=True)
class RegionMatch:
    name: str
    slug: str
    price: market.PriceEstimate
    bbox: tuple[int, int, int, int]  # position within the captured region (not the screen)


def price_region(
    image: Image.Image,
    items_index: ItemsIndex,
    on_match: Optional[Callable[[RegionMatch], None]] = None,
) -> list[RegionMatch]:
    """OCRs a larger region that may contain many different items (a
    user-dragged multi-select box spanning several inventory tiles), and
    prices every one found.

    Unlike price_crop (which assumes every line belongs to ONE item's name
    and is safe to join wholesale), lines here can belong to entirely
    different items, so a line is only combined with another if that other
    line sits directly below it and roughly horizontally aligned (small
    vertical gap, similar left edge) - not just whichever line happens to
    come next in reading order, which for a multi-column grid is usually
    the next item along the SAME row, not the row below. This handles
    Warframe's habit of wrapping a long name across 2 lines within one tile
    without merging two different tiles' names together.

    Prices are fetched concurrently (see market.get_prices) since that's the
    slow part; on_match(match) fires as each item's price resolves, so a
    caller can update a live on-screen overlay incrementally.
    """
    lines = ocr.extract_lines(image, sparse=True)
    skip_indices: set[int] = set()

    # First pass: OCR match every item and remember its (item, bbox), WITHOUT
    # pricing yet, so all the network lookups can be batched concurrently.
    identified: list[tuple[object, tuple[int, int, int, int]]] = []
    for i, line in enumerate(lines):
        if i in skip_indices:
            continue

        lx, ly, lw, lh = line.bbox
        partner_idx = None
        best_gap = None
        for j, other in enumerate(lines):
            if j == i or j in skip_indices:
                continue
            ox, oy, _ow, _oh = other.bbox
            gap = oy - (ly + lh)
            if gap < 0 or gap > lh:
                continue  # not directly below (or too far below) this line
            if abs(ox - lx) >= lw:
                continue  # not aligned with this tile's left edge
            if best_gap is None or gap < best_gap:
                best_gap = gap
                partner_idx = j

        candidate_texts = [line.text]
        if partner_idx is not None:
            candidate_texts.insert(0, f"{line.text} {lines[partner_idx].text}")

        matched_item = None
        used_combo = False
        for text in candidate_texts:
            matched_item = items_index.match(text)
            if matched_item is not None:
                used_combo = text != line.text
                break

        if matched_item is None:
            continue

        bbox = line.bbox
        if used_combo:
            partner = lines[partner_idx]
            skip_indices.add(partner_idx)
            x0 = min(line.bbox[0], partner.bbox[0])
            y0 = min(line.bbox[1], partner.bbox[1])
            x1 = max(line.bbox[0] + line.bbox[2], partner.bbox[0] + partner.bbox[2])
            y1 = max(line.bbox[1] + line.bbox[3], partner.bbox[1] + partner.bbox[3])
            bbox = (x0, y0, x1 - x0, y1 - y0)

        identified.append((matched_item, bbox))

    return _price_and_emit(identified, on_match)


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
    multiple threads at once, and the vision engines cost money per call, so
    those stay sequential.
    """
    if len(per_frame_bands) <= 1 or config.OCR_ENGINE != "tesseract":
        return [ocr.read_name_bands(bands, profile) for bands in per_frame_bands]

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(per_frame_bands), thread_name_prefix="wf-ocr") as pool:
        return list(pool.map(lambda bands: ocr.read_name_bands(bands, profile), per_frame_bands))
