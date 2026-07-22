"""The visible app window.

Tkinter isn't thread-safe, so every widget mutation has to happen on the Tk
thread. Other threads (the pynput hotkey thread, the background scan-worker
thread) only ever call the thread-safe methods here (log/set_status/etc),
which either push onto a queue.Queue the Tk mainloop polls, or schedule a
callback via root.after(0, ...).
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable

from pynput import mouse

from . import config
from .scan import virtual_screen_rect


class AppWindow:
    _ENGINE_OPTIONS = [
        ("easyocr", "EasyOCR (accurate, slower)"),
        ("tesseract", "Tesseract (fast, local)"),
        ("claude_vision", "Claude Vision (in development)"),
        ("gemini_vision", "Gemini Vision (in development)"),
    ]

    def __init__(
        self,
        on_toggle_scan: Callable[[], None],
        on_scan_now: Callable[[], None],
        on_set_box_size: Callable[[], None],
        on_refresh_catalog: Callable[[], None],
        on_engine_change: Callable[[str], None],
        on_set_anthropic_key: Callable[[str], None],
        on_set_google_key: Callable[[str], None],
        on_selection_mode_change: Callable[[str], None],
        on_calibrate_grid: Callable[[], None],
        on_price_workers_change: Callable[[int], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._log_queue: "queue.Queue[str]" = queue.Queue()

        self.root = tk.Tk()
        self.root.title("WF-PriceTracker")
        self.root.geometry("470x560")
        self.root.minsize(380, 420)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.status_var = tk.StringVar(value="Idle")
        self.box_size_var = tk.StringVar(value="Box size: not set")
        self.topmost_var = tk.BooleanVar(value=False)
        self.engine_var = tk.StringVar()
        self.selection_mode_var = tk.StringVar(value="single")
        self.price_workers_var = tk.IntVar(value=1)
        self.price_workers_label_var = tk.StringVar(value="")
        self._current_engine_key = "tesseract"

        self._on_toggle_scan = on_toggle_scan
        self._on_scan_now = on_scan_now
        self._on_set_box_size = on_set_box_size
        self._on_refresh_catalog = on_refresh_catalog
        self._on_engine_change = on_engine_change
        self._on_set_anthropic_key = on_set_anthropic_key
        self._on_set_google_key = on_set_google_key
        self._on_selection_mode_change = on_selection_mode_change
        self._on_calibrate_grid = on_calibrate_grid
        self._on_price_workers_change = on_price_workers_change
        self._on_quit = on_quit

        self._build_widgets()
        self._poll_queue()

        self._snip_overlay = SnipOverlay(self.root)
        self._calibrator: BoxSizeCalibrator | None = None
        self._grid_calibrator: GridCalibrator | None = None
        self._cursor_box_overlay = CursorBoxOverlay(self.root)
        self._multi_result_overlay = MultiResultOverlay(self.root)
        self._grid_outline_overlay = GridOutlineOverlay(self.root)

    def _build_widgets(self) -> None:
        pad = {"padx": 10, "pady": 6}

        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill="x", **pad)
        ttk.Label(status_frame, textvariable=self.status_var, font=("Segoe UI", 14, "bold")).pack(side="left")
        ttk.Label(status_frame, textvariable=self.box_size_var, font=("Segoe UI", 10)).pack(side="right")

        mode_frame = ttk.Frame(self.root)
        mode_frame.pack(fill="x", **pad)
        ttk.Label(mode_frame, text="Selection Mode:").pack(side="left")
        ttk.Radiobutton(
            mode_frame, text="Single Item", value="single", variable=self.selection_mode_var,
            command=self._on_selection_mode_selected,
        ).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(
            mode_frame, text="Multi-Select", value="multi", variable=self.selection_mode_var,
            command=self._on_selection_mode_selected,
        ).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(
            mode_frame, text="Grid Scan", value="grid", variable=self.selection_mode_var,
            command=self._on_selection_mode_selected,
        ).pack(side="left", padx=(6, 0))

        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", **pad)
        self.toggle_btn = ttk.Button(btn_frame, text="Start Scan Mode (F10)", command=self._on_toggle_scan)
        self.toggle_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.scan_now_btn = ttk.Button(btn_frame, text="Scan Now (F9)", command=self._on_scan_now)
        self.scan_now_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))

        setup_frame = ttk.Frame(self.root)
        setup_frame.pack(fill="x", **pad)
        ttk.Button(setup_frame, text="Set Item Box Size...", command=self._on_set_box_size).pack(
            side="left", expand=True, fill="x", padx=(0, 4)
        )
        ttk.Button(setup_frame, text="Refresh Item List", command=self._on_refresh_catalog).pack(
            side="left", expand=True, fill="x", padx=(4, 0)
        )

        grid_frame = ttk.Frame(self.root)
        grid_frame.pack(fill="x", padx=10, pady=(0, 6))
        self.calibrate_grid_btn = ttk.Button(
            grid_frame, text="Calibrate Grid... (for Grid Scan)", command=self._on_calibrate_grid
        )
        self.calibrate_grid_btn.pack(fill="x")

        engine_frame = ttk.Frame(self.root)
        engine_frame.pack(fill="x", **pad)
        ttk.Label(engine_frame, text="OCR Engine:").pack(side="left")
        self.engine_combo = ttk.Combobox(
            engine_frame,
            textvariable=self.engine_var,
            values=[label for _key, label in self._ENGINE_OPTIONS],
            state="readonly",
        )
        self.engine_combo.pack(side="left", expand=True, fill="x", padx=(6, 0))
        self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine_selected)

        key_frame = ttk.Frame(self.root)
        key_frame.pack(fill="x", padx=10, pady=(0, 6))
        # Disabled for now - Claude/Gemini Vision are still in development
        # (see config.DISABLED_ENGINES). Re-enabling these is just removing
        # state="disabled" once those engines are ready.
        self.anthropic_key_btn = ttk.Button(
            key_frame, text="Set Anthropic Key...", command=self._prompt_anthropic_key, state="disabled"
        )
        self.anthropic_key_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.google_key_btn = ttk.Button(
            key_frame, text="Set Google Key...", command=self._prompt_google_key, state="disabled"
        )
        self.google_key_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))

        speed_frame = ttk.Frame(self.root)
        speed_frame.pack(fill="x", padx=10, pady=(0, 2))
        ttk.Label(speed_frame, textvariable=self.price_workers_label_var).pack(side="left")
        self.price_workers_scale = ttk.Scale(
            speed_frame,
            from_=config.PRICE_FETCH_WORKERS_MIN,
            to=config.PRICE_FETCH_WORKERS_MAX,
            orient="horizontal",
            command=self._on_price_workers_scale,
        )
        self.price_workers_scale.pack(side="left", expand=True, fill="x", padx=(6, 0))
        ttk.Label(
            self.root,
            text="Higher = faster scans, but warframe.market may rate-limit your IP above ~3.",
            foreground="gray", font=("Segoe UI", 8),
        ).pack(fill="x", padx=10, pady=(0, 6))

        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.log_box = tk.Listbox(log_frame, font=("Consolas", 9), activestyle="none")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scrollbar.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", **pad)
        ttk.Checkbutton(
            bottom, text="Always on top", variable=self.topmost_var, command=self._apply_topmost
        ).pack(side="left")
        ttk.Button(bottom, text="Quit", command=self._on_quit).pack(side="right")
        self.hint_var = tk.StringVar(value="F10: toggle scan mode   F9: scan at cursor   Ctrl+F10: quit")
        ttk.Label(bottom, textvariable=self.hint_var, foreground="gray").pack(side="right", padx=8)

    def _apply_topmost(self) -> None:
        self.root.attributes("-topmost", self.topmost_var.get())

    def _on_selection_mode_selected(self) -> None:
        mode = self.selection_mode_var.get()
        self._apply_mode_ui(mode)
        self._on_selection_mode_change(mode)

    def set_selection_mode_selection(self, mode: str) -> None:
        """Reflects the persisted selection mode at startup without firing
        the on_selection_mode_change callback. Thread-safe.
        """

        def _set() -> None:
            self.selection_mode_var.set(mode)
            self._apply_mode_ui(mode)

        self.call_soon(_set)

    def _apply_mode_ui(self, mode: str) -> None:
        # F9 ("Scan Now") is meaningful in Single (scan at cursor) and Grid
        # (scan the whole grid), but not in Multi (there you drag instead).
        if mode == "multi":
            self.scan_now_btn.state(["disabled"])
            self.hint_var.set("F10: toggle scan mode   drag to select & scan   Ctrl+F10: quit")
        elif mode == "grid":
            self.scan_now_btn.state(["!disabled"])
            self.hint_var.set("F10: toggle scan mode   F9: scan grid   Ctrl+F10: quit")
        else:  # single
            self.scan_now_btn.state(["!disabled"])
            self.hint_var.set("F10: toggle scan mode   F9: scan at cursor   Ctrl+F10: quit")

    def _on_price_workers_scale(self, raw: str) -> None:
        # ttk.Scale is continuous; snap to an int and only fire the persist
        # callback when the integer value actually changes (not on every
        # sub-pixel of a drag).
        value = int(round(float(raw)))
        self._update_price_workers_label(value)
        if value != self.price_workers_var.get():
            self.price_workers_var.set(value)
            self._on_price_workers_change(value)

    def _update_price_workers_label(self, value: int) -> None:
        note = " (safe)" if value == 1 else (" (polite)" if value <= 3 else " (may rate-limit)")
        self.price_workers_label_var.set(f"Price threads: {value}{note}")

    def set_price_workers(self, value: int) -> None:
        """Reflects the persisted concurrency at startup without firing the
        change callback. Thread-safe."""

        def _set() -> None:
            self.price_workers_var.set(value)
            self.price_workers_scale.set(value)
            self._update_price_workers_label(value)

        self.call_soon(_set)

    def _on_engine_selected(self, _event: object = None) -> None:
        label = self.engine_var.get()
        for key, lbl in self._ENGINE_OPTIONS:
            if lbl != label:
                continue
            if key in config.DISABLED_ENGINES:
                # Snap the dropdown back to whatever was actually active -
                # ttk.Combobox has no per-item disabled state, so this is
                # the only way to make an entry genuinely unpickable while
                # still showing it (labeled "in development") for context.
                self.set_engine_selection(self._current_engine_key)
                self.log(f"{lbl} isn't selectable yet - still in development.")
                return
            self._current_engine_key = key
            self._on_engine_change(key)
            return

    def _prompt_anthropic_key(self) -> None:
        key = self._ask_api_key(
            "Anthropic API key",
            "Paste your Anthropic API key (used only for the Claude Vision "
            "OCR engine). Saved locally to data/cache/anthropic_api_key.json, "
            "which is gitignored - never put a real key in a tracked file.",
        )
        if key:
            self._on_set_anthropic_key(key)

    def _prompt_google_key(self) -> None:
        key = self._ask_api_key(
            "Google AI Studio API key",
            "Paste your Google AI Studio API key (used only for the Gemini "
            "Vision OCR engine). Saved locally to "
            "data/cache/google_api_key.json, which is gitignored - never put "
            "a real key in a tracked file.",
        )
        if key:
            self._on_set_google_key(key)

    def _ask_api_key(self, title: str, prompt: str) -> str | None:
        # Local import: simpledialog is rarely needed elsewhere, and this
        # keeps the module's top-level imports lean.
        from tkinter import simpledialog

        key = simpledialog.askstring(title, prompt, show="*", parent=self.root)
        return key.strip() if key else None

    def set_engine_selection(self, engine_key: str) -> None:
        """Reflects the current engine in the dropdown without firing the
        on_engine_change callback (used at startup to show the persisted
        choice). Thread-safe.
        """

        def _set() -> None:
            for key, label in self._ENGINE_OPTIONS:
                if key == engine_key:
                    self.engine_var.set(label)
                    self._current_engine_key = engine_key
                    return

        self.call_soon(_set)

    def _on_close(self) -> None:
        # Closing the window just hides it to the tray; the tray icon (or
        # Ctrl+F10) is how you actually quit.
        self.root.withdraw()

    # --- thread-safe API used by hotkey / worker threads --------------
    def log(self, message: str) -> None:
        self._log_queue.put(message)

    def call_soon(self, func: Callable[[], None]) -> None:
        self.root.after(0, func)

    def set_status(self, text: str) -> None:
        self.call_soon(lambda: self.status_var.set(text))

    def set_box_size_label(self, text: str) -> None:
        self.call_soon(lambda: self.box_size_var.set(text))

    def set_toggle_label(self, text: str) -> None:
        self.call_soon(lambda: self.toggle_btn.config(text=text))

    def show(self) -> None:
        def _show() -> None:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()

        self.call_soon(_show)

    def show_lookup_result(self, x: int, y: int, lines: list[str]) -> None:
        self.call_soon(lambda: ResultPopup(self.root, x, y, lines))

    def start_box_calibration(
        self,
        on_complete: Callable[[int, int], None],
        on_cancel: Callable[[], None],
    ) -> None:
        """Starts a one-shot drag-to-measure session: shows a live overlay,
        listens for exactly one drag anywhere on screen, then reports the
        box size (or cancels on too small a drag). Must be called from the
        Tk thread (it's wired to a button).
        """
        if self._calibrator is not None:
            return  # already calibrating
        self._calibrator = BoxSizeCalibrator(
            self.root,
            self._snip_overlay,
            on_complete=lambda w, h: (self._clear_calibrator(), on_complete(w, h)),
            on_cancel=lambda: (self._clear_calibrator(), on_cancel()),
        )
        self._calibrator.start()

    def _clear_calibrator(self) -> None:
        self._calibrator = None

    def start_grid_calibration(
        self,
        on_complete: Callable[[dict], None],
        on_cancel: Callable[[], None],
    ) -> None:
        """Runs the two-drag grid calibration (box the first slot's name, then
        the last slot's name), prompts for rows/cols, computes the grid dict,
        and calls on_complete(grid). Must be called from the Tk thread.
        """
        if self._grid_calibrator is not None:
            return
        self._grid_calibrator = GridCalibrator(
            self.root,
            self._snip_overlay,
            log=self.log,
            on_finish=self._grid_calibration_finished,
        )
        self._grid_pending = (on_complete, on_cancel)
        self._grid_calibrator.start()

    def _grid_calibration_finished(self, rects: list[tuple[int, int, int, int]] | None) -> None:
        on_complete, on_cancel = self._grid_pending
        self._grid_calibrator = None
        if rects is None:
            on_cancel()
            return
        from tkinter import simpledialog

        first, last = rects
        rows = simpledialog.askinteger("Grid rows", "How many rows in the grid?", parent=self.root, minvalue=1, maxvalue=50)
        if not rows:
            on_cancel()
            return
        cols = simpledialog.askinteger("Grid columns", "How many columns in the grid?", parent=self.root, minvalue=1, maxvalue=50)
        if not cols:
            on_cancel()
            return
        grid = {
            "first_x": first[0], "first_y": first[1],
            "band_w": first[2], "band_h": first[3],
            "col_pitch": (last[0] - first[0]) / max(cols - 1, 1),
            "row_pitch": (last[1] - first[1]) / max(rows - 1, 1),
            "rows": rows, "cols": cols,
        }
        on_complete(grid)

    # --- grid outline preview (fixed rects shown while Grid Scan mode is on)
    def show_grid_outline(self, rects: list[tuple[int, int, int, int]]) -> None:
        self.call_soon(lambda: self._grid_outline_overlay.show(rects))

    def hide_grid_outline(self) -> None:
        self.call_soon(lambda: self._grid_outline_overlay.hide())

    # --- hide ourselves during a screen grab -----------------------------
    # Our result labels, grid outline, cursor box and even the main window
    # sit on top of the game; if they overlap the scanned area they get
    # captured and OCR'd as garbage (worst of all, result labels drawn over
    # item names corrupt the very names on a re-scan). So the scan worker
    # calls capture_hidden(...) which withdraws everything for the duration
    # of the grab, then restores. Global hotkeys keep working meanwhile.
    def capture_hidden(self, capture_fn: Callable[[], object]) -> object:
        """Run capture_fn (a screen grab, on the CALLER's thread) with all our
        windows hidden. Marshals the hide/restore onto the Tk thread and
        blocks the caller until the hide has actually rendered."""
        hidden = threading.Event()
        self.call_soon(lambda: (self._hide_for_capture(), hidden.set()))
        hidden.wait(timeout=1.5)
        try:
            return capture_fn()
        finally:
            self.call_soon(self._restore_after_capture)

    def _hide_for_capture(self) -> None:
        self._was_main_visible = bool(self.root.winfo_viewable())
        for overlay in (
            self._snip_overlay, self._cursor_box_overlay,
            self._multi_result_overlay, self._grid_outline_overlay,
        ):
            overlay.withdraw()
        self.root.withdraw()
        self.root.update_idletasks()  # force the un-map to take effect before the grab

    def _restore_after_capture(self) -> None:
        if getattr(self, "_was_main_visible", True):
            self.root.deiconify()
        # Overlays are re-shown by their owners: grid outline via main after
        # a grid scan, result labels as on_match fires. Nothing to restore here.

    # --- cursor-following box outline shown while scan mode is on --------
    def show_cursor_box(self, box_w: int, box_h: int, x: int, y: int) -> None:
        self.call_soon(lambda: self._cursor_box_overlay.show(box_w, box_h, x, y))

    def update_cursor_box_position(self, x: int, y: int) -> None:
        self.call_soon(lambda: self._cursor_box_overlay.move_to(x, y))

    def hide_cursor_box(self) -> None:
        self.call_soon(lambda: self._cursor_box_overlay.hide())

    # --- multi-select drag rectangle (reuses the same overlay class the
    # box-size calibrator uses, just driven by DragSelectWatcher instead) --
    def show_drag_select_box(self, x: int, y: int) -> None:
        self.call_soon(lambda: self._snip_overlay.begin(x, y))

    def update_drag_select_box(self, x: int, y: int) -> None:
        self.call_soon(lambda: self._snip_overlay.update_to(x, y))

    def hide_drag_select_box(self) -> None:
        self.call_soon(lambda: self._snip_overlay.end())

    # --- multi-select results: name+price labels drawn in place over each
    # detected item, added incrementally as they're found/priced ----------
    def clear_multi_results(self) -> None:
        self.call_soon(lambda: self._multi_result_overlay.clear())

    def add_multi_result_label(self, x: int, y: int, name: str, price_text: str) -> None:
        self.call_soon(lambda: self._multi_result_overlay.add_label(x, y, name, price_text))

    # --- internals -------------------------------------------------------
    def _poll_queue(self) -> None:
        try:
            while True:
                message = self._log_queue.get_nowait()
                self.log_box.insert("end", message)
                self.log_box.yview_moveto(1.0)
                if self.log_box.size() > 500:
                    self.log_box.delete(0, 100)
        except queue.Empty:
            pass
        self.root.after(150, self._poll_queue)

    def run(self) -> None:
        self.root.mainloop()

    def destroy(self) -> None:
        # May be called from the hotkey thread or the tray icon's thread, so
        # marshal onto the Tk thread rather than touching self.root directly.
        self.call_soon(self._destroy_now)

    def _destroy_now(self) -> None:
        self.root.quit()
        self.root.destroy()


class SnipOverlay(tk.Toplevel):
    """A borderless, always-on-top window that draws a live selection
    rectangle while the user drags, spanning the whole virtual desktop so
    it works no matter which monitor the drag happens on. Ordinary
    corner-to-corner drag: the press point is one corner of the box, and it
    grows toward wherever the mouse is dragged, same as a normal
    click-and-drag selection.

    This window does NOT handle the mouse itself - input comes from a
    pynput mouse listener (see BoxSizeCalibrator), which is the only
    reliable way to track a drag that started over another window (e.g.
    Warframe). This is purely a rendering surface.
    """

    _TRANSPARENT_KEY = "#010203"  # arbitrary color used as the "invisible" background

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-transparentcolor", self._TRANSPARENT_KEY)
        except tk.TclError:
            # -transparentcolor is a Windows-only Tk feature; degrade to a
            # faint tint elsewhere rather than crashing.
            self.attributes("-alpha", 0.25)

        self._offset = (0, 0)
        self.canvas = tk.Canvas(self, bg=self._TRANSPARENT_KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self._rect_id: int | None = None
        self._start: tuple[int, int] | None = None

    def _refresh_geometry(self) -> None:
        # Re-measured every time a drag begins rather than cached once at
        # startup, in case the display configuration (e.g. a game switching
        # resolutions) changed since this overlay was created.
        left, top, width, height = virtual_screen_rect()
        self.geometry(f"{width}x{height}+{left}+{top}")
        self._offset = (left, top)

    def _to_canvas(self, x: int, y: int) -> tuple[int, int]:
        ox, oy = self._offset
        return (x - ox, y - oy)

    def begin(self, x: int, y: int) -> None:
        self._refresh_geometry()
        self._start = (x, y)
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
        cx, cy = self._to_canvas(x, y)
        self._rect_id = self.canvas.create_rectangle(cx, cy, cx, cy, outline="#4ddbea", width=2)
        self.deiconify()
        self.lift()

    def update_to(self, x: int, y: int) -> None:
        if self._start is None or self._rect_id is None:
            return
        cx0, cy0 = self._to_canvas(*self._start)
        cx1, cy1 = self._to_canvas(x, y)
        self.canvas.coords(self._rect_id, cx0, cy0, cx1, cy1)

    def end(self) -> None:
        self._start = None
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
            self._rect_id = None
        self.withdraw()


class CursorBoxOverlay(tk.Toplevel):
    """A borderless, always-on-top window that continuously shows a
    fixed-size box outline centered on the cursor while scan mode is on, so
    you can see exactly what will be grabbed before pressing the scan
    hotkey. Purely a rendering surface - actual cursor tracking comes from
    a pynput mouse listener (see scan.CursorTracker); this just gets told
    where to draw.
    """

    _TRANSPARENT_KEY = "#010203"

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-transparentcolor", self._TRANSPARENT_KEY)
        except tk.TclError:
            self.attributes("-alpha", 0.25)

        self._offset = (0, 0)
        self.canvas = tk.Canvas(self, bg=self._TRANSPARENT_KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self._rect_id: int | None = None
        self._box_w = 0
        self._box_h = 0

    def _refresh_geometry(self) -> None:
        left, top, width, height = virtual_screen_rect()
        self.geometry(f"{width}x{height}+{left}+{top}")
        self._offset = (left, top)

    def _to_canvas(self, x: float, y: float) -> tuple[float, float]:
        ox, oy = self._offset
        return (x - ox, y - oy)

    def show(self, box_w: int, box_h: int, x: int, y: int) -> None:
        self._refresh_geometry()
        self._box_w, self._box_h = box_w, box_h
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
        self._rect_id = self.canvas.create_rectangle(0, 0, 0, 0, outline="#4ddbea", width=2)
        self.move_to(x, y)
        self.deiconify()
        self.lift()

    def move_to(self, x: int, y: int) -> None:
        if self._rect_id is None:
            return
        half_w, half_h = self._box_w / 2, self._box_h / 2
        cx0, cy0 = self._to_canvas(x - half_w, y - half_h)
        cx1, cy1 = self._to_canvas(x + half_w, y + half_h)
        self.canvas.coords(self._rect_id, cx0, cy0, cx1, cy1)

    def hide(self) -> None:
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
            self._rect_id = None
        self.withdraw()


class MultiResultOverlay(tk.Toplevel):
    """A borderless, always-on-top window that draws name+price labels at
    multiple screen positions at once - used by multi-select scan mode to
    label every detected item in place, directly over the game, as each one
    is found and priced. clear() removes everything (called when a new
    multi-select drag starts) so results never pile up from an old scan.
    """

    _TRANSPARENT_KEY = "#010203"

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-transparentcolor", self._TRANSPARENT_KEY)
        except tk.TclError:
            self.attributes("-alpha", 0.25)

        self._offset = (0, 0)
        self.canvas = tk.Canvas(self, bg=self._TRANSPARENT_KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._item_ids: list[int] = []

    def _refresh_geometry(self) -> None:
        left, top, width, height = virtual_screen_rect()
        self.geometry(f"{width}x{height}+{left}+{top}")
        self._offset = (left, top)

    def _to_canvas(self, x: int, y: int) -> tuple[int, int]:
        ox, oy = self._offset
        return (x - ox, y - oy)

    def clear(self) -> None:
        for item_id in self._item_ids:
            self.canvas.delete(item_id)
        self._item_ids.clear()
        self.withdraw()

    def add_label(self, x: int, y: int, name: str, price_text: str) -> None:
        """Draws one name+price label with its top-left corner at screen
        position (x, y). Safe to call repeatedly without clearing first -
        each call adds a new label alongside whatever's already shown.
        """
        self._refresh_geometry()
        cx, cy = self._to_canvas(x, y)
        pad = 4

        name_id = self.canvas.create_text(
            cx + pad, cy + pad, text=name, anchor="nw", fill="#4ddbea", font=("Segoe UI", 9, "bold")
        )
        price_id = self.canvas.create_text(
            cx + pad, cy + pad + 14, text=price_text, anchor="nw", fill="#dddddd", font=("Segoe UI", 8)
        )
        name_box = self.canvas.bbox(name_id)
        price_box = self.canvas.bbox(price_id)
        right = max(name_box[2], price_box[2]) + pad
        bottom = price_box[3] + pad
        bg_id = self.canvas.create_rectangle(cx, cy, right, bottom, fill="#121214", outline="#4ddbea", width=1)
        self.canvas.tag_lower(bg_id, name_id)

        self._item_ids.extend([bg_id, name_id, price_id])
        self.deiconify()
        self.lift()


class BoxSizeCalibrator:
    """One-shot: temporarily listens for a single left-click-drag anywhere
    on screen (via its own short-lived pynput mouse listener - not a
    persistent global hook, it stops itself as soon as the drag ends) and
    reports the resulting box size. Used by the "Set Item Box Size..."
    button to measure one item's icon+name tile without needing pixel math.

    Ordinary corner-to-corner drag: press at one corner of the item (e.g.
    top-left), drag to the opposite corner, release. The measured box is
    whatever rectangle you actually dragged out - it does not get
    recentered or resized afterward. Where that box then gets *placed* on
    each scan (centered on the cursor) is a separate matter, handled by
    scan.grab_box_at.
    """

    MIN_DRAG_PX = 10

    def __init__(
        self,
        root: tk.Misc,
        overlay: SnipOverlay,
        on_complete: Callable[[int, int], None],
        on_cancel: Callable[[], None],
    ) -> None:
        self._root = root
        self._overlay = overlay
        self._on_complete = on_complete
        self._on_cancel = on_cancel
        self._start: tuple[int, int] | None = None
        self._listener: mouse.Listener | None = None

    def start(self) -> None:
        self._listener = mouse.Listener(on_click=self._on_click, on_move=self._on_move)
        self._listener.start()

    def _stop_listener(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def _on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        if button != mouse.Button.left:
            return
        if pressed:
            self._start = (x, y)
            self._root.after(0, lambda: self._overlay.begin(x, y))
            return

        start = self._start
        self._start = None
        self._stop_listener()
        self._root.after(0, self._overlay.end)
        if start is None:
            return
        x0, y0 = start
        width, height = abs(x - x0), abs(y - y0)
        if width < self.MIN_DRAG_PX or height < self.MIN_DRAG_PX:
            self._root.after(0, self._on_cancel)
        else:
            self._root.after(0, lambda: self._on_complete(width, height))

    def _on_move(self, x: int, y: int) -> None:
        if self._start is not None:
            self._root.after(0, lambda: self._overlay.update_to(x, y))


class GridCalibrator:
    """Two sequential one-shot drags for Grid Scan calibration: box the FIRST
    (top-left) slot's name text, then the LAST (bottom-right) slot's name
    text. Reports both full rects (x, y, w, h in screen coords). A single
    persistent listener handles both drags; it stops itself after the second.
    Calls on_finish([first_rect, last_rect]) on success, or on_finish(None)
    if either drag is too small (treated as cancel).
    """

    MIN_DRAG_PX = 8

    def __init__(
        self,
        root: tk.Misc,
        overlay: SnipOverlay,
        log: Callable[[str], None],
        on_finish: Callable[[list | None], None],
    ) -> None:
        self._root = root
        self._overlay = overlay
        self._log = log
        self._on_finish = on_finish
        self._start: tuple[int, int] | None = None
        self._rects: list[tuple[int, int, int, int]] = []
        self._listener: mouse.Listener | None = None

    def start(self) -> None:
        self._log("Grid calibration: drag a box around the FIRST (top-left) item's name text.")
        self._listener = mouse.Listener(on_click=self._on_click, on_move=self._on_move)
        self._listener.start()

    def _stop_listener(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def _on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        if button != mouse.Button.left:
            return
        if pressed:
            self._start = (x, y)
            self._root.after(0, lambda: self._overlay.begin(x, y))
            return

        start = self._start
        self._start = None
        self._root.after(0, self._overlay.end)
        if start is None:
            return
        x0, y0 = start
        w, h = abs(x - x0), abs(y - y0)
        if w < self.MIN_DRAG_PX or h < self.MIN_DRAG_PX:
            self._stop_listener()
            self._root.after(0, lambda: self._on_finish(None))
            return

        rect = (min(x0, x), min(y0, y), w, h)
        self._rects.append(rect)
        if len(self._rects) == 1:
            self._log("Now drag a box around the LAST (bottom-right) item's name text.")
        else:
            self._stop_listener()
            rects = self._rects
            self._root.after(0, lambda: self._on_finish(rects))

    def _on_move(self, x: int, y: int) -> None:
        if self._start is not None:
            self._root.after(0, lambda: self._overlay.update_to(x, y))


class GridOutlineOverlay(tk.Toplevel):
    """Draws a set of fixed rectangles (the calibrated grid's slot name bands)
    over the screen so the user can confirm the grid lines up before scanning.
    Shown while Grid Scan mode + scan mode are both on; purely a preview.
    """

    _TRANSPARENT_KEY = "#010203"

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-transparentcolor", self._TRANSPARENT_KEY)
        except tk.TclError:
            self.attributes("-alpha", 0.25)

        self._offset = (0, 0)
        self.canvas = tk.Canvas(self, bg=self._TRANSPARENT_KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._rect_ids: list[int] = []

    def _refresh_geometry(self) -> None:
        left, top, width, height = virtual_screen_rect()
        self.geometry(f"{width}x{height}+{left}+{top}")
        self._offset = (left, top)

    def show(self, rects: list[tuple[int, int, int, int]]) -> None:
        self._refresh_geometry()
        for rid in self._rect_ids:
            self.canvas.delete(rid)
        self._rect_ids.clear()
        ox, oy = self._offset
        for (x, y, w, h) in rects:
            rid = self.canvas.create_rectangle(
                x - ox, y - oy, x - ox + w, y - oy + h, outline="#4ddbea", width=1
            )
            self._rect_ids.append(rid)
        self.deiconify()
        self.lift()

    def hide(self) -> None:
        for rid in self._rect_ids:
            self.canvas.delete(rid)
        self._rect_ids.clear()
        self.withdraw()


class ResultPopup(tk.Toplevel):
    """A small, auto-dismissing box showing a single-item scan result near
    where the box was scanned. Click it (or wait) to dismiss.
    """

    def __init__(self, parent: tk.Misc, x: int, y: int, lines: list[str], duration_ms: int = 6000) -> None:
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.95)
        except tk.TclError:
            pass

        frame = tk.Frame(self, bg="#121214", highlightbackground="#4ddbea", highlightthickness=1)
        frame.pack()
        for i, line in enumerate(lines):
            tk.Label(
                frame,
                text=line,
                bg="#121214",
                fg="#4ddbea" if i == 0 else "#dddddd",
                font=("Segoe UI", 11, "bold") if i == 0 else ("Segoe UI", 9),
                justify="left",
                anchor="w",
            ).pack(fill="x", padx=10, pady=(8 if i == 0 else 0, 8 if i == len(lines) - 1 else 3))

        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        vleft, vtop, vwidth, vheight = virtual_screen_rect()
        px = min(max(x, vleft), vleft + vwidth - w)
        py = min(max(y, vtop), vtop + vheight - h)
        self.geometry(f"+{px}+{py}")

        self.bind("<Button-1>", lambda _e: self._safe_destroy())
        self.after(duration_ms, self._safe_destroy)

    def _safe_destroy(self) -> None:
        try:
            self.destroy()
        except tk.TclError:
            pass
