"""Fetches and caches the canonical warframe.market item catalog, and does
fuzzy name matching between messy OCR text and real item names.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests
from rapidfuzz import fuzz, process

from . import config

log = logging.getLogger(__name__)

_NON_NAME_CHARS = re.compile(r"[^A-Za-z0-9& ]+")
_WHITESPACE = re.compile(r"\s+")

# Text the app's own overlay and Warframe's chrome put on screen, which OCR
# picks up just like an item label. Matched against the NORMALIZED query (so
# "Stop Scan Mode (F10)" has already become "stop scan mode f10"), and
# anchored, because these have to be the whole line to count: an unanchored
# pattern takes real items down with it. "No Return" and "No Current Leap"
# are real mods, and "F1".."F5" appear in fourteen real relic names
# ("Meso F2 Relic"), so a bare `f\d` or a substring "no return" would blacklist
# items you actually scan.
_UI_ARTIFACT_RE = re.compile(
    r"^(?:"
    r"(?:start|stop) scan mode(?: f\d+)?"
    r"|no items? recognized"
    r"|scanning region"
    r"|inventory"
    r"|f\d+"          # a lone hotkey label, not the "F2" inside "Meso F2 Relic"
    r"|c\d+"
    r"|\d+"
    r")$",
    re.IGNORECASE,
)


def normalize_name(text: str) -> str:
    """Lowercase and reduce to letters/digits/&/spaces, turning every other
    character into a SPACE rather than deleting it.

    Splitting on punctuation (instead of stripping it) matters: OCR loves to
    drop the gap between two words and leave an artifact there, e.g.
    "Prime Chassis" coming back as "Pfime'thassis". Deleting the quote keeps
    one unmatchable blob; turning it into a space recovers two words that
    fuzzy-match "Prime" and "Chassis" properly.
    """
    return _WHITESPACE.sub(" ", _NON_NAME_CHARS.sub(" ", text)).strip().lower()


def _reject_query(text: str) -> bool:
    """True if text is this app's own overlay chrome or pure OCR noise
    ("====", "[[18]]") rather than anything worth matching.

    Normalizes first, which is what lets the patterns stay anchored: it strips
    the punctuation these arrive with ("Stop Scan Mode (F10)") and collapses
    separator noise to nothing at all. Safe to pass an already-normalized
    query - normalize_name is idempotent.
    """
    normalized = normalize_name(text)
    if not normalized:
        return True
    return _UI_ARTIFACT_RE.match(normalized) is not None


@dataclass(frozen=True)
class Item:
    name: str
    slug: str
    tags: tuple[str, ...]


class ItemsIndex:
    """In-memory index of all tradable items, backed by an on-disk cache.

    Deliberately excludes "Set" listings (warframe.market tags these with
    "set", e.g. "Fluctus Prime Set"). A Set is a trading bundle representing
    a full collection of parts + blueprint sold as one lot - it never
    appears as its own entry in your actual in-game inventory (you only
    ever see the individual blueprint pieces: Barrel, Stock, Receiver, ...).
    Without this, a short "X Set" name can out-score the correct, longer
    individual part name on plain string similarity, which was causing
    real mismatches (e.g. OCR'd "Fluctus Prime Stock" matching to
    "Fluctus Prime Set" instead of "Fluctus Prime Stock Blueprint").
    """

    def __init__(self, items: list[Item]):
        # Full catalog kept for manual search only (which SHOULD find sets -
        # people look up "Rhino Prime Set" prices); matching still works off
        # the set-excluded list below.
        self._all_items = list(items)
        self._items = [it for it in items if "set" not in it.tags]
        # rapidfuzz wants a flat sequence of choices to score against;
        # keep a parallel list of Item objects to map matches back.
        self._names = [it.name for it in self._items]
        self._norm_names = [normalize_name(n) for n in self._names]

        # "Family" index: every item's base name (its first word - the
        # frame/weapon, e.g. "atlas", "bronco", "serration") mapped to the
        # items that share it. match() anchors on this first, so a garbled
        # middle can't drag the result off to an unrelated item that merely
        # shares generic words like "prime blueprint".
        self._base_to_indices: dict[str, list[int]] = {}
        for i, norm in enumerate(self._norm_names):
            parts = norm.split()
            if parts:
                self._base_to_indices.setdefault(parts[0], []).append(i)
        self._bases = sorted(self._base_to_indices)

    def __len__(self) -> int:
        return len(self._items)

    def search_entries(self) -> list[tuple[str, str]]:
        """(name, slug) for every catalog item INCLUDING sets, alphabetically,
        for the manual search box's autocomplete."""
        return sorted(((it.name, it.slug) for it in self._all_items), key=lambda t: t[0].lower())

    def match(self, text: str) -> Optional[Item]:
        """Match a raw OCR string to a known item, in two stages.

        1. ANCHOR: fuzzy-match each word of the text against the set of base
           names to decide which item families it could belong to. The base
           name is the most distinctive part and usually survives OCR intact,
           so this is what keeps "wy Atlas Pfime'thassis Blueprint" in the
           Atlas family instead of drifting to "Wyrm Prime Blueprint" on the
           strength of shared generic words.
        2. RANK: score the whole text against only that family's items.

        When the top candidates are too close to call on the whole string,
        stage 3 is a WORD RUNOFF (see _break_tie_by_word): compare them one
        word at a time - first word, then second, then third - and take the
        one that wins a position outright.

        Returns None if nothing clears the confidence cutoff (which is what
        keeps UI chrome like "INVENTORY" from being reported as an item), or
        if even the runoff can't separate the leaders - e.g. text missing an
        item's part-specific last word ("Titania Prime") draws against every
        part of that frame at every word it has, and guessing means silently
        reporting the wrong item.
        """
        if len(text.strip()) < config.OCR_MIN_TEXT_LEN:
            return None
        query = normalize_name(text)
        if not query or _reject_query(query):
            return None

        candidates = self._anchor_candidates(query)

        # An exact read - or the same words in a different order - is
        # unambiguous, so take it before scoring. Without this, a scrambled
        # "Caliban Blueprint Prime" ties with "Caliban Prime Chassis
        # Blueprint" (its words are a subset of that one) and gets refused.
        # Requiring the word sets to be EQUAL - not merely a subset - is what
        # keeps a genuinely incomplete "Titania Prime" from matching here.
        exact = self._exact_candidate(query, candidates)
        if exact is not None:
            return self._items[exact]

        names = [self._norm_names[i] for i in candidates]
        results = process.extract(
            query,
            names,
            scorer=fuzz.WRatio,
            score_cutoff=config.FUZZY_MATCH_SCORE_CUTOFF,
            limit=config.FUZZY_MATCH_CANDIDATES,
        )
        if not results:
            return None
        if len(results) == 1 or results[0][1] - results[1][1] >= config.FUZZY_MATCH_MIN_MARGIN:
            return self._items[candidates[results[0][2]]]

        # Too close to call on the whole string. Before refusing, try to
        # separate the tied group a word at a time (see _break_tie_by_word).
        tied = [candidates[r[2]] for r in results
                if results[0][1] - r[1] < config.FUZZY_MATCH_MIN_MARGIN]
        runoff = self._break_tie_by_word(query, tied)
        if runoff is not None:
            winner, pos = runoff
            log.info(
                "Tie for %r between %s broken on %s -> %r",
                text, [self._names[i] for i in tied],
                f"word {pos + 1}" if pos >= 0 else "coverage of the text",
                self._names[winner],
            )
            return self._items[winner]

        log.info(
            "Ambiguous match for %r: %s - refusing to guess",
            text, [self._names[i] for i in tied],
        )
        return None

    def _break_tie_by_word(self, query: str, tied: list[int]) -> Optional[tuple[int, int]]:
        """Separate candidates the whole-string score couldn't, by comparing
        them against the query ONE WORD AT A TIME.

        Position 0 (the base name) is compared first; if that draws, position
        1, then 2 - configurable via FUZZY_TIEBREAK_MAX_WORDS. A position
        decides the runoff only if exactly one candidate leads it by
        FUZZY_TIEBREAK_MIN_MARGIN; otherwise the trailing candidates are
        dropped and the survivors go on to the next word. This is what picks
        "Volt Prime Systems Blueprint" over its Neuroptics sibling when OCR
        garbles enough of the string that the two score identically overall.

        Returns (item_index, deciding_word_position), the position being -1
        when the coverage guard below left only one candidate standing before
        any word was compared. Returns None - i.e. keeps the old
        refuse-to-guess behavior - when the query runs out of words with more
        than one candidate still standing (a genuinely incomplete read like
        "Titania Prime"), or when no candidate accounts for enough of the
        query (OCR having smeared two neighbouring tiles into one line).
        """
        q_words = query.split()
        if not q_words:
            return None

        # A runoff only makes sense between candidates that explain the text.
        # Two tiles merged into one line leave far more query words than any
        # single item has, and the leading item would otherwise win on word 0.
        alive = [
            i for i in tied
            if len(q_words) - len(self._norm_names[i].split()) <= config.FUZZY_TIEBREAK_MAX_EXTRA_WORDS
        ]
        if len(alive) < 2:
            return (alive[0], -1) if len(alive) == 1 else None

        for pos in range(min(len(q_words), config.FUZZY_TIEBREAK_MAX_WORDS)):
            q_word = q_words[pos]
            scores = []
            for i in alive:
                words = self._norm_names[i].split()
                # A candidate with no word at this position has nothing to
                # offer the comparison and scores zero rather than being
                # skipped, so a shorter name can lose the runoff outright.
                scores.append(fuzz.ratio(q_word, words[pos]) if pos < len(words) else 0.0)

            best = max(scores)
            leaders = [i for i, s in zip(alive, scores) if best - s < config.FUZZY_TIEBREAK_MIN_MARGIN]
            if len(leaders) == 1:
                return leaders[0], pos
            alive = leaders  # still drawn here; the trailing ones are out

        return None

    def _exact_candidate(self, query: str, candidates: list[int]) -> Optional[int]:
        """Index of the one candidate matching `query` exactly, or by an
        identical set of words in any order. None if there's no such match
        (or, defensively, more than one)."""
        for i in candidates:
            if self._norm_names[i] == query:
                return i
        query_words = frozenset(query.split())
        same_words = [i for i in candidates if frozenset(self._norm_names[i].split()) == query_words]
        return same_words[0] if len(same_words) == 1 else None

    def _anchor_candidates(self, query: str) -> list[int]:
        """Indices of items whose base name is plausibly present in `query`.
        Falls back to every item when nothing anchors, so an unusual read can
        still match on overall similarity alone.
        """
        words = query.split()
        tokens = [t for t in words if len(t) >= config.FUZZY_ANCHOR_MIN_TOKEN_LEN]
        # The base name IS the first word, so probe it even when it's below the
        # speck threshold. Five real families have a 1-2 letter base ("Bo Prime
        # Handle", "No Return", "Da-Ren", the Hollvania scenes) and were
        # unreachable without this - the threshold exists to ignore specks
        # found MID-name, not to discard the one word we index families by.
        if words and words[0] not in tokens:
            tokens.insert(0, words[0])
        if not tokens:
            # Nothing but specks - no real item name survives OCR as only
            # 1-2 character tokens, and scoring the catalog against noise is
            # exactly how a false positive gets in.
            return []

        families: set[str] = set()
        for token in tokens:
            for base, _score, _i in process.extract(
                token,
                self._bases,
                scorer=fuzz.partial_ratio,
                score_cutoff=config.FUZZY_ANCHOR_SCORE_CUTOFF,
                limit=config.FUZZY_ANCHOR_MAX_FAMILIES,
            ):
                families.add(base)

        indices = [i for base in families for i in self._base_to_indices[base]]
        return indices or list(range(len(self._items)))


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
