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


# One (rank, platinum) point for a headline stat. rank is None for items that
# aren't ranked; otherwise it's the mod/arcane rank that price applies to.
RankPrice = tuple[Optional[int], int]


@dataclass(frozen=True)
class ItemStats:
    """A fuller market picture than PriceEstimate for the manual lookup, built
    entirely from the live order book (the v2 orders endpoint): the sell side
    (lowest / highest ask, how many sellers online) and the buy side (best
    offer, how many buyers online). volume_48h has no v2 source, so it's pulled
    best-effort from the deprecated v1 statistics endpoint (None if unreachable).

    Each of the three price stats is a tuple of (rank, platinum) points. For an
    UNRANKED item that's a single (None, price). For a RANKED item (mods,
    arcanes) it's the value at the lowest AND highest rank on the book - a Rank
    0 and a Rank 5 arcane trade at completely different prices, so one blended
    number would be meaningless. Collapses to one point when only one rank is
    listed.
    """
    slug: str
    lowest_sell: tuple[RankPrice, ...]
    highest_sell: tuple[RankPrice, ...]
    highest_buy: tuple[RankPrice, ...]
    volume_48h: Optional[int]
    sellers_online: int
    buyers_online: int

    @property
    def has_data(self) -> bool:
        return self.sellers_online > 0 or self.buyers_online > 0 or bool(self.volume_48h)


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


def _order_rank(order: dict) -> Optional[int]:
    """The mod/arcane rank on an order, or None if the item isn't ranked. Tries
    the v2 field first, then older/alternative spellings, so a Rank 0 listing
    still reports 0 (a real rank) rather than being mistaken for unranked."""
    for key in ("rank", "modRank", "mod_rank"):
        value = order.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


# The 48-hour trade volume has no v2 endpoint; this is the (deprecated) v1
# statistics endpoint, used ONLY for that number and allowed to fail quietly.
_WFM_V1_BASE = "https://api.warframe.market/v1"


def _fetch_48h_volume(slug: str) -> Optional[int]:
    """Items traded in the last 48h, from the deprecated v1 statistics endpoint.
    None when it can't be reached (network / endpoint gone); a real 0 when the
    item simply hasn't sold."""
    try:
        resp = requests.get(
            f"{_WFM_V1_BASE}/items/{slug}/statistics",
            headers={"accept": "application/json", "Platform": config.WFM_PLATFORM},
            timeout=config.HTTP_TIMEOUT_SECONDS,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        payload = resp.json().get("payload")
    except (requests.RequestException, ValueError) as exc:
        log.warning("Stats: could not fetch 48h volume for %s: %s", slug, exc)
        return None
    if not payload:
        return None
    for key in ("statistics_closed", "statistics_live"):
        buckets = (payload.get(key) or {}).get("48hours")
        if buckets:
            return sum(int(b.get("volume") or 0) for b in buckets)
    return 0


def _rank_tiers(orders: list[dict], reduce_fn) -> tuple[RankPrice, ...]:
    """Reduce one side's orders to (rank, platinum) points via reduce_fn (min
    for a "lowest", max for a "highest").

    Unranked items -> a single (None, price). Ranked items -> the value at the
    LOWEST and HIGHEST rank present (so a Rank 0 and a Rank 5 arcane are each
    reported), collapsing to one point when only one rank is on the book. An
    empty side -> ((None, 0),) so a row still shows.
    """
    if not orders:
        return ((None, 0),)
    ranks = [_order_rank(o) for o in orders]
    if all(r is None for r in ranks):
        return ((None, int(reduce_fn(int(o["platinum"]) for o in orders))),)

    present = sorted({r for r in ranks if r is not None})
    tiers: list[RankPrice] = []
    for rank in (present[0], present[-1]):
        prices = [int(o["platinum"]) for o, r in zip(orders, ranks) if r == rank]
        point: RankPrice = (rank, int(reduce_fn(prices)))
        if point not in tiers:  # collapse when min rank == max rank
            tiers.append(point)
    return tuple(tiers)


def get_item_stats(slug: str) -> ItemStats:
    """Market snapshot for one item, for the manual search popup.

    The order book (lowest / highest ask, highest bid, online counts, and - for
    ranked mods/arcanes - the value at each rank tier) comes from the v2 orders
    endpoint; the 48h trade volume comes best-effort from v1. Never raises: a
    failed fetch yields an empty book / None volume, and has_data reports
    whether anything came back.
    """
    try:
        orders = _fetch_orders(slug)
    except requests.RequestException as exc:
        log.warning("Stats: could not fetch orders for %s: %s", slug, exc)
        orders = []

    def online(kind: str) -> list[dict]:
        picked = [
            o for o in orders
            if o.get("type") == kind and o.get("visible", True)
            and (o.get("user") or {}).get("status") in config.PREFERRED_USER_STATUSES
            and isinstance(o.get("platinum"), (int, float))
        ]
        return sorted(picked, key=lambda o: o["platinum"])

    sells = online("sell")
    buys = online("buy")

    return ItemStats(
        slug=slug,
        lowest_sell=_rank_tiers(sells, min),
        highest_sell=_rank_tiers(sells, max),
        highest_buy=_rank_tiers(buys, max),
        volume_48h=_fetch_48h_volume(slug),
        sellers_online=len(sells),
        buyers_online=len(buys),
    )


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
