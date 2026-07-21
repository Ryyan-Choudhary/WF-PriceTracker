"""Fetches and caches the canonical warframe.market item catalog, and does
fuzzy name matching between messy OCR text and real item names.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests
from rapidfuzz import fuzz, process

from . import config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Item:
    name: str
    slug: str
    tags: tuple[str, ...]


class ItemsIndex:
    """In-memory index of all tradable items, backed by an on-disk cache."""

    def __init__(self, items: list[Item]):
        self._items = items
        # rapidfuzz wants a flat sequence of choices to score against;
        # keep a parallel list of Item objects to map matches back.
        self._names = [it.name for it in items]

    def __len__(self) -> int:
        return len(self._items)

    def match(self, text: str) -> Optional[Item]:
        """Fuzzy-match a raw OCR string to the closest known item name.

        Returns None if nothing clears the configured confidence cutoff -
        this is what keeps UI chrome ("INVENTORY", "SORT BY", ...) from
        being reported as items.
        """
        text = text.strip()
        if len(text) < config.OCR_MIN_TEXT_LEN:
            return None
        result = process.extractOne(
            text,
            self._names,
            scorer=fuzz.WRatio,
            score_cutoff=config.FUZZY_MATCH_SCORE_CUTOFF,
        )
        if result is None:
            return None
        _matched_name, _score, idx = result
        return self._items[idx]


def _fetch_items_from_api() -> list[Item]:
    url = f"{config.WFM_API_BASE}/items"
    resp = requests.get(
        url,
        headers={"accept": "application/json"},
        timeout=config.HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    payload = resp.json()
    items: list[Item] = []
    for raw in payload.get("data", []):
        en = (raw.get("i18n") or {}).get("en") or {}
        name = en.get("name")
        slug = raw.get("slug")
        if not name or not slug:
            continue
        items.append(Item(name=name, slug=slug, tags=tuple(raw.get("tags") or ())))
    return items


def _load_cache() -> Optional[list[Item]]:
    if not config.ITEMS_CACHE_FILE.exists():
        return None
    try:
        raw = json.loads(config.ITEMS_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fetched_at = raw.get("fetched_at", 0)
    if time.time() - fetched_at > config.ITEMS_CACHE_TTL_SECONDS:
        return None
    try:
        return [
            Item(name=d["name"], slug=d["slug"], tags=tuple(d.get("tags", ())))
            for d in raw["items"]
        ]
    except (KeyError, TypeError):
        return None


def _save_cache(items: list[Item]) -> None:
    payload = {
        "fetched_at": time.time(),
        "items": [{"name": it.name, "slug": it.slug, "tags": list(it.tags)} for it in items],
    }
    config.ITEMS_CACHE_FILE.write_text(json.dumps(payload), encoding="utf-8")


def load_items_index(force_refresh: bool = False) -> ItemsIndex:
    """Load the item catalog, preferring a fresh on-disk cache over the network."""
    items = None if force_refresh else _load_cache()
    if items is None:
        log.info("Fetching item catalog from warframe.market...")
        try:
            items = _fetch_items_from_api()
            _save_cache(items)
            log.info("Fetched %d items from warframe.market", len(items))
        except requests.RequestException as exc:
            log.warning("Failed to fetch item catalog (%s); trying stale cache", exc)
            if config.ITEMS_CACHE_FILE.exists():
                raw = json.loads(config.ITEMS_CACHE_FILE.read_text(encoding="utf-8"))
                items = [
                    Item(name=d["name"], slug=d["slug"], tags=tuple(d.get("tags", ())))
                    for d in raw["items"]
                ]
            else:
                raise
    else:
        log.info("Loaded %d items from cache", len(items))
    return ItemsIndex(items)
