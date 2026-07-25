# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/Ryyan-Choudhary/WF-PriceTracker/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Ryyan-Choudhary/WF-PriceTracker/releases/tag/v1.0.0
