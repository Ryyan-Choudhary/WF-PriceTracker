"""The visible app window.

Tkinter isn't thread-safe, so every widget mutation has to happen on the Tk
thread. Other threads (the pynput hotkey thread, the background scan-worker
thread) only ever call the thread-safe methods here (log/set_status/etc),
which either push onto a queue.Queue the Tk mainloop polls, or schedule a
callback via root.after(0, ...).
"""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable

from pynput import mouse

from . import config
from .scan import virtual_screen_rect

log = logging.getLogger(__name__)


# --- Palette ---------------------------------------------------------------
# One dark, low-contrast set of surfaces with a single bright accent, shared by
# the window AND the on-screen overlays so the labels drawn over the game read
# as part of the same app. Tk can't do rounded corners, shadows or gradients,
# so "soft" here comes from close-valued surfaces, hairline borders and
# generous padding rather than from bevels - nothing uses a 3D relief.
BG = "#12141a"          # window background
SURFACE = "#181b23"     # cards, log, panels
SURFACE_HI = "#20242e"  # inputs, hover states
BORDER = "#2a2f3a"      # hairline separators
TEXT = "#e6e9ef"
TEXT_DIM = "#8b93a7"    # secondary copy, hints
ACCENT = "#4ddbea"      # the one bright highlight (also the overlay outline)
ACCENT_DIM = "#2b7f8c"  # accent at rest / pressed
DANGER = "#ff5f6d"
OK = "#3ddc97"

FONT = "Segoe UI"
MONO = "Consolas"


class AppWindow:
    _ENGINE_OPTIONS = [
        ("easyocr", "EasyOCR (accurate, slower)"),
        ("tesseract", "Tesseract (fast, local)"),
        ("claude_vision", "Claude Vision (in development)"),
        ("gemini_vision", "Gemini Vision (in development)"),
    ]

    _MODE_TABS = [
        ("single", "Single"),
        ("multi", "Multi-Select"),
        ("grid", "Grid Scan"),
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
        # Default is tall enough to show the whole page without a scrollbar
        # (content is ~665px + the pinned footer); the scrollbar only appears
        # once the user shrinks below that. The content scrolls, so the floor
        # just has to keep the header, tabs and footer legible.
        self.root.geometry("520x760")
        self.root.minsize(360, 400)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.status_var = tk.StringVar(value="Idle")
        self.box_size_var = tk.StringVar(value="Not set")
        self.grid_info_var = tk.StringVar(value="Not calibrated")
        self.topmost_var = tk.BooleanVar(value=False)
        self.engine_var = tk.StringVar()
        self.selection_mode_var = tk.StringVar(value="single")
        self.price_workers_var = tk.IntVar(value=1)
        self.price_workers_label_var = tk.StringVar(value="")
        self._current_engine_key = "tesseract"
        self._tabs: dict[str, tuple[tk.Label, tk.Frame]] = {}
        self._panels: dict[str, tk.Frame] = {}
        # (label, width_margin) pairs whose wraplength tracks the window width
        # so copy re-wraps instead of overflowing when the window is narrowed.
        self._wrap_labels: list[tuple[tk.Label, int]] = []

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

        self._init_style()
        self._build_widgets()
        self._poll_queue()

        self._snip_overlay = SnipOverlay(self.root)
        self._calibrator: BoxSizeCalibrator | None = None
        self._grid_calibrator: GridCalibrator | None = None
        self._cursor_box_overlay = CursorBoxOverlay(self.root)
        self._multi_result_overlay = MultiResultOverlay(self.root)
        self._grid_outline_overlay = GridOutlineOverlay(self.root)

    # --- theme ------------------------------------------------------------
    def _init_style(self) -> None:
        """Restyles the handful of ttk widgets that have no usable tk
        equivalent (combobox, slider, scrollbar) to match the palette.

        Built on "clam" because the native Windows themes ignore most colour
        options - they draw themselves from the OS visual style, so a
        background= on the default theme silently does nothing.
        """
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            log.warning("clam ttk theme unavailable; widgets may not match the dark palette")

        # clam draws a light/dark bevel and a border around the combobox by
        # default (the white ring); collapse all of those onto surface/border
        # colours so it reads as one flat field with a hairline edge.
        style.configure(
            "TCombobox",
            borderwidth=1, arrowsize=14, padding=6,
            bordercolor=BORDER, lightcolor=SURFACE_HI, darkcolor=SURFACE_HI,
            insertcolor=TEXT,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", SURFACE_HI)],
            background=[("readonly", SURFACE_HI)],  # the arrow button box
            foreground=[("readonly", TEXT)],
            arrowcolor=[("readonly", ACCENT), ("disabled", TEXT_DIM)],
            bordercolor=[("focus", ACCENT_DIM), ("!focus", BORDER)],
            lightcolor=[("focus", ACCENT_DIM), ("!focus", SURFACE_HI)],
            darkcolor=[("focus", ACCENT_DIM), ("!focus", SURFACE_HI)],
            selectbackground=[("readonly", SURFACE_HI)],  # kill the blue "selected text" band
            selectforeground=[("readonly", TEXT)],
        )
        # The dropdown is a plain Tk listbox owned by Tk itself, so it's
        # reachable only through the option database, not through ttk.Style.
        self.root.option_add("*TCombobox*Listbox.background", SURFACE_HI)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_DIM)
        self.root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)

        # background = the slider grip; troughcolor = the bar. bordercolor has
        # to be pinned to the trough colour too or clam frames the bar in a
        # bright 1px ring.
        style.configure(
            "Accent.Horizontal.TScale",
            background=ACCENT, troughcolor=SURFACE_HI, borderwidth=0,
            bordercolor=SURFACE_HI, lightcolor=ACCENT, darkcolor=ACCENT_DIM,
        )
        style.map("Accent.Horizontal.TScale", background=[("active", "#6ee8f5")])
        style.configure(
            "Dark.Vertical.TScrollbar",
            background=SURFACE_HI, troughcolor=SURFACE, borderwidth=0,
            arrowcolor=TEXT_DIM, bordercolor=SURFACE,
        )
        style.map("Dark.Vertical.TScrollbar", background=[("active", BORDER)])

    # --- small styled building blocks -------------------------------------
    def _card(self, parent: tk.Misc, **pack_kw) -> tk.Frame:
        """A panel: one flat surface with a hairline border, no bevel."""
        card = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1, bd=0)
        card.pack(**pack_kw)
        return card

    def _section_label(self, parent: tk.Misc, text: str) -> tk.Label:
        return tk.Label(
            parent, text=text.upper(), bg=BG, fg=TEXT_DIM,
            font=(FONT, 8, "bold"), anchor="w",
        )

    def _button(
        self, parent: tk.Misc, text: str, command: Callable[[], None],
        primary: bool = False, danger: bool = False, **pack_kw,
    ) -> tk.Button:
        """A flat button with a manual hover state.

        tk.Button is used rather than ttk.Button because it takes plain
        bg/fg/activebackground options - restyling ttk.Button's hover and
        pressed states means fighting the theme's element layout for the same
        result.
        """
        fg = BG if primary else (DANGER if danger else TEXT)
        bg = ACCENT if primary else SURFACE_HI
        hover = "#6ee8f5" if primary else BORDER
        btn = tk.Button(
            parent, text=text, command=command,
            bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
            disabledforeground=TEXT_DIM, relief="flat", bd=0, highlightthickness=0,
            font=(FONT, 10, "bold" if primary else "normal"),
            cursor="hand2", pady=8, padx=10,
        )
        # Bound rather than relying on activebackground: that only applies
        # while the mouse is DOWN, which reads as no hover feedback at all.
        btn.bind("<Enter>", lambda _e: btn["state"] == "normal" and btn.config(bg=hover))
        btn.bind("<Leave>", lambda _e: btn.config(bg=bg))
        btn._rest_bg = bg  # so _set_button_enabled can restore it
        if pack_kw:
            btn.pack(**pack_kw)
        return btn

    @staticmethod
    def _set_button_enabled(btn: tk.Button, enabled: bool) -> None:
        btn.config(
            state="normal" if enabled else "disabled",
            bg=btn._rest_bg if enabled else SURFACE,
            cursor="hand2" if enabled else "arrow",
        )

    def _build_widgets(self) -> None:
        # The footer (Quit, always-on-top, hint) is pinned to the bottom of
        # the window OUTSIDE the scroll area, so it stays put no matter how
        # short the window gets - it was the first thing to fall off the
        # bottom before. Everything else lives in a vertically scrollable
        # region so a small window just gains a scrollbar instead of clipping.
        self._build_footer(self.root)
        content = self._build_scroll_area(self.root)

        self._build_header(content)
        self._build_tab_bar(content)
        self._build_mode_panels(content)
        self._build_actions(content)
        self._build_settings(content)
        self._build_log(content)
        self._apply_mode_ui(self.selection_mode_var.get())

    # --- scroll area ------------------------------------------------------
    def _build_scroll_area(self, parent: tk.Misc) -> tk.Frame:
        """A canvas-backed vertical scroller. Returns the inner frame that all
        the page content packs into.

        The inner frame is stretched to the canvas WIDTH (so horizontal fill
        still works) and to at least the canvas HEIGHT: when the window is
        tall, the extra height flows into the activity log (which packs with
        expand), so there's no dead space; when the window is short, the inner
        frame keeps its natural height and the canvas scrolls.
        """
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(side="top", fill="both", expand=True)

        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        self._scroll_canvas = canvas
        self._scroll_bar = ttk.Scrollbar(
            wrap, orient="vertical", command=canvas.yview, style="Dark.Vertical.TScrollbar"
        )
        self._scroll_bar_shown = False
        canvas.configure(yscrollcommand=self._scroll_bar.set)
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=BG)
        self._scroll_inner = inner
        self._scroll_win = canvas.create_window((0, 0), window=inner, anchor="nw")

        self._reflow_pending = False
        inner.bind("<Configure>", lambda _e: self._schedule_reflow())
        canvas.bind("<Configure>", lambda _e: self._schedule_reflow())
        # One global wheel binding, routed in the handler (the activity log
        # scrolls itself when hovered; everywhere else scrolls the page).
        canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        return inner

    # Width the vertical scrollbar takes when shown. Reserved from the wrap
    # width at ALL times so toggling the bar never changes how text wraps -
    # if it did, showing the bar could add a line, which could re-trigger the
    # bar, which is exactly the oscillation this avoids.
    _SCROLLBAR_RESERVE = 16

    def _schedule_reflow(self) -> None:
        """Coalesce the flurry of <Configure> events a resize emits into one
        reflow, run once the layout has settled (after_idle). Measuring
        mid-resize reads half-applied geometry - which is how the scrollbar
        got stuck visible on a window that actually fit.
        """
        if self._reflow_pending:
            return
        self._reflow_pending = True
        self.root.after_idle(self._reflow_scroll)

    def _reflow_scroll(self) -> None:
        self._reflow_pending = False
        canvas = self._scroll_canvas
        cw = canvas.winfo_width()
        ch = canvas.winfo_height()

        # Wrap against the width the content gets WITH the scrollbar present,
        # regardless of whether it currently is, so `need` is stable. When the
        # bar is already shown `cw` excludes it; when not, subtract the reserve.
        wrap_w = cw - (0 if self._scroll_bar_shown else self._SCROLLBAR_RESERVE)
        for label, margin in self._wrap_labels:
            label.configure(wraplength=max(160, wrap_w - margin))

        need = self._scroll_inner.winfo_reqheight()
        # Match the inner frame to the canvas width, and to whichever is taller
        # of (its content, the visible canvas) so the log fills spare height.
        canvas.itemconfigure(self._scroll_win, width=cw, height=max(need, ch))
        canvas.configure(scrollregion=(0, 0, cw, max(need, ch)))

        # Only show the scrollbar when there's actually something below the
        # fold; a dead scrollbar on a roomy window is just clutter.
        overflowing = need > ch + 1
        if overflowing and not self._scroll_bar_shown:
            self._scroll_bar.pack(side="right", fill="y")
            self._scroll_bar_shown = True
        elif not overflowing and self._scroll_bar_shown:
            self._scroll_bar.pack_forget()
            self._scroll_bar_shown = False
            canvas.yview_moveto(0)

    def _on_mousewheel(self, event: tk.Event) -> object:
        if not self._scroll_bar_shown:
            return None  # nothing to scroll
        under = self.root.winfo_containing(event.x_root, event.y_root)
        node = under
        while node is not None:
            if node is self.log_box:
                return None  # let the listbox's own wheel binding handle it
            if node is self._scroll_canvas or node is self._scroll_inner:
                break
            node = getattr(node, "master", None)
        else:
            return None  # pointer isn't over the scroll area
        self._scroll_canvas.yview_scroll(-1 * (event.delta // 120), "units")
        return "break"

    def _build_header(self, parent: tk.Misc) -> None:
        header = tk.Frame(parent, bg=BG)
        header.pack(fill="x", padx=16, pady=(14, 10))
        tk.Label(
            header, text="WF-PriceTracker", bg=BG, fg=TEXT, font=(FONT, 15, "bold")
        ).pack(side="left")

        # Status reads as a pill: a coloured dot plus the text, so scan state
        # is legible at a glance from across the screen.
        pill = tk.Frame(header, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        pill.pack(side="right")
        self.status_dot = tk.Label(pill, text="●", bg=SURFACE, fg=TEXT_DIM, font=(FONT, 9))
        self.status_dot.pack(side="left", padx=(8, 4), pady=3)
        tk.Label(
            pill, textvariable=self.status_var, bg=SURFACE, fg=TEXT, font=(FONT, 9, "bold")
        ).pack(side="left", padx=(0, 10), pady=3)

    def _build_tab_bar(self, parent: tk.Misc) -> None:
        """The three selection modes as a tab strip. Picking a tab IS picking
        the mode - there's no separate mode control anymore, so the tab a user
        is looking at always matches what F9/F10 will actually do.
        """
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill="x", padx=16)
        for mode, label in self._MODE_TABS:
            holder = tk.Frame(bar, bg=BG)
            holder.pack(side="left", expand=True, fill="x")
            tab = tk.Label(
                holder, text=label, bg=BG, fg=TEXT_DIM,
                font=(FONT, 10), cursor="hand2", pady=8,
            )
            tab.pack(fill="x")
            # The underline is the selected-state marker; an unselected tab
            # keeps a same-height strip in the background colour so switching
            # tabs doesn't shift the layout by 2px.
            underline = tk.Frame(holder, bg=BORDER, height=2)
            underline.pack(fill="x")
            tab.bind("<Button-1>", lambda _e, m=mode: self._on_tab_clicked(m))
            tab.bind("<Enter>", lambda _e, m=mode: self._on_tab_hover(m, True))
            tab.bind("<Leave>", lambda _e, m=mode: self._on_tab_hover(m, False))
            self._tabs[mode] = (tab, underline)

    def _build_mode_panels(self, parent: tk.Misc) -> None:
        """One card per mode, holding ONLY that mode's own settings. Exactly
        one is packed at a time (see _apply_mode_ui)."""
        self._panel_host = tk.Frame(parent, bg=BG)
        self._panel_host.pack(fill="x", padx=16, pady=(12, 0))

        self._panels["single"] = self._build_single_panel()
        self._panels["multi"] = self._build_multi_panel()
        self._panels["grid"] = self._build_grid_panel()

    def _panel_body(self, blurb: str) -> tuple[tk.Frame, tk.Frame]:
        """Shared shell for a mode panel: an explanatory line plus a body
        frame for that mode's own controls. Returns (card, body)."""
        card = tk.Frame(self._panel_host, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        blurb_label = tk.Label(
            card, text=blurb, bg=SURFACE, fg=TEXT_DIM, font=(FONT, 9),
            wraplength=430, justify="left", anchor="w",
        )
        blurb_label.pack(fill="x", padx=14, pady=(12, 0))
        # ~32px inside-card padding + 16px page padding on each side.
        self._wrap_labels.append((blurb_label, 64))
        body = tk.Frame(card, bg=SURFACE)
        body.pack(fill="x", padx=14, pady=12)
        return card, body

    def _stat_row(self, parent: tk.Misc, label: str, var: tk.StringVar) -> None:
        """A labelled read-only value (calibration state), accent-coloured so
        "is this set up yet?" is answerable without reading the log."""
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(fill="x", pady=(10, 0))
        tk.Label(row, text=label, bg=SURFACE, fg=TEXT_DIM, font=(FONT, 9)).pack(side="left")
        tk.Label(row, textvariable=var, bg=SURFACE, fg=ACCENT, font=(FONT, 9, "bold")).pack(side="left", padx=(6, 0))

    def _build_single_panel(self) -> tk.Frame:
        card, body = self._panel_body(
            "Hover an item in-game and press F9. Grabs a fixed-size box centred on "
            "the cursor, then prices whatever it reads."
        )
        self._button(
            body, "Set Item Box Size…", self._on_set_box_size, fill="x"
        )
        self._stat_row(body, "Box size:", self.box_size_var)
        return card

    def _build_multi_panel(self) -> tk.Frame:
        card, body = self._panel_body(
            "Drag a box around any number of items. On release the whole region is "
            "scanned and every item found is labelled in place with its price."
        )
        tk.Label(
            body, text="Nothing to configure — drag anywhere once scan mode is on.",
            bg=SURFACE, fg=TEXT_DIM, font=(FONT, 9, "italic"), anchor="w",
        ).pack(fill="x")
        return card

    def _build_grid_panel(self) -> tk.Frame:
        card, body = self._panel_body(
            "Calibrate a fixed grid of inventory slots once, then F9 reads every "
            "slot's name band and prices the lot."
        )
        self.calibrate_grid_btn = self._button(
            body, "Calibrate Grid…", self._on_calibrate_grid, fill="x"
        )
        self._stat_row(body, "Grid:", self.grid_info_var)
        return card

    def _build_actions(self, parent: tk.Misc) -> None:
        actions = tk.Frame(parent, bg=BG)
        actions.pack(fill="x", padx=16, pady=(12, 0))
        self.toggle_btn = self._button(
            actions, "Start Scan Mode (F10)", self._on_toggle_scan, primary=True,
            side="left", expand=True, fill="x", padx=(0, 5),
        )
        self.scan_now_btn = self._button(
            actions, "Scan Now (F9)", self._on_scan_now,
            side="left", expand=True, fill="x", padx=(5, 0),
        )

    def _build_settings(self, parent: tk.Misc) -> None:
        self._section_label(parent, "Settings").pack(fill="x", padx=16, pady=(16, 4))
        card = self._card(parent, fill="x", padx=16)

        engine_row = tk.Frame(card, bg=SURFACE)
        engine_row.pack(fill="x", padx=14, pady=(12, 0))
        tk.Label(engine_row, text="OCR engine", bg=SURFACE, fg=TEXT_DIM, font=(FONT, 9)).pack(anchor="w")
        self.engine_combo = ttk.Combobox(
            engine_row,
            textvariable=self.engine_var,
            values=[label for _key, label in self._ENGINE_OPTIONS],
            state="readonly",
        )
        self.engine_combo.pack(fill="x", pady=(4, 0))
        self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine_selected)

        speed_row = tk.Frame(card, bg=SURFACE)
        speed_row.pack(fill="x", padx=14, pady=(12, 0))
        tk.Label(
            speed_row, textvariable=self.price_workers_label_var,
            bg=SURFACE, fg=TEXT_DIM, font=(FONT, 9),
        ).pack(anchor="w")
        self.price_workers_scale = ttk.Scale(
            speed_row,
            from_=config.PRICE_FETCH_WORKERS_MIN,
            to=config.PRICE_FETCH_WORKERS_MAX,
            orient="horizontal",
            style="Accent.Horizontal.TScale",
            command=self._on_price_workers_scale,
        )
        self.price_workers_scale.pack(fill="x", pady=(4, 0))
        speed_hint = tk.Label(
            speed_row,
            text="Higher = faster scans, but warframe.market may rate-limit your IP above ~3.",
            bg=SURFACE, fg=TEXT_DIM, font=(FONT, 8), wraplength=430, justify="left", anchor="w",
        )
        speed_hint.pack(fill="x", pady=(4, 0))
        self._wrap_labels.append((speed_hint, 64))

        key_row = tk.Frame(card, bg=SURFACE)
        key_row.pack(fill="x", padx=14, pady=12)
        self._button(key_row, "Refresh Item List", self._on_refresh_catalog,
                     side="left", expand=True, fill="x", padx=(0, 5))
        # Disabled for now - Claude/Gemini Vision are still in development
        # (see config.DISABLED_ENGINES). Re-enabling these is just dropping
        # the _set_button_enabled(False) calls once those engines are ready.
        self.anthropic_key_btn = self._button(
            key_row, "Anthropic Key…", self._prompt_anthropic_key,
            side="left", expand=True, fill="x", padx=(5, 5),
        )
        self.google_key_btn = self._button(
            key_row, "Google Key…", self._prompt_google_key,
            side="left", expand=True, fill="x", padx=(5, 0),
        )
        self._set_button_enabled(self.anthropic_key_btn, False)
        self._set_button_enabled(self.google_key_btn, False)

    def _build_log(self, parent: tk.Misc) -> None:
        self._section_label(parent, "Activity").pack(fill="x", padx=16, pady=(16, 4))
        log_frame = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        # expand=True so the log soaks up the spare height the scroll area
        # hands down on a tall window; height=4 keeps its *minimum* small so a
        # short window scrolls rather than being dominated by the log.
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 4))
        self.log_box = tk.Listbox(
            log_frame, font=(MONO, 9), activestyle="none", height=4,
            bg=SURFACE, fg=TEXT_DIM, selectbackground=SURFACE_HI, selectforeground=TEXT,
            relief="flat", bd=0, highlightthickness=0,
        )
        scrollbar = ttk.Scrollbar(
            log_frame, orient="vertical", command=self.log_box.yview, style="Dark.Vertical.TScrollbar"
        )
        self.log_box.configure(yscrollcommand=scrollbar.set)
        self.log_box.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        scrollbar.pack(side="right", fill="y", pady=6, padx=(0, 4))

    def _build_footer(self, parent: tk.Misc) -> None:
        # side="bottom" pins this below the scroll area, so Quit and the
        # always-on-top toggle are reachable at every window size. A hairline
        # on top separates it from the scrolling content above.
        shell = tk.Frame(parent, bg=BG)
        shell.pack(side="bottom", fill="x")
        tk.Frame(shell, bg=BORDER, height=1).pack(fill="x")
        bottom = tk.Frame(shell, bg=BG)
        bottom.pack(fill="x", padx=16, pady=10)
        tk.Checkbutton(
            bottom, text="Always on top", variable=self.topmost_var, command=self._apply_topmost,
            bg=BG, fg=TEXT_DIM, selectcolor=SURFACE_HI, activebackground=BG, activeforeground=TEXT,
            font=(FONT, 9), relief="flat", bd=0, highlightthickness=0, cursor="hand2",
        ).pack(side="left")
        self._button(bottom, "Quit", self._on_quit, danger=True, side="right")
        self.hint_var = tk.StringVar(value="F10 toggle   ·   F9 scan at cursor   ·   Ctrl+F10 quit")
        tk.Label(
            bottom, textvariable=self.hint_var, bg=BG, fg=TEXT_DIM, font=(FONT, 8)
        ).pack(side="right", padx=10)

    # --- tab interaction ---------------------------------------------------
    def _on_tab_clicked(self, mode: str) -> None:
        if mode == self.selection_mode_var.get():
            return
        self.selection_mode_var.set(mode)
        self._on_selection_mode_selected()

    def _on_tab_hover(self, mode: str, entering: bool) -> None:
        if mode == self.selection_mode_var.get():
            return  # the selected tab already has its own colours
        tab, _underline = self._tabs[mode]
        tab.config(fg=TEXT if entering else TEXT_DIM)

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
        # Show only the active mode's panel, and mark its tab.
        for m, (tab, underline) in self._tabs.items():
            selected = m == mode
            tab.config(fg=ACCENT if selected else TEXT_DIM, font=(FONT, 10, "bold" if selected else "normal"))
            underline.config(bg=ACCENT if selected else BORDER)
        for m, panel in self._panels.items():
            if m == mode:
                panel.pack(fill="x")
            else:
                panel.pack_forget()

        # F9 ("Scan Now") is meaningful in Single (scan at cursor) and Grid
        # (scan the whole grid), but not in Multi (there you drag instead).
        if mode == "multi":
            self._set_button_enabled(self.scan_now_btn, False)
            self.hint_var.set("F10 toggle   ·   drag to select & scan   ·   Ctrl+F10 quit")
        elif mode == "grid":
            self._set_button_enabled(self.scan_now_btn, True)
            self.hint_var.set("F10 toggle   ·   F9 scan grid   ·   Ctrl+F10 quit")
        else:  # single
            self._set_button_enabled(self.scan_now_btn, True)
            self.hint_var.set("F10 toggle   ·   F9 scan at cursor   ·   Ctrl+F10 quit")

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
        # The dot colour tracks scan state: accent when a scan mode is live,
        # dim otherwise. "Idle" is the only resting state main.py sets.
        active = text != "Idle"

        def _set() -> None:
            self.status_var.set(text)
            self.status_dot.config(fg=ACCENT if active else TEXT_DIM)

        self.call_soon(_set)

    def set_box_size_label(self, text: str) -> None:
        # main.py passes "Box size: WxHpx"; the panel already labels the field
        # "Box size:", so strip a redundant leading label if present.
        value = text.split(":", 1)[1].strip() if text.startswith("Box size:") else text
        self.call_soon(lambda: self.box_size_var.set(value))

    def set_grid_info_label(self, text: str) -> None:
        self.call_soon(lambda: self.grid_info_var.set(text))

    def set_toggle_label(self, text: str) -> None:
        # The primary button keeps its accent bg through label changes.
        def _set() -> None:
            self.toggle_btn.config(text=text)
            self.toggle_btn._rest_bg = ACCENT

        self.call_soon(_set)

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

        def hide() -> None:
            # try/finally: if hiding ever raises we must STILL release the
            # waiter, otherwise the grab stalls for the full timeout and then
            # runs with our windows visible - which is exactly how our own
            # UI text ends up being OCR'd as items.
            try:
                self._hide_for_capture()
            finally:
                hidden.set()

        self.call_soon(hide)
        if not hidden.wait(timeout=1.5):
            log.warning(
                "Timed out waiting to hide windows before a screen grab - the capture may "
                "include this app's own window/labels."
            )
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
            try:
                overlay.withdraw()
            except tk.TclError:
                pass
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
        self._rect_id = self.canvas.create_rectangle(cx, cy, cx, cy, outline=ACCENT, width=2)
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
        self._rect_id = self.canvas.create_rectangle(0, 0, 0, 0, outline=ACCENT, width=2)
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
            cx + pad, cy + pad, text=name, anchor="nw", fill=ACCENT, font=("Segoe UI", 9, "bold")
        )
        price_id = self.canvas.create_text(
            cx + pad, cy + pad + 14, text=price_text, anchor="nw", fill=TEXT, font=("Segoe UI", 8)
        )
        name_box = self.canvas.bbox(name_id)
        price_box = self.canvas.bbox(price_id)
        right = max(name_box[2], price_box[2]) + pad
        bottom = price_box[3] + pad
        bg_id = self.canvas.create_rectangle(cx, cy, right, bottom, fill=SURFACE, outline=ACCENT, width=1)
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
                x - ox, y - oy, x - ox + w, y - oy + h, outline=ACCENT, width=1
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

        frame = tk.Frame(self, bg=SURFACE, highlightbackground=ACCENT, highlightthickness=1)
        frame.pack()
        for i, line in enumerate(lines):
            tk.Label(
                frame,
                text=line,
                bg=SURFACE,
                fg=ACCENT if i == 0 else TEXT,
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
