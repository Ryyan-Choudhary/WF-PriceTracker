# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] - 2026-07-27

### Fixed
- **Tracking HUD countdowns no longer stick at `0:00`.** The refresh now wakes
  just as the soonest card lapses, so a flipped cycle or a daily/weekly reset
  rolls straight over to its next value instead of sitting at zero for up to a
  minute.
- **The Tracking HUD is far more resilient to warframestat.us hiccups.** It now
  pulls the whole worldstate in **one** request per refresh instead of one per
  field, so a flaky upstream can no longer leave some cards loaded and others
  blank. Transient gateway errors (the Cloudflare **521** "web server is down",
  timeouts) are retried a couple of times, and a genuine failure is logged as a
  single concise line rather than a stack trace per field. When a fetch fails,
  the Tracking status now pings warframestat's `/heartbeat` to say *whose*
  problem it is — **"feed down"** (warframestat up, game data unavailable) vs
  **"API unreachable"** (warframestat itself down / your network). The clock-based
  reset timers keep working regardless, since they need no network.

### Added
- **Tracking HUD: four new cards** — the weekly **Archon Hunt** (current boss,
  its **Archon Shard** drop, and countdown), the **1999 Calendar** (active
  season plus **today's events, each labelled by type — To Do / Override /
  Big Prize! — with its name and objective**), and **Daily** / **Weekly reset**
  timers. Tick them in the Tracking tab like any other cycle.
  The reset timers are computed locally from Warframe's fixed UTC schedule
  (daily 00:00, weekly Monday 00:00), so they work with no network. Countdowns
  now show a day tier (e.g. `2d 05h`) for the multi-day items, and cards grow
  extra lines to fit the shard / calendar detail.

### Changed
- **Fass / Vome** (Deimos) now use a custom-drawn icon — an oblong, snake-like
  orb with spider legs — instead of a plain coloured circle (Fass orange, Vome
  blue, as before).
- Each scan mode now has its **own** rebindable hotkey — Multi-Select (default
  **F9**), Grid (default **F5**), and Relic (default **F4**) — each firing that
  mode directly regardless of the active tab, replacing the single generic scan
  key. An existing rebind of the old scan key is migrated to Multi-Select.

### Docs
- README overhauled with real screenshots (main window, Grid Scan, Relic Reward,
  quick-search, item stats, the Tracking HUD and tab, Settings) and refreshed
  copy: added the Relic Reward and Tracking sections, dropped the removed
  Single-Item mode and the never-shipped cloud-OCR references, and fixed the tab
  list and hotkey defaults.

## [1.0.0] - 2026-07-26

### Added
- **Tracking tab** — a live world-state HUD (Cetus & Earth day/night, Orb Vallis
  warm/cold, Deimos Fass/Vome, Duviri Spiral) pulled from the community Warframe
  Status API, shown as a click-through overlay on a hotkey (default **F6**), with
  a drag-to-position **Edit Layout** mode. Icons are standard emoji.
- **Clear-overlays hotkey** (default **F7**) to wipe on-screen labels at any time.
- On-screen **colour eyedropper** for the text-colour filter (freeze screen,
  magnify, click a pixel).
- Packaging: `pyproject.toml` with a `wf-pricer` console entry point and
  `python -m wf_pricer` support.

### Changed
- **Project restructure** to a `src/` layout, with tests under `tests/` and
  screenshots under `assets/`.
- Runtime data (cache, logs) now lives in a per-user data directory
  (`%LOCALAPPDATA%\WF-PriceTracker` on Windows) instead of inside the repo;
  settings are migrated from an older in-repo `data/` folder on first run.
- The scan hotkey now acts on demand — the separate scan-mode on/off toggle was
  removed. The grid outline is flashed briefly after calibration instead of
  staying on screen.

### Removed
- Single-item scan mode.
- Claude (Anthropic) and Gemini (Google) Vision OCR engines and their API keys.
- PaddleOCR engine (too heavy). OCR is EasyOCR or Tesseract.

[Unreleased]: https://github.com/Ryyan-Choudhary/WF-PriceTracker/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/Ryyan-Choudhary/WF-PriceTracker/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Ryyan-Choudhary/WF-PriceTracker/releases/tag/v1.0.0
