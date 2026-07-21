# WF-PriceTracker

An attempt to make pricing your warframe inventory easier.

A Windows app (with a tray icon too) for pricing your Warframe inventory.
Turn on capture mode, flip through your inventory in-game hitting a hotkey
to screenshot each screen, turn capture mode back off, and it OCRs every
screenshot, matches what it read against the real item catalog on
[warframe.market](https://warframe.market), and hands you back your
screenshots with each item's current average sell price stamped on top.

## How it works

A window opens when you launch the app, showing status, a live log, and
buttons for everything. There's also a tray icon (cyan diamond = idle, red =
capture mode on) — closing the window with the X just hides it there;
left-click the tray icon or use its "Show window" menu item to bring it
back. Quit fully via the Quit button, the tray menu, or `Ctrl+F10`.

1. **Capture** — `F10` (or the "Start Capture" button) toggles capture mode
   on. While it's on, `F9` (or "Capture Now") takes a screenshot of your
   primary monitor and saves it to `data/captures/<session>/`. Pressing F9
   repeatedly, back-to-back, is fine — grabbing the frame happens instantly
   and encoding/writing the PNG to disk happens in the background, so it
   won't make you wait between shots.
2. **Stop & process** — `F10` again turns capture mode off and kicks off
   processing in the background (the window's log shows progress per
   screenshot as it goes):
   - **OCR** (local Tesseract) reads every line of text in each screenshot.
   - **Matching** fuzzy-matches each line of text against warframe.market's
     full item list (fetched from their API and cached locally), which also
     conveniently filters out UI chrome like "INVENTORY" or "SORT BY" that
     doesn't resemble a real item name.
   - **Pricing** pulls current live sell orders for each matched item from
     warframe.market and averages the cheapest few listings from
     online/in-game sellers (falling back to all listings if nobody's
     online).
   - **Annotation** draws a price label next to where each item's name was
     found, and saves the result to `data/output/<session>/`, which opens
     automatically when it's done.
3. A `summary.txt` is also written into that output folder with every
   matched item, how many times it was seen, and a rough total.

## Setup

You need Python 3.11+ and the Tesseract OCR engine (a separate, non-Python
program that does the actual text recognition).

```powershell
# 1. Install the Tesseract OCR engine (one-time, needs to happen outside pip)
winget install --id UB-Mannheim.TesseractOCR -e

# 2. Create a virtual environment and install the Python dependencies
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

This repo's `.venv` was already set up and Tesseract already installed by
the assistant that scaffolded this project — the two commands above are
here for reference / re-setup on another machine.

## Running it

```powershell
# with a console window, so you can see what's happening (recommended the first time)
.venv\Scripts\python.exe run.py

# silent, no console window (double-click WF-PriceTracker.bat to do the same)
.venv\Scripts\pythonw.exe run.pyw
```

The tray icon appears once the app is ready. Logs always go to
`data/logs/app.log` regardless of which launcher you use, so if something
seems to silently fail, check there first.

## Usage tips

- **Run Warframe in Borderless Window mode**, not exclusive fullscreen.
  Exclusive fullscreen can prevent both the global hotkey and the
  screenshot grab from working reliably; borderless window doesn't have
  that problem and looks identical.
- If hotkeys don't respond while Warframe has focus, try running
  WF-PriceTracker as Administrator — some games/launchers run elevated,
  which blocks keyboard hooks from non-elevated processes.
- OCR works best on screens where item names are legible as actual text
  (Mods screen, Relics list, Prime Parts list, etc.). Pure icon-grids with
  no visible name text won't match anything — that's a fundamental
  limitation of the OCR approach, not a bug.
- Quantities aren't reliably detected — the summary counts how many times
  an item's name was *seen* across your screenshots, not necessarily how
  many you own, so treat totals as a rough estimate rather than exact
  inventory value.
- The item catalog is cached for 3 days and prices for 10 minutes
  (`data/cache/`), so re-running shortly after won't hit the API again for
  the same items. Delete `data/cache/` to force a refresh.

## Project layout

```
wf_pricer/
  config.py     settings: hotkeys, folders, thresholds, API base URL
  capture.py    screenshot capture (async save) + global hotkey binding
  ocr.py         image preprocessing + Tesseract text/line extraction
  items_db.py   warframe.market item catalog fetch/cache + fuzzy matching
  market.py     warframe.market order fetch/cache + price averaging
  annotate.py   draws price labels onto a screenshot
  pipeline.py   wires OCR -> matching -> pricing -> annotation together
  gui.py        the app window (status, buttons, live log)
  tray.py       tray icon image
  main.py       app entry point (window + tray + hotkeys wiring)
run.py / run.pyw  launchers (console / silent)
data/           captures, output, cache, logs (all gitignored)
```
