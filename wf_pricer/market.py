"""Fetches live sell orders from warframe.market and turns them into a
single "what would I actually get for this" price estimate, with a short-TTL
on-disk cache so processing a batch of screenshots with repeated items
doesn't hammer the API.

get_prices() fetches many items concurrently (config.PRICE_FETCH_WORKERS
worker threads) since pricing is the dominant, network-bound cost of a scan.
The shared cache is guarded by a lock so concurrent workers can't corrupt it.
"""
from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from . import config

log = logging.getLogger(__name__)

_cache: dict = {}
_cache_loaded = False
_cache_lock = threading.RLock()  # guards _cache + disk writes (workers share them)


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
    with _cache_lock:
        if _cache_loaded:
            return
        _cache_loaded = True
        if config.PRICE_CACHE_FILE.exists():
            try:
                _cache = json.loads(config.PRICE_CACHE_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                _cache = {}


def _save_disk_cache() -> None:
    # Caller must hold _cache_lock (json.dumps must see a stable dict).
    try:
        config.PRICE_CACHE_FILE.write_text(json.dumps(_cache), encoding="utf-8")
    except OSError:
        log.warning("Could not write price cache to disk", exc_info=True)


# Rewriting the whole cache file after every single price would mean one full
# JSON dump per item in a grid scan, so writes are debounced: the timer is
# pushed back on each new price and only fires once the burst goes quiet.
_cache_dirty = False
_cache_write_timer: Optional[threading.Timer] = None


def _schedule_disk_save() -> None:
    """Debounce a disk write. Caller must hold _cache_lock - the timer handle
    is shared with the worker threads that are also scheduling saves."""
    global _cache_dirty, _cache_write_timer
    _cache_dirty = True
    if _cache_write_timer is not None:
        _cache_write_timer.cancel()
    _cache_write_timer = threading.Timer(config.PRICE_CACHE_WRITE_DELAY_S, _flush_disk_save)
    _cache_write_timer.daemon = True
    _cache_write_timer.start()


def _flush_disk_save() -> None:
    global _cache_dirty, _cache_write_timer
    with _cache_lock:
        if not _cache_dirty:
            return
        _save_disk_cache()
        _cache_dirty = False
        _cache_write_timer = None


def _flush_on_shutdown() -> None:
    """The debounce timer is a daemon thread, so a pending write would be lost
    if the app exits during the quiet period right after a scan."""
    global _cache_write_timer
    with _cache_lock:
        if _cache_write_timer is not None:
            _cache_write_timer.cancel()
            _cache_write_timer = None
    _flush_disk_save()


atexit.register(_flush_on_shutdown)


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


def _cached_estimate(slug: str) -> Optional[PriceEstimate]:
    """Returns a fresh cached estimate for slug, or None if missing/stale.
    Thread-safe."""
    _load_disk_cache()
    with _cache_lock:
        entry = _cache.get(slug)
        if entry and time.time() - entry["ts"] < config.PRICE_CACHE_TTL_SECONDS:
            return PriceEstimate(**entry["estimate"])
    return None


def get_price(slug: str) -> PriceEstimate:
    """Return a price estimate for the given item slug, using a short-lived
    on-disk cache to avoid refetching the same item repeatedly within a run
    (or across runs a few minutes apart). Thread-safe.
    """
    cached = _cached_estimate(slug)
    if cached is not None:
        return cached

    now = time.time()
    try:
        orders = _fetch_orders(slug)
        time.sleep(config.REQUEST_DELAY_SECONDS)
    except requests.RequestException as exc:
        log.warning("Failed to fetch orders for %s: %s", slug, exc)
        with _cache_lock:
            entry = _cache.get(slug)
        if entry:  # serve stale data rather than nothing
            return PriceEstimate(**entry["estimate"])
        return PriceEstimate(0.0, 0, 0, used_fallback=False)

    estimate = _estimate_from_orders(orders)
    with _cache_lock:
        _cache[slug] = {"ts": now, "estimate": estimate.__dict__}
        _schedule_disk_save()
    return estimate


def get_prices(
    slugs: list[str],
    on_result: Optional[Callable[[str, PriceEstimate], None]] = None,
) -> dict[str, PriceEstimate]:
    """Fetch prices for many item slugs at once, returning {slug: estimate}.

    Cached slugs resolve instantly; the rest are fetched concurrently using
    config.PRICE_FETCH_WORKERS worker threads (each still waits
    REQUEST_DELAY_SECONDS per request, so the worker count is effectively the
    concurrency / rate dial). on_result(slug, estimate) is called as each one
    resolves, so a caller can update an overlay incrementally.
    """
    unique = list(dict.fromkeys(slugs))  # dedupe, preserve order
    results: dict[str, PriceEstimate] = {}

    # Resolve cache hits up front (no threads/network needed).
    to_fetch = []
    for slug in unique:
        cached = _cached_estimate(slug)
        if cached is not None:
            results[slug] = cached
            if on_result:
                on_result(slug, cached)
        else:
            to_fetch.append(slug)

    if not to_fetch:
        return results

    workers = max(1, min(config.PRICE_FETCH_WORKERS, len(to_fetch)))
    if workers == 1:
        for slug in to_fetch:
            est = get_price(slug)
            results[slug] = est
            if on_result:
                on_result(slug, est)
        return results

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wf-price") as pool:
        future_to_slug = {pool.submit(get_price, slug): slug for slug in to_fetch}
        for future in as_completed(future_to_slug):
            slug = future_to_slug[future]
            try:
                est = future.result()
            except Exception:
                log.exception("Price fetch failed for %s", slug)
                est = PriceEstimate(0.0, 0, 0, used_fallback=False)
            results[slug] = est
            if on_result:
                on_result(slug, est)
    return results
