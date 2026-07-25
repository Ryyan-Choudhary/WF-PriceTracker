"""Pure geometry + colour helpers shared by the scan pipeline.

Two jobs, both adapted from how WFInfo's "Snap It" reads a screenshot - only
the parts we actually needed, kept dependency-light so they stay
unit-testable (this module imports nothing from the rest of the package):

  * cluster_lines() groups the OCR lines that belong to the SAME item name.
    Warframe wraps a long name onto 2-3 CENTRED lines within one tile
    ("Volt Prime" over a narrower, centred "Blueprint"), which OCR reports as
    separate lines. The old multi-select heuristic paired a line with the one
    "directly below AND left-edge aligned" - but a centred wrap line is offset
    from the first line's left edge, so the pairing missed and each half got
    matched on its own (the "two prices for one slot" bug, or no match at
    all). Clustering instead merges lines whose bounding boxes are vertically
    adjacent and horizontally OVERLAP, which is exactly the centred-wrap case
    and, crucially, never merges two different columns (their x-spans don't
    overlap) - the same "one zone per item" separation WFInfo gets from its
    row/column density projection, without needing a separate pass.

  * isolate_text_color() keeps only pixels close to a chosen UI text colour
    and blacks out everything else, so decorative card art and off-theme UI
    chrome stop being OCR'd as phantom text. Toggleable (see config); the
    brightness path stays the default.

  * dedup_by_bbox() is a safety net: if two matches still land on overlapping
    boxes, keep the stronger one so a slot can never show two prices.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

import numpy as np
from PIL import Image

BBox = tuple[int, int, int, int]  # x, y, w, h


@dataclass(frozen=True)
class Cluster:
    """One item's worth of text: the joined name (top-to-bottom reading
    order), the union bounding box, and the source lines that formed it."""
    text: str
    bbox: BBox
    parts: tuple


# --- line clustering -------------------------------------------------------

def _x_overlap(a: BBox, b: BBox, pad: float) -> float:
    """Signed horizontal overlap of two boxes, widened by `pad` on each side.
    Positive means they share (padded) x-extent."""
    ax0, ax1 = a[0], a[0] + a[2]
    bx0, bx1 = b[0], b[0] + b[2]
    return min(ax1, bx1) - max(ax0, bx0) + pad


def _is_wrap_continuation(upper: BBox, lower: BBox, max_gap_frac: float, overlap_pad_frac: float) -> bool:
    """True if `lower` reads as the wrapped continuation of `upper`'s line:
    it sits directly below with only a small vertical gap, and their x-spans
    overlap (a centred second line stays within the first line's horizontal
    extent). Two different columns fail the x-overlap test; two different rows
    fail the vertical-gap test - so neither is ever merged into one item.
    """
    ah = upper[3] or 1
    bh = lower[3] or 1
    avg_h = (ah + bh) / 2
    gap = lower[1] - (upper[1] + upper[3])  # vertical space between them
    # Too far below = a different row; far above/overlapping = same visual line
    # sitting side by side (handled as separate lines), not a wrap.
    if gap > max_gap_frac * avg_h or gap < -0.3 * avg_h:
        return False
    return _x_overlap(upper, lower, overlap_pad_frac * avg_h) > 0


def cluster_lines(lines: Sequence, max_gap_frac: float = 0.9, overlap_pad_frac: float = 0.2) -> list[Cluster]:
    """Group OCR lines into per-item clusters by 2-D proximity.

    `lines` is any sequence of objects exposing `.text` and `.bbox` (x, y, w,
    h) - e.g. ocr.OcrLine (duck-typed so this module needn't import ocr).
    Returns clusters ordered top-to-bottom, left-to-right.
    """
    lines = list(lines)
    n = len(lines)
    if n == 0:
        return []

    # Union-find: merge any two lines where one is the other's wrap continuation.
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        for j in range(i + 1, n):
            bi, bj = lines[i].bbox, lines[j].bbox
            upper, lower = (bi, bj) if bi[1] <= bj[1] else (bj, bi)
            if _is_wrap_continuation(upper, lower, max_gap_frac, overlap_pad_frac):
                union(i, j)

    groups: dict[int, list] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(lines[i])

    clusters: list[Cluster] = []
    for members in groups.values():
        members.sort(key=lambda l: l.bbox[1])  # top-to-bottom reading order
        text = " ".join(m.text for m in members).strip()
        x0 = min(m.bbox[0] for m in members)
        y0 = min(m.bbox[1] for m in members)
        x1 = max(m.bbox[0] + m.bbox[2] for m in members)
        y1 = max(m.bbox[1] + m.bbox[3] for m in members)
        clusters.append(Cluster(text=text, bbox=(x0, y0, x1 - x0, y1 - y0), parts=tuple(members)))

    clusters.sort(key=lambda c: (c.bbox[1], c.bbox[0]))
    return clusters


# --- overlap dedup ---------------------------------------------------------

def _iou(a: BBox, b: BBox) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix = max(0, min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    iy = max(0, min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = ix * iy
    if inter == 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def dedup_by_bbox(
    items: Iterable[tuple[object, BBox]],
    iou_thresh: float = 0.5,
    priority: Callable[[object, BBox], float] | None = None,
) -> list[tuple[object, BBox]]:
    """Drop duplicates whose boxes overlap by more than `iou_thresh`, keeping
    the higher-`priority` one (default: larger box, i.e. the more complete
    read). Preserves input order among survivors. This is a safety net: after
    clustering, overlaps should be rare, but it guarantees one item can never
    be reported twice for the same on-screen spot.
    """
    if priority is None:
        priority = lambda _payload, bbox: bbox[2] * bbox[3]
    survivors: list[list] = []  # [payload, bbox, prio]
    for payload, bbox in items:
        prio = priority(payload, bbox)
        merged = False
        for slot in survivors:
            if _iou(bbox, slot[1]) > iou_thresh:
                if prio > slot[2]:
                    slot[0], slot[1], slot[2] = payload, bbox, prio
                merged = True
                break
        if not merged:
            survivors.append([payload, bbox, prio])
    return [(s[0], s[1]) for s in survivors]


# --- colour isolation ------------------------------------------------------

def isolate_text_color(image: Image.Image, target_rgb: tuple[int, int, int], tolerance: int) -> Image.Image:
    """Keep only pixels within `tolerance` colour-distance of `target_rgb`;
    return an 8-bit "L" image with that text BLACK on a WHITE background -
    the dark-text-on-light form Tesseract reads best, matching the binarized
    output of the brightness path so the rest of the pipeline is unchanged.

    Distance is a cheap perceptually-weighted Euclidean (green weighted
    highest, blue lowest), which separates the theme's text colour from
    decorative art far better than plain brightness thresholding - the whole
    point of the WFInfo-style colour filter.
    """
    # int32 (not int16): a channel diff can be +/-255, and 255**2 * 9 overflows
    # int16 - which would wrap to garbage distances and mis-classify pixels.
    arr = np.asarray(image.convert("RGB"), dtype=np.int32)
    dr = arr[:, :, 0] - target_rgb[0]
    dg = arr[:, :, 1] - target_rgb[1]
    db = arr[:, :, 2] - target_rgb[2]
    dist = np.sqrt(2 * dr * dr + 4 * dg * dg + 3 * db * db)
    is_text = dist <= tolerance
    out = np.where(is_text, 0, 255).astype(np.uint8)  # text black, rest white
    return Image.fromarray(out, mode="L")
