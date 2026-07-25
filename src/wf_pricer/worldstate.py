"""Live Warframe world-state cycles (day/night, warm/cold, Fass/Vome) for the
Display HUD, pulled from the community Warframe Status API (warframestat.us).

Only the handful of cycle fields the Display tab can show are fetched, each as
its own small field endpoint (/pc/<field>) rather than the whole worldstate
blob. Every cycle reports an `expiry` timestamp, so once fetched the countdown
is computed locally (see format_remaining) - the network is only needed to
learn the current phase and when it flips, not to tick the clock.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from . import config

log = logging.getLogger(__name__)


# The cycles the Display tab offers, in display order. `kind` drives how the
# phase + icon are derived from the API payload (see _parse). This registry is
# the one place to add another cycle later ("for the first part" - more can be
# bolted on here without touching the overlay).
DISPLAY_ITEMS: dict[str, dict] = {
    "cetus":   {"field": "cetusCycle",   "place": "Cetus",      "kind": "daynight"},
    "earth":   {"field": "earthCycle",   "place": "Earth",      "kind": "daynight"},
    "vallis":  {"field": "vallisCycle",  "place": "Orb Vallis", "kind": "warmcold"},
    "cambion": {"field": "cambionCycle", "place": "Deimos",     "kind": "fassvome"},
    "duviri":  {"field": "duviriCycle",  "place": "Duviri",     "kind": "duviri"},
}
DISPLAY_ITEM_ORDER: list[str] = ["cetus", "earth", "vallis", "cambion", "duviri"]

# Human label for a cycle kind (shown next to the checkbox).
KIND_LABELS = {
    "daynight": "Day / Night", "warmcold": "Warm / Cold",
    "fassvome": "Fass / Vome", "duviri": "Spiral",
}

# Per-kind mapping of the API's phase to (phase label, emoji icon). The icon is
# the actual emoji character, rendered by glyphs.icon_image().
_PHASES = {
    "daynight": {True: ("Day", "☀️"), False: ("Night", "\U0001F319")},   # ☀️ / 🌙
    "warmcold": {True: ("Warm", "\U0001F525"), False: ("Cold", "❄️")},   # 🔥 / ❄️
    "fassvome": {"fass": ("Fass", "\U0001F7E0"), "vome": ("Vome", "\U0001F535")},  # 🟠 / 🔵
}
# Duviri's Spiral rotates through moods; each maps to an expressive emoji.
_DUVIRI_MOODS = {
    "joy": ("Joy", "\U0001F604"),        # 😄
    "anger": ("Anger", "\U0001F620"),    # 😠
    "envy": ("Envy", "\U0001F612"),      # 😒
    "sorrow": ("Sorrow", "\U0001F622"),  # 😢
    "fear": ("Fear", "\U0001F628"),      # 😨
}
# Fallback icon when a cycle can't be read (offline / bad payload).
_UNKNOWN_ICON = {
    "daynight": "\U0001F319", "warmcold": "❄️",
    "fassvome": "\U0001F535", "duviri": "\U0001F300",  # 🌀
}


@dataclass
class CycleState:
    key: str
    place: str
    phase_label: str            # "Day" / "Night" / "Warm" / "Cold" / "Fass" / "Vome" / "—"
    icon: str                   # glyph key for glyphs.icon_image
    expiry: datetime | None     # when the current phase ends (UTC), or None if unknown
    ok: bool = True             # False if the fetch/parse failed


_session = requests.Session()
_session.headers.update({"User-Agent": "WF-PriceTracker (worldstate display)"})


def item_label(key: str) -> str:
    """'Cetus (Day / Night)' style label for the checklist."""
    spec = DISPLAY_ITEMS[key]
    return f"{spec['place']} ({KIND_LABELS.get(spec['kind'], spec['kind'])})"


def fetch_states(keys) -> dict[str, "CycleState"]:
    """Fetch the current CycleState for each requested display key. Network
    failures are contained per-cycle: a failed one comes back with ok=False and
    a placeholder rather than aborting the others."""
    out: dict[str, CycleState] = {}
    for key in keys:
        spec = DISPLAY_ITEMS.get(key)
        if spec is None:
            continue
        try:
            data = _get_field(spec["field"])
            out[key] = _parse(key, spec, data)
        except Exception:
            log.warning("World-state fetch failed for %s", key, exc_info=True)
            out[key] = CycleState(key, spec["place"], "—", _UNKNOWN_ICON[spec["kind"]], None, ok=False)
    return out


def _get_field(field: str) -> dict:
    url = f"{config.WORLDSTATE_API_BASE}/{config.WORLDSTATE_PLATFORM}/{field}"
    resp = _session.get(url, timeout=config.HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def _parse(key: str, spec: dict, data: dict) -> "CycleState":
    kind = spec["kind"]
    if kind == "daynight":
        label, icon = _PHASES[kind][bool(data.get("isDay"))]
    elif kind == "warmcold":
        label, icon = _PHASES[kind][bool(data.get("isWarm"))]
    elif kind == "duviri":
        mood = str(data.get("state", "")).lower()
        label, icon = _DUVIRI_MOODS.get(mood, (mood.title() or "—", _UNKNOWN_ICON[kind]))
    else:  # fassvome
        phase_key = str(data.get("state", "")).lower()
        label, icon = _PHASES[kind].get(phase_key, ("—", _UNKNOWN_ICON[kind]))
    return CycleState(key, spec["place"], label, icon, _parse_expiry(data.get("expiry")), ok=True)


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
    """'1h 04m' / '7m 12s' / '—' countdown until `expiry`, computed locally so
    the HUD can tick every second without re-hitting the network."""
    if expiry is None:
        return "—"
    secs = int((expiry - datetime.now(timezone.utc)).total_seconds())
    secs = max(0, secs)
    hours, rem = divmod(secs, 3600)
    mins, s = divmod(rem, 60)
    if hours:
        return f"{hours}h {mins:02d}m"
    return f"{mins}m {s:02d}s"
