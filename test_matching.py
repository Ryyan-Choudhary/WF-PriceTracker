"""Quick validation script for the matching-accuracy + performance changes."""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wf_pricer.items_db import ItemsIndex, _reject_query, normalize_name, Item
from wf_pricer import config

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

TEST_CASES = [
    # (input_text, expected_item_name_or_None_for_no_match_or_AMBIG)
    ("Afuris Prime Blueprint", "Afuris Prime Blueprint"),
    ("Afuris Prime Link", "Afuris Prime Link"),
    # Truncated, but "Akarius Prime Barrel" is the ONLY item it can be, so
    # matching it is correct - refusing would just lose a readable item.
    ("ius Prime Barrel", "Akarius Prime Barrel"),
    ("Titania Prime", "AMBIG"),  # ambiguous - should refuse
    ("Silva & Aegis Prime", "AMBIG"),  # ambiguous
    ("Stop Scan Mode (F10)", None),  # UI artifact
    ("No Return", "No Return"),  # real item with phrase that used to be blacklisted
    ("No Current Leap", "No Current Leap"),  # ditto
    ("Bo Prime Handle", "Bo Prime Handle"),  # 2-letter base name must still anchor
    ("Meso F2 Relic", "Meso F2 Relic"),  # real item, not the "F2" hotkey label
    ("No Item", None),  # UI artifact
    ("INVENTORY", None),  # UI artifact
    ("[[18]]", None),  # OCR artifact
    ("====", None),  # OCR artifact
    ("Paris Prime Upper Limb Perigale Prime Receiver", None),  # cross-tile garbage
    ("Caliban Prime Chassis Blueprint", "Caliban Prime Chassis Blueprint"),
    ("Bronco Prime Barrel", "Bronco Prime Barrel"),
    ("Atlas Prime Neuroptics Blueprint", "Atlas Prime Neuroptics Blueprint"),
    # --- word runoff: whole-string scores tie, an early word separates them ---
    # Garbled tail, but word 1 ("volt") is decisive against the other families
    # the noise drags in.
    ("Volt Pr1me Neuroptlcs Blueprnt", "Volt Prime Neuroptics Blueprint"),
    # Identical until word 3, which is what the runoff has to notice.
    ("Rhino Prime Systems Blueprint", "Rhino Prime Systems Blueprint"),
    ("Rhino Prime Neuroptics Blueprint", "Rhino Prime Neuroptics Blueprint"),
]


def load_catalog() -> ItemsIndex:
    raw = json.loads(config.ITEMS_CACHE_FILE.read_text(encoding="utf-8"))
    items = []
    for d in raw["items"]:
        items.append(Item(
            name=d["name"],
            slug=d["slug"],
            tags=tuple(d.get("tags", ()))
        ))
    return ItemsIndex(items)


def main() -> None:
    log.info("Loading %d items from %s ...", len(json.loads(config.ITEMS_CACHE_FILE.read_text(encoding="utf-8"))["items"]), config.ITEMS_CACHE_FILE)
    idx = load_catalog()
    log.info("Loaded %d items (after excluding Sets).", len(idx))

    log.info("\n--- Rejection filter ---")
    for text in ["Stop Scan Mode (F10)", "No Return", "No Item", "INVENTORY", "[[18]]", "====",
                 "f10", "Meso F2 Relic", "No Current Leap"]:
        rejected = _reject_query(text)
        log.info("  %r -> rejected=%s", text, rejected)

    log.info("\n--- Matching ---")
    passed = 0
    failed = 0
    for text, expected in TEST_CASES:
        result = idx.match(text)
        if expected == "AMBIG":
            ok = result is None
            log.info("  %r -> %s (expected: None/AMBIG) [%s]", text, result.name if result else None, "PASS" if ok else "FAIL")
        elif expected is None:
            ok = result is None
            log.info("  %r -> %s (expected: None) [%s]", text, result.name if result else None, "PASS" if ok else "FAIL")
        else:
            ok = result is not None and result.name == expected
            log.info("  %r -> %s (expected: %s) [%s]", text, result.name if result else None, expected, "PASS" if ok else "FAIL")
        if ok:
            passed += 1
        else:
            failed += 1

    log.info("\nResults: %d passed, %d failed out of %d", passed, failed, len(TEST_CASES))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
