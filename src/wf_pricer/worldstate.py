"""Live Warframe world-state items for the Display HUD: the location cycles
(day/night, warm/cold, Fass/Vome, Spiral), the weekly Archon Hunt, the 1999
calendar season, and the daily / weekly reset timers.

The cycles and events are pulled from the community Warframe Status API
(warframestat.us) in a single request for the whole platform worldstate (/pc) -
one request for every card is lighter on the upstream and far less likely to
partially fail than a request per field. Every item reports an `expiry`, so once
fetched the countdown is computed locally (see format_remaining) - the network
is only needed to learn the current phase and when it flips, not to tick the
clock.

The reset timers need no network at all: Warframe resets on a fixed UTC schedule
(daily at 00:00, weekly Monday 00:00), so their expiry is computed straight from
the clock (see _LOCAL_KINDS / _parse_local).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from . import config

log = logging.getLogger(__name__)


# The items the Display tab offers, in display order. `kind` drives how the
# label + icon + expiry are derived (see _parse / _parse_local). This registry
# is the one place to add another item later - more can be bolted on here
# without touching the overlay.
#
# Most items pull a single worldstate field (/pc/<field>) and show its current
# phase. The reset timers carry field=None: they need no network, their expiry
# is computed locally from the clock (see _LOCAL_KINDS / _parse_local).
DISPLAY_ITEMS: dict[str, dict] = {
    "cetus":    {"field": "cetusCycle",   "place": "Cetus",       "kind": "daynight"},
    "earth":    {"field": "earthCycle",   "place": "Earth",       "kind": "daynight"},
    "vallis":   {"field": "vallisCycle",  "place": "Orb Vallis",  "kind": "warmcold"},
    "cambion":  {"field": "cambionCycle", "place": "Deimos",      "kind": "fassvome"},
    "duviri":   {"field": "duviriCycle",  "place": "Duviri",      "kind": "duviri"},
    "archon":   {"field": "archonHunt",   "place": "Archon Hunt", "kind": "archon"},
    "calendar": {"field": "calendar",     "place": "1999 Calendar", "kind": "calendar"},
    "weekly":   {"field": None,           "place": "Weekly Reset", "kind": "reset_weekly"},
    "daily":    {"field": None,           "place": "Daily Reset",  "kind": "reset_daily"},
}
DISPLAY_ITEM_ORDER: list[str] = [
    "cetus", "earth", "vallis", "cambion", "duviri",
    "archon", "calendar", "weekly", "daily",
]

# Kinds whose state is computed from the local clock, not fetched (no network).
_LOCAL_KINDS = frozenset({"reset_weekly", "reset_daily"})

# Human label for an item kind (shown next to the checkbox). "" means the place
# name already says it all, so item_label shows just the place (no suffix).
KIND_LABELS = {
    "daynight": "Day / Night", "warmcold": "Warm / Cold",
    "fassvome": "Fass / Vome", "duviri": "Spiral",
    "archon": "Weekly boss", "calendar": "season",
    "reset_weekly": "", "reset_daily": "",
}

# Fass / Vome (Deimos) get a custom-drawn icon instead of a plain emoji: an
# oblong, snake-like orb with spider legs, evoking the Cambion Drift wyrms. The
# "orb:<hex>" tokens are recognised by glyphs.icon_image, which draws the shape
# in that colour. Colours kept as before: Fass orange, Vome blue.
_FASS_ICON = "orb:#F6902A"   # orange orb-with-legs
_VOME_ICON = "orb:#3AA0E6"   # blue orb-with-legs

# Per-kind mapping of the API's phase to (phase label, icon). The icon is an
# emoji character (rendered from the emoji font) or an "orb:<hex>" custom token.
_PHASES = {
    "daynight": {True: ("Day", "☀️"), False: ("Night", "\U0001F319")},   # ☀️ / 🌙
    "warmcold": {True: ("Warm", "\U0001F525"), False: ("Cold", "❄️")},   # 🔥 / ❄️
    "fassvome": {"fass": ("Fass", _FASS_ICON), "vome": ("Vome", _VOME_ICON)},
}
# Duviri's Spiral rotates through moods; each maps to an expressive emoji.
_DUVIRI_MOODS = {
    "joy": ("Joy", "\U0001F604"),        # 😄
    "anger": ("Anger", "\U0001F620"),    # 😠
    "envy": ("Envy", "\U0001F612"),      # 😒
    "sorrow": ("Sorrow", "\U0001F622"),  # 😢
    "fear": ("Fear", "\U0001F628"),      # 😨
}
# Fixed icons for the non-cycle items (they don't rotate through phases).
_ARCHON_ICON = "⚔️"      # ⚔️
_CALENDAR_ICON = "\U0001F4C5"      # 📅
_WEEKLY_ICON = "\U0001F504"        # 🔄
_DAILY_ICON = "⏰"             # ⏰

# Fallback icon when an item can't be read (offline / bad payload).
_UNKNOWN_ICON = {
    "daynight": "\U0001F319", "warmcold": "❄️",
    "fassvome": _VOME_ICON, "duviri": "\U0001F300",  # 🌀
    "archon": _ARCHON_ICON, "calendar": _CALENDAR_ICON,
    "reset_weekly": _WEEKLY_ICON, "reset_daily": _DAILY_ICON,
}


@dataclass
class CycleState:
    key: str
    place: str
    phase_label: str            # "Day" / "Night" / "Warm" / "Cold" / "Fass" / "Vome" / "—"
    icon: str                   # glyph key for glyphs.icon_image
    expiry: datetime | None     # when the current phase ends (UTC), or None if unknown
    ok: bool = True             # False if the fetch/parse failed
    # Optional extra lines under the countdown (Archon shard; today's calendar
    # events). Each is a (text, is_header) pair - headers (the calendar event
    # type: "To Do" / "Override" / "Big Prize!") are drawn emphasised, the rest
    # dimmed. Empty for the plain cycle/timer cards.
    detail: list = field(default_factory=list)


# Each Archon Hunt boss always drops one Archon Shard colour.
_ARCHON_SHARD = {
    "amar": "Crimson Shard",
    "nira": "Amber Shard",
    "boreal": "Azure Shard",
}


_session = requests.Session()
_session.headers.update({"User-Agent": "WF-PriceTracker (worldstate display)"})


def item_label(key: str) -> str:
    """'Cetus (Day / Night)' style label for the checklist. Items whose kind
    label is empty (the reset timers) show just their place name."""
    spec = DISPLAY_ITEMS[key]
    label = KIND_LABELS.get(spec["kind"], spec["kind"])
    return f"{spec['place']} ({label})" if label else spec["place"]


def fetch_states(keys) -> dict[str, "CycleState"]:
    """Fetch the current CycleState for each requested display key. Network
    failures are contained per-cycle: a failed one comes back with ok=False and
    a placeholder rather than aborting the others."""
    out: dict[str, CycleState] = {}
    blob: dict | None = None       # the whole worldstate, fetched once, lazily
    blob_err: Exception | None = None
    fetched = False
    for key in keys:
        spec = DISPLAY_ITEMS.get(key)
        if spec is None:
            continue
        if spec["kind"] in _LOCAL_KINDS:
            out[key] = _parse_local(key, spec)  # clock-only, no network
            continue
        if not fetched:                          # first network-backed card
            fetched = True
            try:
                blob = _get_worldstate()         # one request covers them all
            except requests.RequestException as err:
                blob_err = err                   # logged once, below, no traceback
        field_data = blob.get(spec["field"]) if blob is not None else None
        if field_data is None:                   # fetch failed, or field absent
            out[key] = CycleState(key, spec["place"], "—", _UNKNOWN_ICON[spec["kind"]], None, ok=False)
            continue
        try:
            out[key] = _parse(key, spec, field_data)
        except Exception:
            # A parse error (unexpected) is a real bug - keep the traceback.
            log.warning("World-state parse failed for %s", key, exc_info=True)
            out[key] = CycleState(key, spec["place"], "—", _UNKNOWN_ICON[spec["kind"]], None, ok=False)
    if blob_err is not None:
        net_keys = [k for k in keys
                    if DISPLAY_ITEMS.get(k) and DISPLAY_ITEMS[k]["kind"] not in _LOCAL_KINDS]
        log.warning("World-state fetch failed for %s: %s", ", ".join(net_keys), blob_err)
    return out


# Cloudflare / gateway statuses worth a quick retry: warframestat.us serves
# transient 5xx (notably 521 "web server is down") that often clear in a moment.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524})
_MAX_TRIES = 3


def _get_worldstate() -> dict:
    """Fetch the whole platform worldstate (/pc) in one request, retrying the
    transient gateway errors warframestat.us is prone to. One request for every
    card - rather than one per field - is far less likely to partially fail
    against a flaky upstream, and lighter on the server. Raises the last requests
    error if every attempt fails."""
    url = f"{config.WORLDSTATE_API_BASE}/{config.WORLDSTATE_PLATFORM}"
    last_exc: Exception = requests.RequestException(f"no response for {url}")
    for attempt in range(_MAX_TRIES):
        try:
            resp = _session.get(url, timeout=config.HTTP_TIMEOUT_SECONDS)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
        else:
            if resp.status_code not in _RETRY_STATUSES:
                resp.raise_for_status()
                return resp.json()
            last_exc = requests.HTTPError(f"{resp.status_code} Server Error for url: {url}", response=resp)
        if attempt < _MAX_TRIES - 1:
            time.sleep(0.4 * (attempt + 1))  # brief backoff: 0.4s, then 0.8s
    raise last_exc


def api_heartbeat() -> bool:
    """Ping warframestat's /heartbeat health endpoint. True if the API itself is
    up. Lets a caller tell 'warframestat is unreachable' apart from 'warframestat
    is up but the live worldstate feed is momentarily unavailable'. Best-effort:
    any error returns False. A quick single try - no retries."""
    try:
        resp = _session.get(
            f"{config.WORLDSTATE_API_BASE}/heartbeat",
            timeout=min(5, config.HTTP_TIMEOUT_SECONDS),
        )
        return resp.status_code == 200
    except requests.RequestException:
        return False


def _parse(key: str, spec: dict, data) -> "CycleState":
    kind = spec["kind"]
    detail: list[tuple[str, bool]] = []
    # A few field endpoints hand back a list of objects; take the current one.
    if isinstance(data, list):
        data = data[0] if data else {}
    if kind == "daynight":
        label, icon = _PHASES[kind][bool(data.get("isDay"))]
    elif kind == "warmcold":
        label, icon = _PHASES[kind][bool(data.get("isWarm"))]
    elif kind == "duviri":
        mood = str(data.get("state", "")).lower()
        label, icon = _DUVIRI_MOODS.get(mood, (mood.title() or "—", _UNKNOWN_ICON[kind]))
    elif kind == "archon":
        # Weekly Archon Hunt: name the boss, its shard drop, and count down to
        # the weekly expiry. The API gives e.g. "Archon Boreal"; the title
        # already says "Archon Hunt", so drop a redundant leading "Archon ".
        boss = str(data.get("boss") or "").strip()
        label = boss[len("archon "):].strip() if boss.lower().startswith("archon ") else (boss or "Archon")
        icon = _ARCHON_ICON
        if boss:
            detail = [("Drop: " + _ARCHON_SHARD.get(label.lower(), "Archon Shard"), True)]
    elif kind == "calendar":
        # 1999 Calendar: show the active season plus today's rewards, mission
        # name and objective; count down to when it advances (its expiry).
        label = str(data.get("season") or "").strip() or "—"
        icon = _CALENDAR_ICON
        detail = _calendar_today(data)
    else:  # fassvome
        phase_key = str(data.get("state", "")).lower()
        label, icon = _PHASES[kind].get(phase_key, ("—", _UNKNOWN_ICON[kind]))
    return CycleState(key, spec["place"], label, icon, _parse_expiry(data.get("expiry")), ok=True, detail=detail)


def _calendar_today(data) -> list[tuple[str, bool]]:
    """Detail lines for today's 1999-calendar day: for each event, its type as a
    header ("To Do" / "Override" / "Big Prize!") followed by its name and, where
    there is one, its objective/description. The `days` carry in-lore 1999 dates,
    so 'today' is the day whose date (matched in the calendar's own year) is the
    latest not after now. Returns [] if the calendar has no usable day data."""
    days = data.get("days")
    if not isinstance(days, list):
        return []
    parsed: list[tuple[datetime, list]] = []
    for day in days:
        if not isinstance(day, dict):
            continue
        dt = _parse_expiry(day.get("date"))
        if dt is not None:
            parsed.append((dt, day.get("events") or []))
    if not parsed:
        return []
    parsed.sort(key=lambda p: p[0])
    now = datetime.now(timezone.utc)
    try:
        today_ref = now.replace(year=parsed[0][0].year, hour=0, minute=0, second=0, microsecond=0)
    except ValueError:  # Feb 29 in a non-leap calendar year
        today_ref = now.replace(year=parsed[0][0].year, month=2, day=28, hour=0, minute=0, second=0, microsecond=0)
    past = [p for p in parsed if p[0] <= today_ref]
    _dt, events = past[-1] if past else parsed[0]
    return _calendar_lines(events)


def _calendar_lines(events) -> list[tuple[str, bool]]:
    """Turn a day's events into (text, is_header) display lines. Each event
    contributes a header line with its type, then its name (mission / override /
    reward) and any objective/description beneath. Long lines are trimmed to fit
    the card."""
    lines: list[tuple[str, bool]] = []
    for ev in events if isinstance(events, list) else []:
        if not isinstance(ev, dict):
            continue
        etype = str(ev.get("type") or "").strip()
        challenge = ev.get("challenge")
        upgrade = ev.get("upgrade")
        reward = ev.get("reward")
        name, sub = "", ""
        if isinstance(challenge, dict):           # "To Do": a mission + objective
            name = str(challenge.get("title") or "").strip()
            sub = str(challenge.get("description") or "").strip()
        elif isinstance(upgrade, dict):           # "Override": a buff + effect
            name = str(upgrade.get("title") or "").strip()
            sub = str(upgrade.get("description") or "").strip()
        elif reward:                              # "Big Prize!" / "Reward": an item
            name = str(reward).strip()
        if etype:
            lines.append((etype, True))
        if name:
            lines.append((name, False))
        if sub:
            lines.append((sub, False))
    return [(t if len(t) <= 30 else t[:29] + "…", h) for t, h in lines]


def _parse_local(key: str, spec: dict) -> "CycleState":
    """Build the state for a clock-only item (the daily / weekly reset timers).
    No phase label - the card shows just the title and the countdown."""
    now = datetime.now(timezone.utc)
    if spec["kind"] == "reset_weekly":
        expiry, icon = _next_weekly_reset(now), _WEEKLY_ICON
    else:
        expiry, icon = _next_daily_reset(now), _DAILY_ICON
    return CycleState(key, spec["place"], "", icon, expiry, ok=True)


def _next_daily_reset(now: datetime) -> datetime:
    """Warframe's daily reset is 00:00 UTC. Returns the next one strictly in
    the future."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(days=1)


def _next_weekly_reset(now: datetime) -> datetime:
    """Warframe's weekly reset (Archon Hunt, weekly challenges, etc.) is Monday
    00:00 UTC. Returns the next one strictly in the future."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_ahead = (0 - now.weekday()) % 7  # Monday == 0
    nxt = midnight + timedelta(days=days_ahead)
    if nxt <= now:  # today is Monday but the reset already passed
        nxt += timedelta(days=7)
    return nxt


def _parse_expiry(value) -> datetime | None:
    if not value:
        return None
    try:
        # Python 3.11+ fromisoformat accepts the trailing 'Z'.
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_remaining(expiry: datetime | None) -> str:
    """'2d 05h' / '1h 04m' / '7m 12s' / '—' countdown until `expiry`, computed
    locally so the HUD can tick every second without re-hitting the network. The
    day tier kicks in for the weekly items (Archon Hunt, resets) which can be
    several days out; the cycle cards stay in the h/m and m/s tiers as before."""
    if expiry is None:
        return "—"
    secs = int((expiry - datetime.now(timezone.utc)).total_seconds())
    secs = max(0, secs)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, s = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {mins:02d}m"
    return f"{mins}m {s:02d}s"
