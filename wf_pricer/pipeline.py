"""Ties OCR, item matching, pricing, and annotation together into one pass
over a folder of screenshots taken during a single capture session.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from . import annotate, market, ocr
from .items_db import ItemsIndex

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".webp")


@dataclass(frozen=True)
class MatchedItem:
    name: str
    slug: str
    price: market.PriceEstimate
    bbox: tuple[int, int, int, int]
    source_image: str


@dataclass(frozen=True)
class SessionResult:
    processed_images: int
    matches: list[MatchedItem]
    output_dir: Path


def process_image(path: Path, items_index: ItemsIndex, output_dir: Path) -> list[MatchedItem]:
    image = Image.open(path)
    lines = ocr.extract_lines(image)

    matched: list[MatchedItem] = []
    labels: list[annotate.Label] = []
    for line in lines:
        item = items_index.match(line.text)
        if item is None:
            continue
        price = market.get_price(item.slug)
        if not price.has_data:
            continue  # matched a real item name but no live sell orders to price it with
        matched.append(
            MatchedItem(name=item.name, slug=item.slug, price=price, bbox=line.bbox, source_image=path.name)
        )
        approx = "~" if price.used_fallback else ""
        labels.append(annotate.Label(bbox=line.bbox, text=f"{item.name}: {approx}{price.avg_platinum:g}p"))

    annotated = annotate.draw_labels(image, labels)
    out_path = output_dir / f"{path.stem}_priced.png"
    annotated.save(out_path)
    log.info("Processed %s: %d item(s) matched -> %s", path.name, len(matched), out_path.name)
    return matched


def process_session(
    session_dir: Path,
    output_dir: Path,
    items_index: ItemsIndex,
    on_progress: Optional[Callable[[str], None]] = None,
) -> SessionResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    image_paths = sorted(
        p for p in session_dir.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES
    )
    report = on_progress or (lambda _msg: None)

    all_matches: list[MatchedItem] = []
    for i, path in enumerate(image_paths, start=1):
        try:
            matches = process_image(path, items_index, output_dir)
            all_matches.extend(matches)
            report(f"[{i}/{len(image_paths)}] {path.name}: {len(matches)} item(s) matched")
        except Exception:
            log.exception("Failed to process %s", path)
            report(f"[{i}/{len(image_paths)}] {path.name}: FAILED (see data/logs/app.log)")

    write_summary(all_matches, output_dir)
    return SessionResult(processed_images=len(image_paths), matches=all_matches, output_dir=output_dir)


def write_summary(matches: list[MatchedItem], output_dir: Path) -> None:
    summary_path = output_dir / "summary.txt"
    if not matches:
        summary_path.write_text(
            "No items were recognized. Try bigger/clearer screenshots, or check that "
            "data/logs/app.log doesn't show a Tesseract error.\n",
            encoding="utf-8",
        )
        return

    by_name: dict[str, list[MatchedItem]] = {}
    for m in matches:
        by_name.setdefault(m.name, []).append(m)

    lines = [f"WF-PriceTracker summary - {len(matches)} item instance(s) detected\n"]
    total = 0.0
    for name, instances in sorted(by_name.items(), key=lambda kv: kv[0]):
        price = instances[0].price
        approx = "~" if price.used_fallback else ""
        count = len(instances)
        subtotal = price.avg_platinum * count
        total += subtotal
        lines.append(f"  {name}  x{count}  @ {approx}{price.avg_platinum:g}p avg  = {subtotal:g}p")

    lines.append(
        f"\nEstimated total: {total:g}p "
        "(naive sum of avg sell price x detected instances - "
        "a '~' means no online/in-game sellers were found so it falls back to all listings; "
        "this can't tell stack quantities apart from repeated icons, so treat it as a rough estimate)"
    )
    summary_path.write_text("\n".join(lines), encoding="utf-8")
