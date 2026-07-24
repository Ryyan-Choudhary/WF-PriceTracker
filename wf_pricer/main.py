"""WF-PriceTracker entry point.

Run with `python -m wf_pricer.main` (or via run.py / run.pyw at the repo
root). Opens an app window plus a tray icon. F10 toggles scan mode;
Ctrl+F10 quits. What scanning actually does depends on the Selection Mode
picked in the window:

  - Multi-Select (default) - drag a box around any number of items;
    releasing the drag captures that whole region, OCRs it for every item
    inside, and labels each one in place with its name and price.
  - Grid Scan - calibrate a fixed slot grid once, then the scan hotkey OCRs
    every slot's name band and prices the lot.
  - Relic Reward - on the Void Fissure reward screen, the scan hotkey reads
    the reward names, prices them, and stars the most valuable.
"""
from __future__ import annotations

import ctypes
import datetime
import logging
import threading
from logging.handlers import RotatingFileHandler

import pystray

from . import config, gui, items_db, market, pipeline, scan
from . import tray as tray_mod

log = logging.getLogger(__name__)


def _set_dpi_aware() -> None:
    """Marks this process as per-monitor DPI aware.

    Without this, a process that hasn't declared its DPI awareness gets its
    coordinates silently virtualized/scaled by Windows - GetSystemMetrics,
    pynput's reported cursor position, and Tkinter's own window geometry
    would all agree with EACH OTHER, but not necessarily with a DPI-aware
    fullscreen application's own idea of where the cursor is. Warframe (like
    most modern games) is DPI-aware, so without this call our overlays could
    end up systematically offset from the real cursor position specifically
    while it's focused, even though everything looks consistent when tested
    against the desktop alone. Must be called before any window is created.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE (Windows 8.1+)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # fallback (Vista+)
        except (AttributeError, OSError):
            log.warning("Could not set process DPI awareness", exc_info=True)


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(config.LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)


class App:
    def __init__(self) -> None:
        self.icon: pystray.Icon | None = None
        self.window: gui.AppWindow | None = None
        self.items_index = None
        self.scan_active = False
        self.scan_count = 0
        self.selection_mode = config.SELECTION_MODE
        self.hotkeys: scan.HotkeyListener | None = None
        # While the user is capturing a new hotkey, the global listener is
        # still live; this flag makes its callbacks no-op so the very keys
        # being bound don't also fire the action they're being bound to.
        self._capturing_hotkey = False
        self._catalog_refresh_lock = threading.Lock()

    def load_items(self) -> None:
        try:
            self.items_index = items_db.load_items_index()
            if self.window:
                self.window.log(f"Loaded {len(self.items_index)} items from warframe.market.")
                self.window.set_search_catalog(self.items_index.search_entries())
        except Exception:
            log.exception("Could not load warframe.market item catalog; matching is disabled")
            if self.window:
                self.window.log("ERROR: failed to load the item catalog (check your internet connection).")

    def refresh_catalog(self) -> None:
        if not self._catalog_refresh_lock.acquire(blocking=False):
            if self.window:
                self.window.log("Already refreshing the item catalog - hang on.")
            return
        if self.window:
            self.window.log("Refreshing item catalog from warframe.market (bypassing the 3-day cache)...")
        threading.Thread(target=self._refresh_catalog_worker, daemon=True).start()

    def _refresh_catalog_worker(self) -> None:
        try:
            self.items_index = items_db.load_items_index(force_refresh=True)
        except Exception:
            log.exception("Failed to refresh item catalog")
            if self.window:
                self.window.log("ERROR: failed to refresh item catalog - check your internet connection.")
            return
        finally:
            self._catalog_refresh_lock.release()
        if self.window:
            self.window.log(f"Refreshed: {len(self.items_index)} matchable items (Sets are always excluded).")
            self.window.set_search_catalog(self.items_index.search_entries())

    # --- hotkey / button callbacks --------------------------------------
    def on_toggle_scan(self) -> None:
        if self._capturing_hotkey:
            return
        self.scan_active = not self.scan_active
        self._refresh_icon()
        if self.scan_active:
            self._start_mode_listener()
        else:
            self._stop_mode_listener()
        if not self.window:
            return
        self.window.set_scan_active(self.scan_active)
        if self.scan_active:
            if self.selection_mode == "grid":
                self.window.log("--- Scan mode ON: open your inventory, press the scan hotkey ---")
            elif self.selection_mode == "relic":
                self.window.log("--- Scan mode ON: open the reward screen, press the scan hotkey ---")
            else:  # multi
                self.window.log("--- Scan mode ON: drag a box around any items to scan them ---")
        else:
            self.window.log("--- Scan mode OFF ---")

    def on_scan(self) -> None:
        if self._capturing_hotkey:
            return
        if not self.scan_active:
            if self.window:
                self.window.log("Not scanning - turn on Scan Mode first.")
            return
        if self.selection_mode == "grid":
            if config.GRID is None:
                if self.window:
                    self.window.log('Calibrate the grid first ("Calibrate Grid..." button).')
                return
            threading.Thread(target=self._grid_scan_worker, daemon=True).start()
            return
        if self.selection_mode == "relic":
            threading.Thread(target=self._relic_scan_worker, daemon=True).start()
            return
        # Multi-Select: arm a one-shot region pick, then scan it.
        self.start_multi_select()

    _MODE_NAMES = {
        "multi": "Multi-Select", "grid": "Grid Scan", "relic": "Relic Reward",
    }

    def set_selection_mode(self, mode: str) -> None:
        if mode == self.selection_mode:
            return
        was_active = self.scan_active
        if was_active:
            self._stop_mode_listener()
        config.save_selection_mode(mode)
        self.selection_mode = mode
        if self.window:
            self.window.log(f"Selection mode set to: {self._MODE_NAMES.get(mode, mode)}")
        if was_active:
            self._start_mode_listener()

    def calibrate_grid(self) -> None:
        if self.window is None:
            return
        self.window.start_grid_calibration(
            on_complete=self._on_grid_calibrated,
            on_cancel=lambda: self.window.log("Grid calibration cancelled."),
        )

    def _on_grid_calibrated(self, grid: dict) -> None:
        config.save_grid_calibration(grid)
        if self.window:
            self.window.set_grid_info_label(
                f"{grid['rows']}x{grid['cols']} slots, band {grid['band_w']}x{grid['band_h']}px"
            )
            self.window.log(
                f"Grid calibrated: {grid['rows']}x{grid['cols']} slots, "
                f"band {grid['band_w']}x{grid['band_h']}px."
            )
            if self.scan_active and self.selection_mode == "grid":
                self.window.show_grid_outline(pipeline.grid_slot_rects(grid))

    _ENGINE_NAMES = {
        "easyocr": "EasyOCR (accurate, slower)",
        "tesseract": "Tesseract (fast, local)",
    }

    def set_engine(self, engine_key: str) -> None:
        config.save_ocr_engine(engine_key)
        if self.window:
            self.window.log(f"OCR engine set to: {self._ENGINE_NAMES.get(engine_key, engine_key)}")

    def set_price_workers(self, workers: int) -> None:
        config.save_price_fetch_workers(workers)
        if self.window:
            self.window.log(f"Price fetch concurrency set to {config.PRICE_FETCH_WORKERS} thread(s).")

    def set_match_tolerance(self, cutoff: int) -> None:
        # Takes effect on the next scan - items_db reads this cutoff live.
        config.save_match_cutoff(cutoff)
        if self.window:
            self.window.log(f"Match cutoff set to {config.FUZZY_MATCH_SCORE_CUTOFF} (higher = stricter).")

    # --- manual item lookup (search box) --------------------------------
    def lookup_item(self, slug: str, name: str) -> None:
        if self.window:
            self.window.log(f"Looking up {name}...")
        threading.Thread(target=self._lookup_item_worker, args=(slug, name), daemon=True).start()

    def _lookup_item_worker(self, slug: str, name: str) -> None:
        try:
            stats = market.get_item_stats(slug)
        except Exception:
            log.exception("Item stats lookup failed for %s", slug)
            if self.window:
                self.window.show_item_stats(name, None)
            return
        if not self.window:
            return
        if not stats.has_data:
            self.window.show_item_stats(name, None)
            return
        def stat_rows(label: str, points) -> list[tuple[str, str]]:
            # One row per rank tier; ranked items get a row for the lowest and
            # highest rank on the book, unranked items just one plain row.
            rows = []
            for rk, price in points:
                suffix = f" (Rank {rk})" if rk is not None else ""
                rows.append((f"{label}{suffix}", f"{price} p"))
            return rows

        volume = "—" if stats.volume_48h is None else f"{stats.volume_48h:,} sold"
        lines = [
            *stat_rows("Lowest sell", stats.lowest_sell),
            *stat_rows("Highest sell", stats.highest_sell),
            *stat_rows("Highest buy", stats.highest_buy),
            ("48h volume", volume),
            ("Sellers online", str(stats.sellers_online)),
            ("Buyers online", str(stats.buyers_online)),
        ]
        self.window.show_item_stats(name, lines)

    def open_search(self) -> None:
        # Global search hotkey: bring the window forward and drop the cursor in
        # the search box, so you can look a price up without alt-tabbing.
        if self._capturing_hotkey:
            return
        if self.window:
            self.window.open_search()

    # --- hotkeys ---------------------------------------------------------
    _HK_ACTION_LABELS = {
        "toggle": "Toggle scan mode", "scan": "Scan now",
        "search": "Open search", "quit": "Quit",
    }

    def begin_hotkey_capture(self) -> None:
        # Suppress the global listener's actions until set_hotkey clears this,
        # so the combo being captured doesn't also trigger a scan/quit.
        self._capturing_hotkey = True

    def set_hotkey(self, action: str, hotkey: str | None) -> None:
        """Apply a rebind captured in the UI. `hotkey` is in pynput syntax, or
        None if the capture was cancelled. Validates, rejects collisions,
        persists, and restarts the global listener."""
        self._capturing_hotkey = False
        if not hotkey:
            return  # cancelled - nothing to change

        from pynput import keyboard

        try:
            keyboard.HotKey.parse(hotkey)
        except Exception:
            if self.window:
                self.window.log("Couldn't read that key combo - keeping the current binding.")
            return

        current = {
            "scan": config.HOTKEY_SCAN,
            "toggle": config.HOTKEY_TOGGLE_SCAN,
            "quit": config.HOTKEY_QUIT,
            "search": config.HOTKEY_SEARCH,
        }
        for other, existing in current.items():
            if other != action and existing == hotkey:
                if self.window:
                    self.window.log(
                        f"{gui.hotkey_label(hotkey)} is already bound to "
                        f"{self._HK_ACTION_LABELS[other]} - pick another key."
                    )
                return

        current[action] = hotkey
        config.save_hotkeys(
            scan=current["scan"], toggle=current["toggle"],
            quit_=current["quit"], search=current["search"],
        )
        if self.hotkeys is not None:
            try:
                self.hotkeys.restart()
            except Exception:
                log.exception("Failed to apply the new hotkey binding")
                if self.window:
                    self.window.log("Failed to apply the new hotkey - check data/logs/app.log.")
                return
        if self.window:
            self.window.set_hotkey_labels({k: gui.hotkey_label(v) for k, v in current.items()})
            self.window.log(f"{self._HK_ACTION_LABELS[action]} rebound to {gui.hotkey_label(hotkey)}.")

    def on_quit(self) -> None:
        if self._capturing_hotkey:
            return
        self._stop_mode_listener()
        if self.window:
            self.window.log("Quitting...")
        if self.icon:
            self.icon.stop()
        if self.window:
            self.window.destroy()

    def show_window(self) -> None:
        if self.window:
            self.window.show()

    # --- internals -------------------------------------------------------
    def _refresh_icon(self) -> None:
        if self.icon:
            self.icon.icon = tray_mod.make_icon_image(self.scan_active)

    # --- mode-aware start/stop: dispatches to whichever selection mode is
    # currently active whenever scan mode is toggled on/off, or the mode
    # itself is switched while scan mode is already on. ---------------------
    def _start_mode_listener(self) -> None:
        # Multi-Select and Relic are both triggered on demand by the scan
        # hotkey, so there's nothing persistent to arm and the mouse stays
        # yours until you ask for a scan. Grid just shows its calibrated
        # outline so alignment can be confirmed before scanning.
        if self.selection_mode == "grid":
            if self.window and config.GRID is not None:
                self.window.show_grid_outline(pipeline.grid_slot_rects(config.GRID))

    def _stop_mode_listener(self) -> None:
        if self.window:
            self.window.hide_grid_outline()

    def start_multi_select(self) -> None:
        """Arm ONE Multi-Select region pick. The overlay captures the mouse for
        the duration, so the drag can't select anything in the game, and it
        disarms itself as soon as you release."""
        if self.window is None:
            return
        self.window.clear_multi_results()
        self.window.log("Drag a box around the items to scan (Esc to cancel)...")
        self.window.start_region_select(
            on_complete=self._on_region_selected,
            on_cancel=lambda: self.window.log("Selection cancelled."),
        )

    def _on_region_selected(self, left: int, top: int, right: int, bottom: int) -> None:
        threading.Thread(
            target=self._multi_scan_worker, args=(left, top, right, bottom), daemon=True
        ).start()

    def _capture(self, capture_fn):
        """Run a screen grab with our own windows/overlays hidden, so we never
        OCR our result labels, grid outline, or app window on top of the game.
        """
        if self.window is None:
            return capture_fn()
        return self.window.capture_hidden(capture_fn)

    def _multi_scan_worker(self, left: int, top: int, right: int, bottom: int) -> None:
        if self.items_index is None:
            if self.window:
                self.window.log("Still loading item catalog - try again in a moment.")
            return

        try:
            region = self._capture(lambda: scan.grab_region(left, top, right, bottom))
        except Exception:
            log.exception("Multi-select screen capture failed")
            return

        if self.window:
            self.window.log(f"--- Scanning region ({region.width}x{region.height}px)... ---")

        found = 0

        def on_match(match: pipeline.RegionMatch) -> None:
            nonlocal found
            found += 1
            self.scan_count += 1
            approx = "~" if match.price.used_fallback else ""
            price_line = (
                f"{approx}{match.price.avg_platinum:g}p avg "
                f"(lowest {match.price.lowest_platinum}p, n={match.price.sample_size})"
            )
            if self.window:
                screen_x = left + match.bbox[0]
                screen_y = top + match.bbox[1]
                self.window.add_multi_result_label(screen_x, screen_y, match.name, price_line)
                self.window.log(f"[{self.scan_count}] {match.name}: {price_line}")
            self._append_scan_log(match.name, price_line, "(multi-select)")

        try:
            pipeline.price_region(region, self.items_index, on_match=on_match)
        except Exception:
            log.exception("Multi-select OCR/pricing failed")
            if self.window:
                self.window.log("Region scan failed - check data/logs/app.log")
            return

        if self.window:
            if found == 0:
                self.window.log("No items recognized in that region.")
            else:
                self.window.log(f"--- Done: {found} item(s) found in region ---")

    def _grid_scan_worker(self) -> None:
        grid = config.GRID
        if grid is None:
            return
        if self.items_index is None:
            if self.window:
                self.window.log("Still loading item catalog - try again in a moment.")
            return

        # Bounding rect over all slot name bands = the region to capture.
        rects = pipeline.grid_slot_rects(grid)
        left = min(x for x, y, w, h in rects)
        top = min(y for x, y, w, h in rects)
        right = max(x + w for x, y, w, h in rects)
        bottom = max(y + h for x, y, w, h in rects)

        if self.window:
            self.window.clear_multi_results()
            self.window.log(
                f"--- Grid scan: {grid['rows']}x{grid['cols']} slots, "
                f"{config.GRID_SCAN_FRAMES} frame(s)... ---"
            )

        try:
            frames = self._capture(lambda: scan.capture_frames(
                left, top, right, bottom, config.GRID_SCAN_FRAMES, config.GRID_SCAN_FRAME_DELAY_S
            ))
        except Exception:
            log.exception("Grid scan capture failed")
            return
        finally:
            # capture_hidden withdrew the grid outline; bring it back for the
            # next scan while scan mode stays on.
            if self.window and self.scan_active and self.selection_mode == "grid":
                self.window.show_grid_outline(rects)

        found = 0

        def on_match(match: pipeline.RegionMatch) -> None:
            nonlocal found
            found += 1
            self.scan_count += 1
            approx = "~" if match.price.used_fallback else ""
            price_line = (
                f"{approx}{match.price.avg_platinum:g}p avg "
                f"(lowest {match.price.lowest_platinum}p, n={match.price.sample_size})"
            )
            if self.window:
                screen_x = left + match.bbox[0]
                screen_y = top + match.bbox[1]
                self.window.add_multi_result_label(screen_x, screen_y, match.name, price_line)
                self.window.log(f"[{self.scan_count}] {match.name}: {price_line}")
            self._append_scan_log(match.name, price_line, "(grid)")

        unreadable = 0

        def on_unreadable(bbox, best_text: str) -> None:
            nonlocal unreadable
            unreadable += 1
            if self.window:
                self.window.add_multi_result_label(
                    left + bbox[0], top + bbox[1], "Unreadable", f"OCR saw: {best_text[:40]}"
                )
                self.window.log(f"[slot] UNREADABLE - best OCR read: {best_text!r}")

        try:
            pipeline.price_grid(
                frames, grid, (left, top), self.items_index,
                on_match=on_match, on_unreadable=on_unreadable,
            )
        except Exception:
            log.exception("Grid scan OCR/pricing failed")
            if self.window:
                self.window.log("Grid scan failed - check data/logs/app.log")
            return

        if self.window:
            total = grid["rows"] * grid["cols"]
            suffix = f", {unreadable} unreadable" if unreadable else ""
            if found == 0:
                self.window.log(
                    f"No items recognized{suffix} - check grid calibration / open the inventory first."
                )
            else:
                self.window.log(f"--- Done: {found}/{total} slots identified{suffix} ---")

    def _relic_scan_worker(self) -> None:
        if self.items_index is None:
            if self.window:
                self.window.log("Still loading item catalog - try again in a moment.")
            return

        # A calibrated reward area wins; otherwise derive it from WFInfo's
        # reward-screen geometry scaled to the primary monitor (the fissure
        # reward screen renders on the game's monitor - calibrate if it's not
        # the primary, or your UI scale is unusual).
        if config.RELIC_REGION is not None:
            r = config.RELIC_REGION
            left, top, right, bottom = r["left"], r["top"], r["right"], r["bottom"]
        else:
            sw, sh = scan.primary_screen_size()
            left, top, right, bottom = pipeline.relic_reward_rect(sw, sh, config.RELIC_UI_SCALE)

        if self.window:
            self.window.clear_multi_results()
            self.window.log(
                f"--- Relic scan: reading reward names ({right - left}x{bottom - top}px band)... ---"
            )

        try:
            band = self._capture(lambda: scan.grab_region(left, top, right, bottom))
        except Exception:
            log.exception("Relic scan capture failed")
            return

        try:
            matches = pipeline.price_relic(band, self.items_index)
        except Exception:
            log.exception("Relic scan OCR/pricing failed")
            if self.window:
                self.window.log("Relic scan failed - check data/logs/app.log")
            return

        if not matches:
            if self.window:
                self.window.log(
                    "No rewards recognized - is the reward-selection screen open? "
                    "If the band is misaligned, calibrate the reward area or set your UI scale."
                )
            return

        # Highlight the most valuable reward with a star, once every price is in.
        best = max(matches, key=lambda m: m.price.avg_platinum)
        for match in matches:
            self.scan_count += 1
            approx = "~" if match.price.used_fallback else ""
            price_line = (
                f"{approx}{match.price.avg_platinum:g}p avg "
                f"(lowest {match.price.lowest_platinum}p, n={match.price.sample_size})"
            )
            name = f"★ {match.name}" if match is best else match.name
            if self.window:
                self.window.add_multi_result_label(left + match.bbox[0], top + match.bbox[1], name, price_line)
                self.window.log(f"[{self.scan_count}] {name}: {price_line}")
            self._append_scan_log(name, price_line, "(relic)")

        if self.window:
            self.window.log(
                f"--- Relic: {len(matches)} reward(s); best value: "
                f"{best.name} (~{best.price.avg_platinum:g}p) ---"
            )

    # --- relic reward-area calibration / settings -----------------------
    def set_relic_ui_scale(self, scale: float) -> None:
        config.save_relic_ui_scale(scale)
        if self.window:
            self.window.log(f"Relic UI scale set to {config.RELIC_UI_SCALE:g}.")

    def calibrate_relic_region(self) -> None:
        if self.window is None:
            return
        self.window.log("Drag a box around the reward-name band on the reward screen (Esc to cancel)...")
        self.window.start_region_select(
            on_complete=self._on_relic_region_selected,
            on_cancel=lambda: self.window.log("Reward-area calibration cancelled."),
        )

    def _on_relic_region_selected(self, left: int, top: int, right: int, bottom: int) -> None:
        config.save_relic_region(left, top, right, bottom)
        if self.window:
            self.window.set_relic_info_label(f"Custom: {right - left}x{bottom - top}px")
            self.window.log(f"Reward area set to {right - left}x{bottom - top}px (custom).")

    def clear_relic_region(self) -> None:
        config.clear_relic_region()
        if self.window:
            self.window.set_relic_info_label("Auto (from screen size)")
            self.window.log("Reward area reset to auto (derived from your screen size).")

    # --- text colour filter ---------------------------------------------
    def set_color_filter(self, enabled: bool, rgb: tuple[int, int, int], tolerance: int) -> None:
        config.save_color_filter(enabled, rgb, tolerance)
        if self.window:
            state = "on" if config.TEXT_COLOR_FILTER_ENABLED else "off"
            self.window.log(
                f"Text colour filter {state} "
                f"(rgb={config.TEXT_COLOR_RGB}, tolerance={config.TEXT_COLOR_TOLERANCE})."
            )

    def _append_scan_log(self, name: str, price_line: str, raw_display: str) -> None:
        timestamp = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            with open(config.SCAN_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{timestamp}  {name}: {price_line}  [OCR: {raw_display}]\n")
        except OSError:
            log.warning("Could not write to scan log file", exc_info=True)

    def build_tray(self) -> pystray.Icon:
        # Labels are callables so they re-read the (rebindable) hotkeys each
        # time the menu opens, instead of freezing the defaults at startup.
        menu = pystray.Menu(
            pystray.MenuItem("Show window", lambda: self.show_window(), default=True),
            pystray.MenuItem(
                lambda _i: f"Toggle scan mode ({gui.hotkey_label(config.HOTKEY_TOGGLE_SCAN)})",
                lambda: self.on_toggle_scan(),
            ),
            pystray.MenuItem(
                lambda _i: f"Scan now ({gui.hotkey_label(config.HOTKEY_SCAN)})",
                lambda: self.on_scan(),
            ),
            pystray.MenuItem(
                lambda _i: f"Quit ({gui.hotkey_label(config.HOTKEY_QUIT)})",
                lambda: self.on_quit(),
            ),
        )
        self.icon = pystray.Icon("wf_pricer", tray_mod.make_icon_image(False), "WF-PriceTracker", menu)
        return self.icon


def main() -> None:
    _set_dpi_aware()
    _setup_logging()
    log.info("Starting WF-PriceTracker")

    app = App()

    window = gui.AppWindow(
        on_toggle_scan=app.on_toggle_scan,
        on_scan_now=app.on_scan,
        on_refresh_catalog=app.refresh_catalog,
        on_engine_change=app.set_engine,
        on_selection_mode_change=app.set_selection_mode,
        on_calibrate_grid=app.calibrate_grid,
        on_price_workers_change=app.set_price_workers,
        on_match_tolerance_change=app.set_match_tolerance,
        on_set_hotkey=app.set_hotkey,
        on_hotkey_capture_start=app.begin_hotkey_capture,
        on_lookup_item=app.lookup_item,
        on_relic_ui_scale_change=app.set_relic_ui_scale,
        on_calibrate_relic=app.calibrate_relic_region,
        on_clear_relic_region=app.clear_relic_region,
        on_color_filter_change=app.set_color_filter,
        on_quit=app.on_quit,
    )
    app.window = window
    window.set_engine_selection(config.OCR_ENGINE)
    window.set_selection_mode_selection(config.SELECTION_MODE)
    window.set_price_workers(config.PRICE_FETCH_WORKERS)
    window.set_match_tolerance(config.FUZZY_MATCH_SCORE_CUTOFF)
    window.set_relic_ui_scale(config.RELIC_UI_SCALE)
    window.set_relic_info_label(
        "Auto (from screen size)" if config.RELIC_REGION is None
        else f"Custom: {config.RELIC_REGION['right'] - config.RELIC_REGION['left']}x"
             f"{config.RELIC_REGION['bottom'] - config.RELIC_REGION['top']}px"
    )
    window.set_color_filter_state(
        config.TEXT_COLOR_FILTER_ENABLED, config.TEXT_COLOR_RGB, config.TEXT_COLOR_TOLERANCE
    )

    if config.GRID is not None:
        window.set_grid_info_label(
            f"{config.GRID['rows']}x{config.GRID['cols']} slots, "
            f"band {config.GRID['band_w']}x{config.GRID['band_h']}px"
        )
    window.log("WF-PriceTracker ready. Loading item catalog...")
    if config.OCR_ENGINE == "easyocr":
        window.log("(First EasyOCR run will download its model weights - needs internet, one-time.)")

    hotkeys = scan.HotkeyListener(
        on_scan=app.on_scan, on_toggle_scan=app.on_toggle_scan,
        on_quit=app.on_quit, on_search=app.open_search,
    )
    app.hotkeys = hotkeys  # so rebinds can restart it
    hotkeys.start()

    icon = app.build_tray()
    icon.run_detached()  # tray runs on its own thread; Tk owns the main thread

    # Loading the catalog does a network call, so keep it off the Tk thread.
    threading.Thread(target=app.load_items, daemon=True).start()

    try:
        window.run()  # blocks until Quit
    finally:
        hotkeys.stop()
        if app.icon:
            app.icon.stop()


if __name__ == "__main__":
    main()
