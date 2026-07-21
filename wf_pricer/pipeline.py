"""Turns a screen crop into priced item(s).

price_crop() is for single-item scans: OCR the crop, fuzzy-match the best
line (or the whole crop's lines joined together, if the name wrapped) and
price it.

price_region() is for multi-select scans: OCR a larger region that may
contain several different items' tiles, and price every one found.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image

from . import market, ocr
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

    Calls on_match(match) as each item is found and priced, so a caller can
    update a live on-screen overlay incrementally rather than waiting for
    the whole (possibly slow - one live price lookup per item) region to
    finish.
    """
    lines = ocr.extract_lines(image, sparse=True)
    matches: list[RegionMatch] = []
    skip_indices: set[int] = set()

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

        price = market.get_price(matched_item.slug)
        if not price.has_data:
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

        match = RegionMatch(name=matched_item.name, slug=matched_item.slug, price=price, bbox=bbox)
        matches.append(match)
        if on_match:
            on_match(match)

    return matches
