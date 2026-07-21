"""WF-PriceTracker entry point.

Run with `python -m wf_pricer.main` (or via run.py / run.pyw at the repo
root). Opens an app window plus a tray icon:

  - F9  captures one screenshot (only while capture mode is on)
  - F10 toggles capture mode; turning it OFF processes everything captured
        this session and drops annotated, priced images in data/output/
  - Ctrl+F10 quits

The window mirrors all of these as buttons for when hotkeys aren't
convenient, and shows a live log of what's happening. Closing the window
just hides it to the tray (left-click the tray icon, or its "Show window"
menu item, to bring it back) - use Quit (button, tray menu, or Ctrl+F10) to
actually exit.
"""
from __future__ import annotations

import logging
import os
import threading
from logging.handlers import RotatingFileHandler

import pystray

from . import capture, config, gui, items_db, pipeline
from . import tray as tray_mod

log = logging.getLogger(__name__)


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
        self.session = capture.CaptureSession()
        self.icon: pystray.Icon | None = None
        self.window: gui.AppWindow | None = None
        self.items_index = None
        self._processing_lock = threading.Lock()

    def load_items(self) -> None:
        try:
            self.items_index = items_db.load_items_index()
            if self.window:
                self.window.log(f"Loaded {len(self.items_index)} items from warframe.market.")
        except Exception:
            log.exception("Could not load warframe.market item catalog; matching is disabled")
            if self.window:
                self.window.log("ERROR: failed to load the item catalog (check your internet connection).")

    # --- hotkey / button callbacks --------------------------------------
    def on_capture(self) -> None:
        if not self.session.active:
            if self.window:
                self.window.log("Not capturing - press F10 (or the Start Capture button) first.")
            return
        n = self.session.capture_one()
        if n and self.window:
            self.window.log(f"Captured screenshot #{n}")
            self.window.set_count(f"{n} screenshot(s)")

    def on_toggle(self) -> None:
        if self.session.active:
            self._stop_and_process()
        else:
            session_id = self.session.start()
            self._refresh_icon()
            if self.window:
                self.window.set_status("Capture mode ON")
                self.window.set_toggle_label("Stop && Process (F10)")
                self.window.set_count("0 screenshot(s)")
                self.window.log(f"--- Capture session {session_id} started: F9 to snap, F10 to stop ---")

    def on_quit(self) -> None:
        if self.window:
            self.window.log("Quitting...")
        if self.icon:
            self.icon.stop()
        if self.window:
            self.window.destroy()

    def open_output_folder(self) -> None:
        try:
            os.startfile(config.OUTPUT_DIR)
        except OSError:
            log.warning("Could not open output folder", exc_info=True)
            if self.window:
                self.window.log("Could not open the output folder.")

    def show_window(self) -> None:
        if self.window:
            self.window.show()

    # --- internals -------------------------------------------------------
    def _refresh_icon(self) -> None:
        if self.icon:
            self.icon.icon = tray_mod.make_icon_image(self.session.active)

    def _stop_and_process(self) -> None:
        self.session.stop()
        self._refresh_icon()
        count = self.session.count
        session_dir = self.session.session_dir
        session_id = self.session.session_id

        if self.window:
            self.window.set_status("Idle")
            self.window.set_toggle_label("Start Capture (F10)")

        if count == 0:
            if self.window:
                self.window.log("Capture mode off. No screenshots were taken.")
            return

        if self.window:
            self.window.set_status(f"Processing {count} screenshot(s)...")
            self.window.log(f"--- Processing {count} screenshot(s) ---")
        threading.Thread(
            target=self._process_worker, args=(session_dir, session_id, count), daemon=True
        ).start()

    def _process_worker(self, session_dir, session_id, count) -> None:
        with self._processing_lock:
            # Make sure every screenshot's background PNG write has actually
            # finished before OCR tries to read the files.
            self.session.wait_for_pending_saves()

            if self.items_index is None:
                self.load_items()
            if self.items_index is None:
                if self.window:
                    self.window.log("Aborting: item catalog unavailable.")
                    self.window.set_status("Idle")
                return

            output_dir = config.OUTPUT_DIR / session_id
            progress = self.window.log if self.window else (lambda _msg: None)
            try:
                result = pipeline.process_session(session_dir, output_dir, self.items_index, on_progress=progress)
            except Exception:
                log.exception("Processing failed")
                if self.window:
                    self.window.log("ERROR: processing failed - check data/logs/app.log")
                    self.window.set_status("Idle")
                return

            unique_items = len({m.slug for m in result.matches})
            if self.window:
                self.window.log(
                    f"--- Done! {unique_items} unique item(s) priced across {count} screenshot(s) ---"
                )
                self.window.set_status("Idle")
                self.window.set_count("")
            try:
                os.startfile(output_dir)
            except OSError:
                log.warning("Could not auto-open output folder %s", output_dir)

    def build_tray(self) -> pystray.Icon:
        menu = pystray.Menu(
            pystray.MenuItem("Show window", lambda: self.show_window(), default=True),
            pystray.MenuItem("Toggle capture mode (F10)", lambda: self.on_toggle()),
            pystray.MenuItem("Capture screenshot now (F9)", lambda: self.on_capture()),
            pystray.MenuItem("Open output folder", lambda: self.open_output_folder()),
            pystray.MenuItem("Quit (Ctrl+F10)", lambda: self.on_quit()),
        )
        self.icon = pystray.Icon("wf_pricer", tray_mod.make_icon_image(False), "WF-PriceTracker", menu)
        return self.icon


def main() -> None:
    _setup_logging()
    log.info("Starting WF-PriceTracker")

    if config.TESSERACT_PATH is None:
        log.warning(
            "Tesseract OCR engine not found on this machine. Install it from "
            "https://github.com/UB-Mannheim/tesseract/wiki (or `winget install UB-Mannheim.TesseractOCR`) "
            "or OCR will fail on every screenshot."
        )

    app = App()

    window = gui.AppWindow(
        on_toggle_capture=app.on_toggle,
        on_capture_now=app.on_capture,
        on_open_output=app.open_output_folder,
        on_quit=app.on_quit,
    )
    app.window = window
    if config.TESSERACT_PATH is None:
        window.log("WARNING: Tesseract OCR was not found - install it or OCR will fail on every screenshot.")
    window.log("WF-PriceTracker ready. Loading item catalog...")

    hotkeys = capture.HotkeyListener(on_capture=app.on_capture, on_toggle=app.on_toggle, on_quit=app.on_quit)
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
