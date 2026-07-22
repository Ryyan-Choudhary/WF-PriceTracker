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
        candidates.append(" ".join(l.text for l in ordered))
    candidates.extend(line.text for line in lines)

    for candidate_text in candidates:
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

    Returns one RegionMatch per confidently-identified slot, with a
    region-local bbox (caller adds region_origin to place it on screen, same
    as price_region).
    """
    ox, oy = region_origin
    slot_screen_rects = grid_slot_rects(grid)
    n_slots = len(slot_screen_rects)

    def crop_bands(frame: Image.Image) -> list[Image.Image]:
        fw, fh = frame.size
        bands = []
        for (sx, sy, w, h) in slot_screen_rects:
            lx, ly = sx - ox, sy - oy
            box = (max(0, lx), max(0, ly), min(fw, lx + w), min(fh, ly + h))
            bands.append(frame.crop(box))
        return bands

    # OCR every frame's slot bands -> per-frame list of texts. The frames are
    # independent, so OCR them concurrently when the engine allows it (see
    # _read_frames_texts). This is CPU/latency-bound, not rate-limited.
    frame_texts = _read_frames_texts([crop_bands(f) for f in frames])

    votes: list[list[str]] = [[] for _ in range(n_slots)]
    slug_to_item = {}
    for texts in frame_texts:
        for i, text in enumerate(texts):
            if not text:
                continue
            item = items_index.match(text)
            if item is not None:
                votes[i].append(item.slug)
                slug_to_item[item.slug] = item

    # Resolve each slot's winning item by vote (skip ties), then batch-price.
    identified: list[tuple[object, tuple[int, int, int, int]]] = []
    for i, slugs in enumerate(votes):
        if not slugs:
            continue
        counts = Counter(slugs).most_common()
        if len(counts) > 1 and counts[0][1] == counts[1][1]:
            log.info("Grid slot %d: tied vote %s - refusing to guess", i, counts[:2])
            continue  # ambiguous - two items tied
        winner_slug = counts[0][0]
        item = slug_to_item[winner_slug]
        sx, sy, w, h = slot_screen_rects[i]
        identified.append((item, (sx - ox, sy - oy, w, h)))

    return _price_and_emit(identified, on_match)


def _read_frames_texts(per_frame_bands: list[list[Image.Image]]) -> list[list[str]]:
    """OCR each frame's slot bands, returning one text-list per frame.

    Runs the frames concurrently for Tesseract (each ocr call spawns its own
    tesseract.exe subprocess, which releases the GIL - safe and a real
    speedup). EasyOCR shares one PyTorch model that is NOT safe to call from
    multiple threads at once, and the vision engines cost money per call, so
    those stay sequential.
    """
    if len(per_frame_bands) <= 1 or config.OCR_ENGINE != "tesseract":
        return [ocr.read_name_bands(bands) for bands in per_frame_bands]

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(per_frame_bands), thread_name_prefix="wf-ocr") as pool:
        return list(pool.map(ocr.read_name_bands, per_frame_bands))
