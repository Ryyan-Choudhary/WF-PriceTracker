"""WF-PriceTracker entry point.

Run with `python -m wf_pricer.main` (or via run.py / run.pyw at the repo
root). Opens an app window plus a tray icon. F10 toggles scan mode;
Ctrl+F10 quits. What scanning actually does depends on the Selection Mode
picked in the window:

  - Single Item (default) - hover an item, press F9. Grabs a box centered
    on the cursor (size set once via "Set Item Box Size..."), OCRs it,
    matches it against warframe.market, and shows the price in a popup
    next to the cursor.
  - Multi-Select - drag a box around any number of items; releasing the
    drag captures that whole region, OCRs it for every item inside, and
    labels each one in place with its name and price.
"""
from __future__ import annotations

import ctypes
import datetime
import logging
import threading
from logging.handlers import RotatingFileHandler

import pystray

from . import config, gui, items_db, pipeline, scan
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
        self.cursor_tracker: scan.CursorTracker | None = None
        self.drag_select_watcher: scan.DragSelectWatcher | None = None
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
            if self.selection_mode == "multi":
                self.window.log("--- Scan mode ON: drag a box around any items to scan them ---")
            elif self.selection_mode == "grid":
                self.window.log("--- Scan mode ON: open your inventory, press the scan hotkey ---")
            else:
                self.window.log("--- Scan mode ON: hover an item, press the scan hotkey ---")
        else:
            self.window.log("--- Scan mode OFF ---")

    def on_scan(self) -> None:
        if self._capturing_hotkey:
            return
        if self.selection_mode == "multi":
            if self.window:
                self.window.log("The scan hotkey is for Single / Grid mode - Multi-Select scans on click-drag instead.")
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
        if config.BOX_WIDTH_PX is None or config.BOX_HEIGHT_PX is None:
            if self.window:
                self.window.log('Set your item box size first ("Set Item Box Size..." button).')
            return
        cx, cy = scan.get_cursor_position()
        threading.Thread(target=self._scan_worker, args=(cx, cy), daemon=True).start()

    _MODE_NAMES = {"single": "Single Item", "multi": "Multi-Select", "grid": "Grid Scan"}

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
        "claude_vision": "Claude Vision (in development)",
        "gemini_vision": "Gemini Vision (in development)",
    }

    def set_engine(self, engine_key: str) -> None:
        config.save_ocr_engine(engine_key)
        if self.window:
            self.window.log(f"OCR engine set to: {self._ENGINE_NAMES.get(engine_key, engine_key)}")

    def set_anthropic_key(self, key: str) -> None:
        config.save_anthropic_api_key(key)
        if self.window:
            self.window.log("Anthropic API key saved (data/cache/anthropic_api_key.json, gitignored).")

    def set_google_key(self, key: str) -> None:
        config.save_google_api_key(key)
        if self.window:
            self.window.log("Google API key saved (data/cache/google_api_key.json, gitignored).")

    def set_price_workers(self, workers: int) -> None:
        config.save_price_fetch_workers(workers)
        if self.window:
            self.window.log(f"Price fetch concurrency set to {config.PRICE_FETCH_WORKERS} thread(s).")

    # --- hotkeys ---------------------------------------------------------
    _HK_ACTION_LABELS = {"toggle": "Toggle scan mode", "scan": "Scan now", "quit": "Quit"}

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
        config.save_hotkeys(scan=current["scan"], toggle=current["toggle"], quit_=current["quit"])
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

    def set_box_size(self) -> None:
        if self.window is None:
            return
        self.window.log("Drag a box around one item's icon+name on screen to set the box size...")
        self.window.start_box_calibration(
            on_complete=self._on_box_calibrated,
            on_cancel=lambda: self.window.log("Box size calibration cancelled (drag was too small)."),
        )

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

    def _on_box_calibrated(self, width: int, height: int) -> None:
        config.save_box_calibration(width, height)
        if self.window:
            self.window.set_box_size_label(f"Box size: {width}x{height}px")
            self.window.log(f"Item box size set to {width}x{height}px.")
        if self.scan_active and self.selection_mode == "single":
            self._start_mode_listener()  # refresh the outline with the new size

    # --- mode-aware start/stop: dispatches to whichever selection mode is
    # currently active whenever scan mode is toggled on/off, or the mode
    # itself is switched while scan mode is already on. ---------------------
    def _start_mode_listener(self) -> None:
        if self.selection_mode == "multi":
            self._start_drag_select()
        elif self.selection_mode == "grid":
            # Grid mode is triggered by F9 (already globally bound); just show
            # the calibrated grid outline so the user can confirm alignment.
            if self.window and config.GRID is not None:
                self.window.show_grid_outline(pipeline.grid_slot_rects(config.GRID))
        else:
            self._start_cursor_box()

    def _stop_mode_listener(self) -> None:
        self._stop_cursor_box()
        self._stop_drag_select()
        if self.window:
            self.window.hide_grid_outline()

    def _start_cursor_box(self) -> None:
        if config.BOX_WIDTH_PX is None or config.BOX_HEIGHT_PX is None:
            return  # nothing to show yet; on_scan() will prompt to set it
        if self.cursor_tracker is None:
            self.cursor_tracker = scan.CursorTracker(on_move=self._on_cursor_move)
            self.cursor_tracker.start()
        if self.window:
            cx, cy = scan.get_cursor_position()
            self.window.show_cursor_box(config.BOX_WIDTH_PX, config.BOX_HEIGHT_PX, cx, cy)

    def _stop_cursor_box(self) -> None:
        if self.cursor_tracker is not None:
            self.cursor_tracker.stop()
            self.cursor_tracker = None
        if self.window:
            self.window.hide_cursor_box()

    def _on_cursor_move(self, x: int, y: int) -> None:
        if self.window:
            self.window.update_cursor_box_position(x, y)

    def _reshow_cursor_box(self) -> None:
        """Re-draw the cursor target box after a scan's capture withdrew it.
        No-ops unless single-scan mode is live and the box is calibrated."""
        if (
            self.window is None
            or not self.scan_active
            or self.selection_mode != "single"
            or config.BOX_WIDTH_PX is None
            or config.BOX_HEIGHT_PX is None
        ):
            return
        cx, cy = scan.get_cursor_position()
        self.window.show_cursor_box(config.BOX_WIDTH_PX, config.BOX_HEIGHT_PX, cx, cy)

    def _start_drag_select(self) -> None:
        if self.drag_select_watcher is None:
            self.drag_select_watcher = scan.DragSelectWatcher(
                on_drag_start=self._on_drag_select_start,
                on_drag_update=self._on_drag_select_update,
                on_drag_end=self._on_drag_select_end,
            )
            self.drag_select_watcher.start()

    def _stop_drag_select(self) -> None:
        if self.drag_select_watcher is not None:
            self.drag_select_watcher.stop()
            self.drag_select_watcher = None

    def _on_drag_select_start(self, x: int, y: int) -> None:
        if self.window:
            self.window.clear_multi_results()
            self.window.show_drag_select_box(x, y)

    def _on_drag_select_update(self, x: int, y: int) -> None:
        if self.window:
            self.window.update_drag_select_box(x, y)

    def _on_drag_select_end(self, left: int, top: int, right: int, bottom: int) -> None:
        if self.window:
            self.window.hide_drag_select_box()
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

    def _scan_worker(self, cx: int, cy: int) -> None:
        # Clear the previous scan's result popup up front, so the old name
        # vanishes the instant you scan again (and can't be re-captured into
        # this grab if it happened to overlap the box).
        if self.window:
            self.window.clear_lookup_result()
        try:
            crop = self._capture(lambda: scan.grab_box_at(cx, cy, config.BOX_WIDTH_PX, config.BOX_HEIGHT_PX))
        except Exception:
            log.exception("Scan screen capture failed")
            return
        finally:
            # capture_hidden withdraws the cursor box for the grab and never
            # restores it (its owner does); bring it back so the target
            # outline keeps following the mouse after every scan.
            self._reshow_cursor_box()

        if self.items_index is None:
            if self.window:
                self.window.show_lookup_result(
                    cx, cy, ["Still loading item catalog - try again in a moment."]
                )
            return

        try:
            result, raw_texts = pipeline.price_crop(crop, self.items_index)
        except Exception:
            log.exception("Scan OCR/pricing failed")
            if self.window:
                self.window.show_lookup_result(cx, cy, ["Scan failed - check data/logs/app.log"])
            return

        self.scan_count += 1
        # Included in every log line so a wrong or missing match is
        # actually diagnosable later - what did the OCR engine really see?
        raw_display = " | ".join(raw_texts) if raw_texts else "(no text detected)"

        if result is None:
            if self.window:
                self.window.show_lookup_result(cx, cy, ["No item recognized in that box."])
                self.window.log(f"[{self.scan_count}] No match. OCR saw: {raw_display}")
            return

        approx = "~" if result.price.used_fallback else ""
        price_line = (
            f"{approx}{result.price.avg_platinum:g}p avg "
            f"(lowest {result.price.lowest_platinum}p, n={result.price.sample_size})"
        )
        if self.window:
            self.window.show_lookup_result(cx, cy, [result.name, price_line])
            self.window.log(f"[{self.scan_count}] {result.name}: {price_line}  (OCR saw: {raw_display})")
        self._append_scan_log(result.name, price_line, raw_display)

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
        on_set_box_size=app.set_box_size,
        on_refresh_catalog=app.refresh_catalog,
        on_engine_change=app.set_engine,
        on_set_anthropic_key=app.set_anthropic_key,
        on_set_google_key=app.set_google_key,
        on_selection_mode_change=app.set_selection_mode,
        on_calibrate_grid=app.calibrate_grid,
        on_price_workers_change=app.set_price_workers,
        on_set_hotkey=app.set_hotkey,
        on_hotkey_capture_start=app.begin_hotkey_capture,
        on_quit=app.on_quit,
    )
    app.window = window
    window.set_engine_selection(config.OCR_ENGINE)
    window.set_selection_mode_selection(config.SELECTION_MODE)
    window.set_price_workers(config.PRICE_FETCH_WORKERS)

    if config.BOX_WIDTH_PX is not None and config.BOX_HEIGHT_PX is not None:
        window.set_box_size_label(f"Box size: {config.BOX_WIDTH_PX}x{config.BOX_HEIGHT_PX}px")
    else:
        window.log('Set your item box size first ("Set Item Box Size..." button) before scanning.')
    if config.GRID is not None:
        window.set_grid_info_label(
            f"{config.GRID['rows']}x{config.GRID['cols']} slots, "
            f"band {config.GRID['band_w']}x{config.GRID['band_h']}px"
        )
    window.log("WF-PriceTracker ready. Loading item catalog...")
    if config.OCR_ENGINE == "easyocr":
        window.log("(First EasyOCR run will download its model weights - needs internet, one-time.)")

    hotkeys = scan.HotkeyListener(on_scan=app.on_scan, on_toggle_scan=app.on_toggle_scan, on_quit=app.on_quit)
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
