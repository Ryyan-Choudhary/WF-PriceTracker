"""Fetches live sell orders from warframe.market and turns them into a
single "what would I actually get for this" price estimate, with a short-TTL
on-disk cache so processing a batch of screenshots with repeated items
doesn't hammer the API.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

from . import config

log = logging.getLogger(__name__)

_cache: dict = {}
_cache_loaded = False


@dataclass(frozen=True)
class PriceEstimate:
    avg_platinum: float
    lowest_platinum: int
    sample_size: int
    used_fallback: bool  # True if no online/ingame sellers were found

    @property
    def has_data(self) -> bool:
        return self.sample_size > 0


def _load_disk_cache() -> None:
    global _cache, _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    if config.PRICE_CACHE_FILE.exists():
        try:
            _cache = json.loads(config.PRICE_CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _cache = {}


def _save_disk_cache() -> None:
    try:
        config.PRICE_CACHE_FILE.write_text(json.dumps(_cache), encoding="utf-8")
    except OSError:
        log.warning("Could not write price cache to disk", exc_info=True)


def _fetch_orders(slug: str) -> list[dict]:
    url = f"{config.WFM_API_BASE}/orders/item/{slug}"
    resp = requests.get(
        url,
        headers={"accept": "application/json"},
        timeout=config.HTTP_TIMEOUT_SECONDS,
    )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return resp.json().get("data", [])


def _estimate_from_orders(orders: list[dict]) -> PriceEstimate:
    sells = [o for o in orders if o.get("type") == "sell" and o.get("visible", True)]
    if not sells:
        return PriceEstimate(0.0, 0, 0, used_fallback=False)

    preferred = [
        o for o in sells
        if (o.get("user") or {}).get("status") in config.PREFERRED_USER_STATUSES
    ]
    used_fallback = not preferred
    pool = preferred if preferred else sells

    prices = sorted(o["platinum"] for o in pool)
    sample = prices[: config.PRICE_SAMPLE_SIZE]
    avg = sum(sample) / len(sample)
    return PriceEstimate(
        avg_platinum=round(avg, 1),
        lowest_platinum=prices[0],
        sample_size=len(sample),
        used_fallback=used_fallback,
    )


def get_price(slug: str) -> PriceEstimate:
    """Return a price estimate for the given item slug, using a short-lived
    on-disk cache to avoid refetching the same item repeatedly within a run
    (or across runs a few minutes apart).
    """
    _load_disk_cache()
    entry = _cache.get(slug)
    now = time.time()
    if entry and now - entry["ts"] < config.PRICE_CACHE_TTL_SECONDS:
        d = entry["estimate"]
        return PriceEstimate(**d)

    try:
        orders = _fetch_orders(slug)
        time.sleep(config.REQUEST_DELAY_SECONDS)
    except requests.RequestException as exc:
        log.warning("Failed to fetch orders for %s: %s", slug, exc)
        if entry:  # serve stale data rather than nothing
            return PriceEstimate(**entry["estimate"])
        return PriceEstimate(0.0, 0, 0, used_fallback=False)

    estimate = _estimate_from_orders(orders)
    _cache[slug] = {"ts": now, "estimate": estimate.__dict__}
    _save_disk_cache()
    return estimate
