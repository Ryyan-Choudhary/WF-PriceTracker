"""The visible app window.

Tkinter isn't thread-safe, so every widget mutation has to happen on the Tk
thread. Other threads (the pynput hotkey thread, the background scan-worker
thread) only ever call the thread-safe methods here (log/set_scan_active/etc),
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

from PIL import Image
from pynput import keyboard

from . import config
from .scan import force_foreground, grab_virtual_screen, primary_screen_size, virtual_screen_rect

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


# --- Hotkey formatting ------------------------------------------------------
# Hotkeys are stored in pynput GlobalHotKeys syntax ("<ctrl>+<f10>", "<f9>",
# "a"); these turn that into something a person reads ("Ctrl + F10").
_HK_MOD_ORDER = ("ctrl", "alt", "shift", "cmd")
_HK_PRETTY = {
    "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win",
    "space": "Space", "esc": "Esc", "enter": "Enter", "tab": "Tab",
    "backspace": "Backspace", "delete": "Del", "insert": "Ins",
    "home": "Home", "end": "End", "page_up": "PgUp", "page_down": "PgDn",
    "up": "↑", "down": "↓", "left": "←", "right": "→",
}


def hotkey_label(hotkey: str) -> str:
    """Human-readable form of a pynput hotkey string, e.g. '<ctrl>+<f10>' ->
    'Ctrl + F10'. Falls back to the raw token for anything unrecognised (a
    bare virtual-key code shows as e.g. 'Key 220')."""
    parts = []
    for token in hotkey.split("+"):
        token = token.strip()
        if token.startswith("<") and token.endswith(">"):
            name = token[1:-1]
            if name in _HK_PRETTY:
                parts.append(_HK_PRETTY[name])
            elif name.isdigit():
                parts.append(f"Key {name}")
            else:
                parts.append(name.upper() if len(name) <= 3 else name.capitalize())
        else:
            parts.append(token.upper())
    return " + ".join(parts) if parts else "(unset)"


# Maps every pynput modifier Key variant (ctrl / ctrl_l / ctrl_r / ...) to the
# canonical GlobalHotKeys token, so a captured combo comes out in the same
# syntax the listener is configured with.
_HK_MOD_MAP: dict = {}
for _name, _tok in (("ctrl", "<ctrl>"), ("alt", "<alt>"), ("shift", "<shift>"), ("cmd", "<cmd>")):
    for _suffix in ("", "_l", "_r", "_gr"):
        _key = getattr(keyboard.Key, _name + _suffix, None)
        if _key is not None:
            _HK_MOD_MAP[_key] = _tok
_HK_MOD_TOKEN_ORDER = [f"<{m}>" for m in _HK_MOD_ORDER]


def _hk_key_token(key) -> str | None:
    """Canonical GlobalHotKeys token for a pynput key, or None to ignore it.

    Modifiers -> '<ctrl>' etc; named keys (F9, Esc) -> '<f9>'; printable chars
    -> the lowercased char; anything else (dead/control chars) -> its virtual
    key code '<220>', which GlobalHotKeys still matches even when the char is
    swallowed by a held modifier."""
    if key in _HK_MOD_MAP:
        return _HK_MOD_MAP[key]
    if isinstance(key, keyboard.Key):
        return f"<{key.name}>"
    char = getattr(key, "char", None)
    if char and char.isprintable() and not char.isspace():
        return char.lower()
    vk = getattr(key, "vk", None)
    return f"<{vk}>" if vk is not None else None


def _autocomplete(index: list, text: str, limit: int = 8) -> list[tuple[str, str]]:
    """Up to `limit` (name, slug) matches for `text` against a catalog of
    (name, slug, lowercased-name) rows: names that START with it first, then
    names that merely contain it. Shared by the inline and quick search."""
    text = text.lower()
    starts: list[tuple[str, str]] = []
    contains: list[tuple[str, str]] = []
    for name, slug, low in index:
        if low.startswith(text):
            starts.append((name, slug))
            if len(starts) >= limit:
                break
        elif text in low:
            contains.append((name, slug))
    return (starts + contains)[:limit]


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


class AppWindow:
    _ENGINE_OPTIONS = [
        ("easyocr", "EasyOCR (accurate, slower)"),
        ("tesseract", "Tesseract (fast, local)"),
    ]

    # The tab strip. The first three are scan modes; "settings" is a plain
    # page (see _active_tab). Kept in one list so they render as one strip.
    _TAB_DEFS = [
        ("multi", "Multi"),
        ("grid", "Grid"),
        ("relic", "Relic"),
        ("settings", "Settings"),
    ]
    _MODE_TABS = ("multi", "grid", "relic")

    def __init__(
        self,
        on_toggle_scan: Callable[[], None],
        on_scan_now: Callable[[], None],
        on_refresh_catalog: Callable[[], None],
        on_engine_change: Callable[[str], None],
        on_selection_mode_change: Callable[[str], None],
        on_calibrate_grid: Callable[[], None],
        on_price_workers_change: Callable[[int], None],
        on_match_tolerance_change: Callable[[int], None],
        on_set_hotkey: Callable[[str, str], None],
        on_hotkey_capture_start: Callable[[], None],
        on_lookup_item: Callable[[str, str], None],
        on_relic_ui_scale_change: Callable[[float], None],
        on_calibrate_relic: Callable[[], None],
        on_clear_relic_region: Callable[[], None],
        on_color_filter_change: Callable[[bool, tuple, int], None],
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
        self.grid_info_var = tk.StringVar(value="Not calibrated")
        self.topmost_var = tk.BooleanVar(value=False)
        self.engine_var = tk.StringVar()
        self.selection_mode_var = tk.StringVar(value="multi")
        self.price_workers_var = tk.IntVar(value=1)
        self.price_workers_label_var = tk.StringVar(value="")
        self.match_cutoff_var = tk.IntVar(value=config.FUZZY_MATCH_SCORE_CUTOFF)
        self.match_tolerance_label_var = tk.StringVar(value="")
        # Relic reward mode
        self.relic_info_var = tk.StringVar(value="Auto (from screen size)")
        self.relic_ui_scale_var = tk.DoubleVar(value=config.RELIC_UI_SCALE)
        self.relic_ui_scale_label_var = tk.StringVar(value="")
        # Text colour filter
        self.color_filter_var = tk.BooleanVar(value=config.TEXT_COLOR_FILTER_ENABLED)
        self.color_tolerance_var = tk.IntVar(value=config.TEXT_COLOR_TOLERANCE)
        self.color_tolerance_label_var = tk.StringVar(value="")
        self._color_rgb = tuple(config.TEXT_COLOR_RGB)  # current picked text colour
        self._current_engine_key = "tesseract"
        self._tabs: dict[str, tuple[tk.Label, tk.Frame]] = {}
        self._panels: dict[str, tk.Frame] = {}
        # Which tab's panel is showing. Distinct from selection_mode_var: the
        # Settings tab is NOT a scan mode, so opening it must not change what
        # F9/F10 do. selection_mode_var stays put; only _active_tab moves.
        self._active_tab = "multi"
        # (label, width_margin) pairs whose wraplength tracks the window width
        # so copy re-wraps instead of overflowing when the window is narrowed.
        self._wrap_labels: list[tuple[tk.Label, int]] = []
        # Hotkey display state (see set_hotkey_labels). Seeded from config so
        # the labels are right on first paint, before main pushes anything.
        self._hk_labels = {
            "scan": hotkey_label(config.HOTKEY_SCAN),
            "toggle": hotkey_label(config.HOTKEY_TOGGLE_SCAN),
            "quit": hotkey_label(config.HOTKEY_QUIT),
            "search": hotkey_label(config.HOTKEY_SEARCH),
        }
        self._hk_vars = {k: tk.StringVar(value=v) for k, v in self._hk_labels.items()}
        self._scan_active = False
        # Manual item search (magnifier button in the header).
        self._search_visible = False
        self._search_index: list[tuple[str, str, str]] = []  # (name, slug, lowercased name)
        self._suggest_win: tk.Toplevel | None = None
        self._suggestions: list[tuple[str, str]] = []  # (name, slug) currently shown
        self._sugg_index = -1
        self._quick_search: "QuickSearchPopup | None" = None  # the hotkey pop-up

        self._on_toggle_scan = on_toggle_scan
        self._on_scan_now = on_scan_now
        self._on_refresh_catalog = on_refresh_catalog
        self._on_engine_change = on_engine_change
        self._on_selection_mode_change = on_selection_mode_change
        self._on_calibrate_grid = on_calibrate_grid
        self._on_price_workers_change = on_price_workers_change
        self._on_match_tolerance_change = on_match_tolerance_change
        self._on_set_hotkey = on_set_hotkey
        self._on_hotkey_capture_start = on_hotkey_capture_start
        self._on_lookup_item = on_lookup_item
        self._on_relic_ui_scale_change = on_relic_ui_scale_change
        self._on_calibrate_relic = on_calibrate_relic
        self._on_clear_relic_region = on_clear_relic_region
        self._on_color_filter_change = on_color_filter_change
        self._on_quit = on_quit

        self._init_style()
        self._build_widgets()
        self._poll_queue()

        self._select_overlay = RegionSelectOverlay(self.root)
        self._multi_result_overlay = MultiResultOverlay(self.root)
        self._grid_outline_overlay = GridOutlineOverlay(self.root)
        self._color_picker_overlay = ColorPickerOverlay(self.root)

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
        self._build_search(content)
        self._build_mode_panels(content)
        self._build_actions(content)
        self._build_log(content)
        self._apply_active_tab(self._active_tab)
        self._apply_mode_controls(self.selection_mode_var.get())
        # Click-away closes the autocomplete dropdown (add="+" so it coexists
        # with the mouse-wheel binding on the scroll area).
        self.root.bind("<Button-1>", self._on_root_click, add="+")

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

        # Magnifier button, just right of the title, opens the manual search;
        # its hotkey is shown alongside so the shortcut is discoverable.
        self._search_btn = tk.Canvas(header, width=26, height=26, bg=BG, highlightthickness=0, cursor="hand2")
        self._render_search_icon(False)
        self._search_btn.pack(side="left", padx=(10, 0))
        self._search_btn.bind("<Button-1>", lambda _e: self._toggle_search())
        self._search_btn.bind("<Enter>", lambda _e: self._render_search_icon(True))
        self._search_btn.bind("<Leave>", lambda _e: self._render_search_icon(False))
        search_hint = tk.Label(
            header, textvariable=self._hk_vars["search"], bg=BG, fg=TEXT_DIM,
            font=(FONT, 8), cursor="hand2",
        )
        search_hint.pack(side="left", padx=(4, 0))
        search_hint.bind("<Button-1>", lambda _e: self._toggle_search())

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
        """The tab strip: three scan modes plus Settings. Clicking a mode tab
        selects that scan mode AND shows its panel; clicking Settings only
        shows the settings panel (the active scan mode is left untouched).
        """
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill="x", padx=16)
        self._tab_bar_frame = bar  # the search bar packs just above this
        for key, label in self._TAB_DEFS:
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
            tab.bind("<Button-1>", lambda _e, k=key: self._on_tab_clicked(k))
            tab.bind("<Enter>", lambda _e, k=key: self._on_tab_hover(k, True))
            tab.bind("<Leave>", lambda _e, k=key: self._on_tab_hover(k, False))
            self._tabs[key] = (tab, underline)

    # --- manual item search -----------------------------------------------
    def _render_search_icon(self, hover: bool) -> None:
        """(Re)draw the magnifier: a ring + handle, accent when hovered or the
        search bar is open, dim otherwise. Drawn rather than an emoji/font
        glyph so it renders crisply and in the theme colour everywhere."""
        color = ACCENT if (hover or self._search_visible) else TEXT_DIM
        cv = self._search_btn
        cv.delete("all")
        cv.create_oval(5, 5, 17, 17, outline=color, width=2)
        cv.create_line(16, 16, 22, 22, fill=color, width=2, capstyle="round")

    def _build_search(self, parent: tk.Misc) -> None:
        # Built now but packed only while open (see _open_search), just above
        # the tab strip. The autocomplete dropdown is a separate Toplevel so it
        # floats over the content instead of shoving the tabs around.
        self._search_frame = tk.Frame(parent, bg=BG)
        box = tk.Frame(self._search_frame, bg=SURFACE_HI, highlightbackground=BORDER, highlightthickness=1)
        box.pack(fill="x")
        self._search_entry = tk.Entry(
            box, bg=SURFACE_HI, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=0, highlightthickness=0, font=(FONT, 11),
        )
        self._search_entry.pack(side="left", fill="x", expand=True, padx=(10, 6), pady=8)
        clear = tk.Label(box, text="✕", bg=SURFACE_HI, fg=TEXT_DIM, cursor="hand2", font=(FONT, 10))
        clear.pack(side="right", padx=(0, 10))
        clear.bind("<Button-1>", lambda _e: self._close_search())

        self._search_entry.bind("<KeyRelease>", self._on_search_key)
        self._search_entry.bind("<Return>", lambda _e: self._submit_search())
        self._search_entry.bind("<Down>", lambda _e: self._move_suggestion(1))
        self._search_entry.bind("<Up>", lambda _e: self._move_suggestion(-1))
        self._search_entry.bind("<Escape>", lambda _e: self._close_search())

    def _toggle_search(self) -> None:
        self._close_search() if self._search_visible else self._open_search()

    def _open_search(self) -> None:
        self._search_frame.pack(fill="x", padx=16, pady=(10, 0), before=self._tab_bar_frame)
        self._search_visible = True
        self._render_search_icon(True)
        self._search_entry.focus_set()
        self._schedule_reflow()

    def _close_search(self) -> None:
        self._hide_suggestions()
        self._search_entry.delete(0, "end")
        self._search_frame.pack_forget()
        self._search_visible = False
        self._render_search_icon(False)
        self._schedule_reflow()

    def _match_names(self, text: str) -> list[tuple[str, str]]:
        return _autocomplete(self._search_index, text)

    def _on_search_key(self, event: tk.Event) -> None:
        if event.keysym in ("Down", "Up", "Return", "Escape", "Left", "Right"):
            return  # navigation / submit handled by their own bindings
        text = self._search_entry.get().strip().lower()
        if not text or not self._search_index:
            self._hide_suggestions()
            return
        self._show_suggestions(self._match_names(text))

    def _show_suggestions(self, items: list[tuple[str, str]]) -> None:
        self._suggestions = items
        self._sugg_index = -1
        if not items:
            self._hide_suggestions()
            return
        if self._suggest_win is None:
            self._suggest_win = tk.Toplevel(self.root)
            self._suggest_win.overrideredirect(True)
            self._suggest_win.attributes("-topmost", True)
            self._suggest_list = tk.Listbox(
                self._suggest_win, activestyle="none", bg=SURFACE_HI, fg=TEXT,
                selectbackground=ACCENT_DIM, selectforeground=TEXT, relief="flat", bd=0,
                highlightthickness=1, highlightbackground=BORDER, font=(FONT, 10),
            )
            self._suggest_list.pack(fill="both", expand=True)
            self._suggest_list.bind("<ButtonRelease-1>", lambda _e: self._pick_clicked())
        lb = self._suggest_list
        lb.delete(0, "end")
        for name, _slug in items:
            lb.insert("end", name)
        lb.config(height=len(items))
        # Anchor the dropdown to the entry's on-screen position and width.
        self.root.update_idletasks()
        e = self._search_entry
        x, y = e.winfo_rootx(), e.winfo_rooty() + e.winfo_height() + 3
        self._suggest_win.geometry(f"{e.winfo_width()}x{self._suggest_win.winfo_reqheight()}+{x}+{y}")
        self._suggest_win.deiconify()
        self._suggest_win.lift()

    def _hide_suggestions(self) -> None:
        self._suggestions = []
        self._sugg_index = -1
        if self._suggest_win is not None:
            self._suggest_win.withdraw()

    def _move_suggestion(self, delta: int) -> object:
        # Keyboard nav keeps focus in the entry and just moves the highlight,
        # sidestepping all the focus juggling a focusable dropdown would need.
        if self._suggest_win is None or not self._suggest_win.winfo_viewable() or not self._suggestions:
            return None
        n = len(self._suggestions)
        self._sugg_index = max(0, min(n - 1, self._sugg_index + delta))
        self._suggest_list.selection_clear(0, "end")
        self._suggest_list.selection_set(self._sugg_index)
        self._suggest_list.see(self._sugg_index)
        return "break"

    def _resolve(self, text: str) -> tuple[str | None, str | None]:
        low = text.lower()
        for name, slug, l in self._search_index:
            if l == low:
                return name, slug
        matches = self._match_names(low)
        return matches[0] if matches else (None, None)

    def _submit_search(self) -> None:
        text = self._search_entry.get().strip()
        if not text:
            return
        if 0 <= self._sugg_index < len(self._suggestions):
            name, slug = self._suggestions[self._sugg_index]  # a highlighted suggestion wins
        else:
            name, slug = self._resolve(text)
        if slug is None:
            self.log(f'No item found for "{text}".')
            return
        self._search_entry.delete(0, "end")
        self._search_entry.insert(0, name)
        self._hide_suggestions()
        self._on_lookup_item(slug, name)

    def _pick_clicked(self) -> None:
        sel = self._suggest_list.curselection()
        if not sel:
            return
        name, slug = self._suggestions[sel[0]]
        self._search_entry.delete(0, "end")
        self._search_entry.insert(0, name)
        self._hide_suggestions()
        self._on_lookup_item(slug, name)

    def _on_root_click(self, event: tk.Event) -> None:
        if self._suggest_win is None or not self._suggest_win.winfo_viewable():
            return
        if event.widget is self._search_entry or event.widget is self._suggest_list:
            return
        self._hide_suggestions()

    def _build_mode_panels(self, parent: tk.Misc) -> None:
        """One card per mode, holding ONLY that mode's own settings. Exactly
        one is packed at a time (see _apply_active_tab)."""
        self._panel_host = tk.Frame(parent, bg=BG)
        self._panel_host.pack(fill="x", padx=16, pady=(12, 0))

        self._panels["multi"] = self._build_multi_panel()
        self._panels["grid"] = self._build_grid_panel()
        self._panels["relic"] = self._build_relic_panel()
        self._panels["settings"] = self._build_settings_panel()

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

    def _build_multi_panel(self) -> tk.Frame:
        card, body = self._panel_body(
            "Press Select Area, drag a box around any number of items, and release. "
            "The whole region is scanned and every item found is labelled in place "
            "with its price. The screen dims while selecting, and your drag is "
            "captured — it won't click anything in-game."
        )
        tk.Label(
            body, text="Selecting is one-shot: it arms on demand, so the mouse stays yours.",
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

    def _build_relic_panel(self) -> tk.Frame:
        card, body = self._panel_body(
            "On the Void Fissure reward-selection screen, press the scan hotkey. "
            "The reward names across the top are read, priced, and the most "
            "valuable is starred. The capture area is derived from the "
            "reward-screen layout, scaled to your resolution."
        )
        tk.Label(
            body, textvariable=self.relic_ui_scale_label_var,
            bg=SURFACE, fg=TEXT_DIM, font=(FONT, 9),
        ).pack(anchor="w")
        self.relic_ui_scale_scale = ttk.Scale(
            body, from_=config.RELIC_UI_SCALE_MIN, to=config.RELIC_UI_SCALE_MAX,
            orient="horizontal", style="Accent.Horizontal.TScale", command=self._on_relic_ui_scale,
        )
        self.relic_ui_scale_scale.pack(fill="x", pady=(4, 0))
        scale_hint = tk.Label(
            body,
            text="Match this to Options > Interface (in-game UI size); 100% = 1.0. "
                 "Only affects the auto area.",
            bg=SURFACE, fg=TEXT_DIM, font=(FONT, 8), wraplength=430, justify="left", anchor="w",
        )
        scale_hint.pack(fill="x", pady=(4, 0))
        self._wrap_labels.append((scale_hint, 64))

        # Optional manual override for unusual resolutions / HUD offsets.
        btn_row = tk.Frame(body, bg=SURFACE)
        btn_row.pack(fill="x", pady=(10, 0))
        self._button(
            btn_row, "Calibrate Reward Area…", self._on_calibrate_relic,
            side="left", expand=True, fill="x", padx=(0, 5),
        )
        self._button(
            btn_row, "Reset to Auto", self._on_clear_relic_region,
            side="left", expand=True, fill="x", padx=(5, 0),
        )
        self._stat_row(body, "Reward area:", self.relic_info_var)
        return card

    def _build_actions(self, parent: tk.Misc) -> None:
        actions = tk.Frame(parent, bg=BG)
        actions.pack(fill="x", padx=16, pady=(12, 0))
        self._actions_frame = actions  # hidden on the Settings tab
        self.toggle_btn = self._button(
            actions, self._toggle_btn_text(), self._on_toggle_scan, primary=True,
            side="left", expand=True, fill="x", padx=(0, 5),
        )
        self.scan_now_btn = self._button(
            actions, self._scan_btn_text(), self._on_scan_now,
            side="left", expand=True, fill="x", padx=(5, 0),
        )

    def _toggle_btn_text(self) -> str:
        verb = "Stop" if self._scan_active else "Start"
        return f"{verb} Scan Mode ({self._hk_labels['toggle']})"

    def _scan_btn_text(self) -> str:
        # In Multi-Select the same action arms a one-shot area pick rather than
        # scanning immediately, so the button says what it actually does.
        verb = "Select Area" if self.selection_mode_var.get() == "multi" else "Scan Now"
        return f"{verb} ({self._hk_labels['scan']})"

    _HK_ACTION_NAMES = {
        "toggle": "Toggle scan mode", "scan": "Scan now",
        "search": "Open search", "quit": "Quit app",
    }

    def _settings_subhead(self, parent: tk.Misc, text: str, first: bool = False) -> None:
        """A divider + small caps heading to group the settings card into
        sections (OCR / hotkeys / catalog)."""
        if not first:
            tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=(14, 0))
        tk.Label(
            parent, text=text.upper(), bg=SURFACE, fg=TEXT_DIM,
            font=(FONT, 8, "bold"), anchor="w",
        ).pack(fill="x", pady=(12, 2))

    def _build_settings_panel(self) -> tk.Frame:
        """The Settings tab's panel: OCR engine, price concurrency, rebindable
        hotkeys, and catalog/key actions - the app-wide controls that don't
        belong to any one scan mode."""
        card = tk.Frame(self._panel_host, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        body = tk.Frame(card, bg=SURFACE)
        body.pack(fill="x", padx=14, pady=(2, 12))

        # --- OCR engine + speed ---
        self._settings_subhead(body, "OCR & speed", first=True)
        tk.Label(body, text="OCR engine", bg=SURFACE, fg=TEXT_DIM, font=(FONT, 9)).pack(anchor="w")
        self.engine_combo = ttk.Combobox(
            body, textvariable=self.engine_var,
            values=[label for _key, label in self._ENGINE_OPTIONS], state="readonly",
        )
        self.engine_combo.pack(fill="x", pady=(4, 0))
        self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine_selected)

        tk.Label(
            body, textvariable=self.price_workers_label_var,
            bg=SURFACE, fg=TEXT_DIM, font=(FONT, 9),
        ).pack(anchor="w", pady=(12, 0))
        self.price_workers_scale = ttk.Scale(
            body, from_=config.PRICE_FETCH_WORKERS_MIN, to=config.PRICE_FETCH_WORKERS_MAX,
            orient="horizontal", style="Accent.Horizontal.TScale", command=self._on_price_workers_scale,
        )
        self.price_workers_scale.pack(fill="x", pady=(4, 0))
        speed_hint = tk.Label(
            body, text="Higher = faster scans, but warframe.market may rate-limit your IP above ~3.",
            bg=SURFACE, fg=TEXT_DIM, font=(FONT, 8), wraplength=430, justify="left", anchor="w",
        )
        speed_hint.pack(fill="x", pady=(4, 0))
        self._wrap_labels.append((speed_hint, 64))

        # --- Matching tolerance ---
        self._settings_subhead(body, "Matching")
        tk.Label(
            body, textvariable=self.match_tolerance_label_var,
            bg=SURFACE, fg=TEXT_DIM, font=(FONT, 9),
        ).pack(anchor="w")
        # from_ > to_ so dragging RIGHT lowers the cutoff, i.e. raises
        # tolerance. The raw value is the score cutoff; the label shows it as a
        # tolerance percentage (see _update_match_tolerance_label).
        self.match_tolerance_scale = ttk.Scale(
            body,
            from_=config.FUZZY_MATCH_SCORE_CUTOFF_MAX,
            to=config.FUZZY_MATCH_SCORE_CUTOFF_MIN,
            orient="horizontal", style="Accent.Horizontal.TScale",
            command=self._on_match_tolerance_scale,
        )
        self.match_tolerance_scale.pack(fill="x", pady=(4, 0))
        match_hint = tk.Label(
            body,
            text="Higher tolerance guesses on messy reads; lower reports 'unmatched' "
                 "instead of risking a wrong item.",
            bg=SURFACE, fg=TEXT_DIM, font=(FONT, 8), wraplength=430, justify="left", anchor="w",
        )
        match_hint.pack(fill="x", pady=(4, 0))
        self._wrap_labels.append((match_hint, 64))

        # --- Text colour filter ---
        self._settings_subhead(body, "Text colour filter")
        tk.Checkbutton(
            body, text="Only read text of a chosen colour",
            variable=self.color_filter_var, command=self._on_color_filter_toggle,
            bg=SURFACE, fg=TEXT, selectcolor=SURFACE_HI, activebackground=SURFACE,
            activeforeground=TEXT, font=(FONT, 9), relief="flat", bd=0,
            highlightthickness=0, cursor="hand2", anchor="w",
        ).pack(fill="x", pady=(2, 0))
        color_row = tk.Frame(body, bg=SURFACE)
        color_row.pack(fill="x", pady=(8, 0))
        tk.Label(color_row, text="Text colour", bg=SURFACE, fg=TEXT_DIM, font=(FONT, 9)).pack(side="left")
        self._color_swatch = tk.Frame(
            color_row, bg=_rgb_to_hex(self._color_rgb), width=22, height=22,
            highlightbackground=BORDER, highlightthickness=1,
        )
        self._color_swatch.pack(side="left", padx=(8, 0))
        self._color_swatch.pack_propagate(False)
        # Two ways to set the colour: the OS colour dialog, or the on-screen
        # eyedropper (freezes the screen and lets you magnify + click a pixel,
        # so you can sample the game's own text colour directly).
        self._button(color_row, "Pick…", self._pick_color, side="right")
        self._button(color_row, "From screen…", self._pick_color_from_screen, side="right", padx=(0, 6))
        tk.Label(
            body, textvariable=self.color_tolerance_label_var,
            bg=SURFACE, fg=TEXT_DIM, font=(FONT, 9),
        ).pack(anchor="w", pady=(10, 0))
        self.color_tolerance_scale = ttk.Scale(
            body, from_=config.TEXT_COLOR_TOLERANCE_MIN, to=config.TEXT_COLOR_TOLERANCE_MAX,
            orient="horizontal", style="Accent.Horizontal.TScale", command=self._on_color_tolerance_scale,
        )
        self.color_tolerance_scale.pack(fill="x", pady=(4, 0))
        cf_hint = tk.Label(
            body,
            text="Set this to your UI theme's text colour to cut through animated "
                 "card art. Leave OFF unless the colour matches — a wrong colour "
                 "hides real text.",
            bg=SURFACE, fg=TEXT_DIM, font=(FONT, 8), wraplength=430, justify="left", anchor="w",
        )
        cf_hint.pack(fill="x", pady=(4, 0))
        self._wrap_labels.append((cf_hint, 64))

        # --- Hotkeys ---
        self._settings_subhead(body, "Hotkeys")
        for action in ("toggle", "scan", "search", "quit"):
            self._hotkey_row(body, action)

        # --- Catalog ---
        self._settings_subhead(body, "Catalog")
        self._button(body, "Refresh Item List", self._on_refresh_catalog, fill="x")
        return card

    def _hotkey_row(self, parent: tk.Misc, action: str) -> None:
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(
            row, text=self._HK_ACTION_NAMES[action], bg=SURFACE, fg=TEXT, font=(FONT, 9)
        ).pack(side="left")
        self._button(row, "Change…", lambda a=action: self._change_hotkey(a), side="right")
        tk.Label(
            row, textvariable=self._hk_vars[action], bg=SURFACE, fg=ACCENT, font=(FONT, 9, "bold")
        ).pack(side="right", padx=10)

    def _change_hotkey(self, action: str) -> None:
        # Suspend global hotkeys while capturing so the keys being pressed to
        # rebind don't also fire the action they're bound to (see App).
        self._on_hotkey_capture_start()
        HotkeyCaptureDialog(
            self.root,
            title=f"Rebind: {self._HK_ACTION_NAMES[action]}",
            on_result=lambda hk, a=action: self._on_set_hotkey(a, hk),
        )

    def _build_log(self, parent: tk.Misc) -> None:
        # Kept as the re-pack anchor so the action bar always lands directly
        # above the activity section when it's shown again.
        self._activity_anchor = self._section_label(parent, "Activity")
        self._activity_anchor.pack(fill="x", padx=16, pady=(16, 4))
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
        # Filled in by _apply_mode_controls from the current hotkey labels.
        self.hint_var = tk.StringVar(value="")
        tk.Label(
            bottom, textvariable=self.hint_var, bg=BG, fg=TEXT_DIM, font=(FONT, 8)
        ).pack(side="right", padx=10)

    # --- tab interaction ---------------------------------------------------
    def _on_tab_clicked(self, key: str) -> None:
        # A mode tab both selects the scan mode and shows its panel; the
        # Settings tab only swaps the panel, leaving the scan mode alone.
        if key in self._MODE_TABS and key != self.selection_mode_var.get():
            self.selection_mode_var.set(key)
            self._apply_mode_controls(key)
            self._on_selection_mode_change(key)
        self._apply_active_tab(key)

    def _on_tab_hover(self, key: str, entering: bool) -> None:
        if key == self._active_tab:
            return  # the active tab already has its own colours
        tab, _underline = self._tabs[key]
        tab.config(fg=TEXT if entering else TEXT_DIM)

    def _apply_topmost(self) -> None:
        self.root.attributes("-topmost", self.topmost_var.get())

    def set_selection_mode_selection(self, mode: str) -> None:
        """Reflects the persisted selection mode at startup without firing
        the on_selection_mode_change callback. Thread-safe.
        """

        def _set() -> None:
            self.selection_mode_var.set(mode)
            self._apply_mode_controls(mode)
            self._apply_active_tab(mode)

        self.call_soon(_set)

    def _apply_active_tab(self, tab: str) -> None:
        """Highlight `tab`, show only its panel, and hide the scan action bar
        on the Settings tab (those buttons are scan controls, not config)."""
        self._active_tab = tab
        for k, (label, underline) in self._tabs.items():
            selected = k == tab
            label.config(fg=ACCENT if selected else TEXT_DIM, font=(FONT, 10, "bold" if selected else "normal"))
            underline.config(bg=ACCENT if selected else BORDER)
        for k, panel in self._panels.items():
            if k == tab:
                panel.pack(fill="x")
            else:
                panel.pack_forget()

        if tab == "settings":
            self._actions_frame.pack_forget()
        elif not self._actions_frame.winfo_ismapped():
            self._actions_frame.pack(fill="x", padx=16, pady=(12, 0), before=self._activity_anchor)

        # The inner frame's height is pinned to the viewport (so the log can
        # fill a tall window), which suppresses the <Configure> that would
        # otherwise re-run the reflow after this content swap - so trigger it
        # explicitly, or a taller panel would clip with no scrollbar.
        self._schedule_reflow()

    def _apply_mode_controls(self, mode: str) -> None:
        """Scan-now enablement and the footer hint follow the active SCAN MODE
        (not the visible tab), so they stay correct while the Settings tab is
        open."""
        if mode == "grid":
            action = f"{self._hk_labels['scan']} scan grid"
        elif mode == "relic":
            action = f"{self._hk_labels['scan']} scan rewards"
        else:  # multi
            action = f"{self._hk_labels['scan']} select area"
        # Every mode now has a scan-hotkey action (Multi's arms an area pick).
        self._set_button_enabled(self.scan_now_btn, True)
        self.scan_now_btn.config(text=self._scan_btn_text())
        self.hint_var.set(
            f"{self._hk_labels['toggle']} toggle   ·   {action}   ·   {self._hk_labels['quit']} quit"
        )

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

    def _on_match_tolerance_scale(self, raw: str) -> None:
        # The raw scale value IS the score cutoff; snap it and only fire the
        # persist callback when the integer cutoff actually changes.
        cutoff = int(round(float(raw)))
        self._update_match_tolerance_label(cutoff)
        if cutoff != self.match_cutoff_var.get():
            self.match_cutoff_var.set(cutoff)
            self._on_match_tolerance_change(cutoff)

    def _update_match_tolerance_label(self, cutoff: int) -> None:
        span = config.FUZZY_MATCH_SCORE_CUTOFF_MAX - config.FUZZY_MATCH_SCORE_CUTOFF_MIN
        # Present the cutoff as leniency: a low cutoff = high tolerance.
        tol = round((config.FUZZY_MATCH_SCORE_CUTOFF_MAX - cutoff) / span * 100) if span else 0
        word = "strict" if tol < 25 else ("balanced" if tol < 60 else "lenient")
        self.match_tolerance_label_var.set(f"Fault tolerance: {tol}% ({word})")

    def set_match_tolerance(self, cutoff: int) -> None:
        """Reflects the persisted match cutoff at startup without firing the
        change callback. Thread-safe."""

        def _set() -> None:
            self.match_cutoff_var.set(cutoff)
            self.match_tolerance_scale.set(cutoff)
            self._update_match_tolerance_label(cutoff)

        self.call_soon(_set)

    # --- relic reward mode ------------------------------------------------
    def _on_relic_ui_scale(self, raw: str) -> None:
        # ttk.Scale is continuous; snap to the nearest 0.05 and only persist
        # when the snapped value actually changes.
        value = round(round(float(raw) / 0.05) * 0.05, 2)
        self._update_relic_ui_scale_label(value)
        if value != round(self.relic_ui_scale_var.get(), 2):
            self.relic_ui_scale_var.set(value)
            self._on_relic_ui_scale_change(value)

    def _update_relic_ui_scale_label(self, value: float) -> None:
        self.relic_ui_scale_label_var.set(f"UI scale: {value:g}  ({int(round(value * 100))}%)")

    def set_relic_ui_scale(self, scale: float) -> None:
        """Reflect the persisted relic UI scale at startup without firing the
        change callback (var is set before the scale, so the command sees no
        change). Thread-safe."""

        def _set() -> None:
            snapped = round(scale, 2)
            self.relic_ui_scale_var.set(snapped)
            self._update_relic_ui_scale_label(snapped)
            self.relic_ui_scale_scale.set(scale)

        self.call_soon(_set)

    def set_relic_info_label(self, text: str) -> None:
        self.call_soon(lambda: self.relic_info_var.set(text))

    # --- text colour filter -----------------------------------------------
    def _on_color_filter_toggle(self) -> None:
        self._push_color_filter()

    def _pick_color(self) -> None:
        from tkinter import colorchooser

        result = colorchooser.askcolor(
            color=_rgb_to_hex(self._color_rgb), parent=self.root,
            title="Pick your UI text colour",
        )
        if result and result[0]:
            self._color_rgb = tuple(int(c) for c in result[0])
            self._color_swatch.config(bg=_rgb_to_hex(self._color_rgb))
            self._push_color_filter()

    def _pick_color_from_screen(self) -> None:
        """On-screen eyedropper: freeze the screen and let the user magnify +
        click a pixel to sample the game's own text colour. The main window is
        hidden first so it isn't part of the frozen screenshot; a short delay
        lets the compositor actually un-map it before the grab."""
        was_visible = bool(self.root.winfo_viewable())
        self.root.withdraw()
        self.root.update_idletasks()
        self.root.after(150, lambda: self._launch_color_picker(was_visible))

    def _launch_color_picker(self, restore_main: bool) -> None:
        try:
            shot, _offset = grab_virtual_screen()
        except Exception:
            log.exception("Colour-picker screen grab failed")
            if restore_main:
                self.root.deiconify()
            self.log("Couldn't grab the screen for the colour picker - check data/logs/app.log.")
            return

        def done(rgb: tuple[int, int, int] | None) -> None:
            if restore_main:
                self.root.deiconify()
                self.root.lift()
            if rgb is None:
                self.log("Colour pick cancelled.")
                return
            self._color_rgb = tuple(int(c) for c in rgb)
            self._color_swatch.config(bg=_rgb_to_hex(self._color_rgb))
            self.log(f"Text colour sampled from screen: {_rgb_to_hex(self._color_rgb)}.")
            self._push_color_filter()

        self._color_picker_overlay.start(shot, done)

    def _on_color_tolerance_scale(self, raw: str) -> None:
        value = int(round(float(raw)))
        self._update_color_tolerance_label(value)
        if value != self.color_tolerance_var.get():
            self.color_tolerance_var.set(value)
            self._push_color_filter()

    def _update_color_tolerance_label(self, value: int) -> None:
        self.color_tolerance_label_var.set(f"Colour tolerance: {value}")

    def _push_color_filter(self) -> None:
        self._on_color_filter_change(
            self.color_filter_var.get(), self._color_rgb, self.color_tolerance_var.get()
        )

    def set_color_filter_state(self, enabled: bool, rgb: tuple, tolerance: int) -> None:
        """Reflect the persisted colour-filter settings at startup without
        firing the change callback (vars set before the scale). Thread-safe."""

        def _set() -> None:
            self.color_filter_var.set(bool(enabled))
            self._color_rgb = tuple(int(c) for c in rgb)
            if hasattr(self, "_color_swatch"):
                self._color_swatch.config(bg=_rgb_to_hex(self._color_rgb))
            self.color_tolerance_var.set(int(tolerance))
            self._update_color_tolerance_label(int(tolerance))
            self.color_tolerance_scale.set(tolerance)

        self.call_soon(_set)

    def _on_engine_selected(self, _event: object = None) -> None:
        label = self.engine_var.get()
        for key, lbl in self._ENGINE_OPTIONS:
            if lbl != label:
                continue
            self._current_engine_key = key
            self._on_engine_change(key)
            return

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

    def set_grid_info_label(self, text: str) -> None:
        self.call_soon(lambda: self.grid_info_var.set(text))

    def set_scan_active(self, active: bool) -> None:
        """Reflect scan-mode on/off across the status pill and the primary
        button's label (Start/Stop + the current toggle hotkey). Thread-safe.
        """

        def _set() -> None:
            self._scan_active = active
            self.status_var.set("Scan mode ON" if active else "Idle")
            self.status_dot.config(fg=ACCENT if active else TEXT_DIM)
            self.toggle_btn.config(text=self._toggle_btn_text())

        self.call_soon(_set)

    def set_hotkey_labels(self, labels: dict) -> None:
        """Update the shown hotkey bindings (a dict of any of scan/toggle/quit
        -> pretty label) after a rebind, and refresh everything derived from
        them: the settings rows, the two action buttons, the footer hint.
        Thread-safe.
        """

        def _set() -> None:
            self._hk_labels.update(labels)
            for action, pretty in labels.items():
                if action in self._hk_vars:
                    self._hk_vars[action].set(pretty)
            self.toggle_btn.config(text=self._toggle_btn_text())
            self.scan_now_btn.config(text=self._scan_btn_text())
            self._apply_mode_controls(self.selection_mode_var.get())

        self.call_soon(_set)

    def show(self) -> None:
        def _show() -> None:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()

        self.call_soon(_show)

    def set_search_catalog(self, entries: list[tuple[str, str]]) -> None:
        """Feed the search box its (name, slug) catalog for autocomplete.
        Called once the item catalog has loaded/refreshed. Thread-safe."""

        def _set() -> None:
            self._search_index = [(name, slug, name.lower()) for name, slug in entries]

        self.call_soon(_set)

    def open_search(self) -> None:
        """Fired by the global search hotkey: pop a small, already-focused
        search bar (NOT the whole app) so you can type and hit Enter straight
        away. Thread-safe."""

        def _do() -> None:
            if self._quick_search is not None and self._quick_search.winfo_exists():
                self._quick_search.reactivate()  # already open - just refocus
                return
            self._quick_search = QuickSearchPopup(
                self.root, self._search_index, self._on_lookup_item,
            )

        self.call_soon(_do)

    def show_item_stats(self, name: str, lines: list[tuple[str, str]] | None) -> None:
        """Pop up the market-stats window for a searched item. `lines` is a
        list of (label, value) rows, or None when nothing could be fetched.
        Thread-safe."""
        self.call_soon(lambda: ItemStatsPopup(self.root, name, lines))

    # --- drag-to-select flows (all run on the capturing overlay) ----------
    def start_region_select(
        self,
        on_complete: Callable[[int, int, int, int], None],
        on_cancel: Callable[[], None],
    ) -> None:
        """One drag to pick the Multi-Select scan region. Reports the region as
        (left, top, right, bottom) in screen coords. Tk thread only."""

        def done(rect: tuple[int, int, int, int] | None) -> None:
            if rect is None:
                on_cancel()
                return
            x, y, w, h = rect
            on_complete(x, y, x + w, y + h)

        self._select_overlay.start(
            "Drag a box around the items to scan    ·    Esc to cancel",
            done, min_px=10,
        )

    def start_grid_calibration(
        self,
        on_complete: Callable[[dict], None],
        on_cancel: Callable[[], None],
    ) -> None:
        """Two drags (first slot's name, then the last slot's name), then a
        rows/cols prompt, then on_complete(grid). Tk thread only."""

        def first_done(first: tuple[int, int, int, int] | None) -> None:
            if first is None:
                on_cancel()
                return
            # Chain the second drag once the first has been released.
            self.root.after(120, lambda: self._select_overlay.start(
                "Now drag a box around the LAST (bottom-right) item's name    ·    Esc to cancel",
                lambda last: self._grid_calibration_finished(first, last, on_complete, on_cancel),
                min_px=8,
            ))

        self._select_overlay.start(
            "Drag a box around the FIRST (top-left) item's name text    ·    Esc to cancel",
            first_done, min_px=8,
        )

    def _grid_calibration_finished(
        self,
        first: tuple[int, int, int, int],
        last: tuple[int, int, int, int] | None,
        on_complete: Callable[[dict], None],
        on_cancel: Callable[[], None],
    ) -> None:
        if last is None:
            on_cancel()
            return
        from tkinter import simpledialog

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
            self._select_overlay,
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


class RegionSelectOverlay(tk.Toplevel):
    """A full-screen surface for dragging out a rectangle, which CAPTURES the
    mouse for the duration.

    This is the important bit: the old overlay used -transparentcolor, and on
    Windows colour-keyed transparent pixels are click-THROUGH, so every drag
    landed on whatever was underneath - dragging a selection box over Warframe
    would select/highlight items in-game. This window instead dims the screen
    with -alpha, which keeps it a real input target, so presses, drags and
    releases are swallowed here and never reach the game.

    Because the drag now happens on our own window, it's handled with ordinary
    Tk bindings rather than a global pynput hook. Escape cancels; a drag
    smaller than min_px is treated as a cancel too (a stray click).
    """

    _DIM = "#05070b"     # near-black wash; -alpha does the rest
    _DIM_ALPHA = 0.30    # visible enough to signal "selection mode", light
                         # enough to still see what you're selecting

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", self._DIM_ALPHA)
        except tk.TclError:
            pass

        self._offset = (0, 0)
        self._rect_id: int | None = None
        self._hint_id: int | None = None
        self._start: tuple[int, int] | None = None
        self._on_done: Callable[[tuple[int, int, int, int] | None], None] | None = None
        self._min_px = 8

        self.canvas = tk.Canvas(
            self, bg=self._DIM, highlightthickness=0, cursor="crosshair",
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Escape>", lambda _e: self._finish(None))
        # Right-click cancels too - the reflex when a selection mode surprises you.
        self.canvas.bind("<ButtonPress-3>", lambda _e: self._finish(None))

    def start(
        self,
        hint: str,
        on_done: Callable[[tuple[int, int, int, int] | None], None],
        min_px: int = 8,
    ) -> None:
        """Show the capture surface and wait for ONE drag. Calls
        on_done((x, y, w, h)) in screen coords, or on_done(None) if cancelled.
        """
        self._on_done = on_done
        self._min_px = min_px
        self._start = None

        # Re-measured each time in case the display configuration changed.
        left, top, width, height = virtual_screen_rect()
        self.geometry(f"{width}x{height}+{left}+{top}")
        self._offset = (left, top)

        self.canvas.delete("all")
        self._rect_id = None
        self._hint_id = self.canvas.create_text(
            width // 2, 40, text=hint, fill=TEXT, font=(FONT, 16, "bold"), anchor="n",
        )
        self.deiconify()
        self.lift()
        force_foreground(self)
        self.focus_force()
        self.grab_set()  # keep Tk input here while selecting

    # --- drag handling (canvas coords; +offset converts to screen) ---
    def _on_press(self, event: tk.Event) -> None:
        self._start = (event.x, event.y)
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
        self._rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline=ACCENT, width=2,
        )

    def _on_motion(self, event: tk.Event) -> None:
        if self._start is None or self._rect_id is None:
            return
        x0, y0 = self._start
        self.canvas.coords(self._rect_id, x0, y0, event.x, event.y)

    def _on_release(self, event: tk.Event) -> None:
        if self._start is None:
            return
        x0, y0 = self._start
        self._start = None
        w, h = abs(event.x - x0), abs(event.y - y0)
        if w < self._min_px or h < self._min_px:
            self._finish(None)  # stray click, not a real selection
            return
        ox, oy = self._offset
        self._finish((min(x0, event.x) + ox, min(y0, event.y) + oy, w, h))

    def _finish(self, rect: tuple[int, int, int, int] | None) -> None:
        on_done, self._on_done = self._on_done, None
        self._start = None
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.canvas.delete("all")
        self._rect_id = None
        self.withdraw()
        if on_done is not None:
            on_done(rect)

    def cancel(self) -> None:
        """Abort an in-progress selection from outside (e.g. mode switched)."""
        if self._on_done is not None:
            self._finish(None)


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


class ColorPickerOverlay(tk.Toplevel):
    """A full-screen on-screen eyedropper. start() displays a FROZEN screenshot
    (so the pixels can't move under the cursor) and follows the mouse with a
    magnifier loupe - a zoomed, pixel-crisp view of the area under the cursor
    with the exact target pixel outlined and its colour read out. Clicking
    samples that pixel's RGB; Esc / right-click cancels.

    Like RegionSelectOverlay this grabs the mouse, so nothing reaches the game
    while picking. It samples the screenshot it was handed rather than the live
    screen, so the overlay's own chrome (loupe, hint) can never contaminate a
    sampled colour.
    """

    _SAMPLE = 15   # source pixels sampled across the loupe (odd -> a true centre)
    _ZOOM = 11     # on-screen pixels per source pixel
    _NUDGE = 26    # gap between cursor and loupe so the loupe never covers the target

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        self._shot: Image.Image | None = None
        self._photo = None           # full-screen ImageTk.PhotoImage (kept alive)
        self._loupe_photo = None     # current loupe ImageTk.PhotoImage (kept alive)
        self._ImageTk = None
        self._on_done: Callable[[tuple[int, int, int] | None], None] | None = None
        self._loupe_ids: list[int] = []

        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<ButtonPress-3>", lambda _e: self._finish(None))
        self.bind("<Escape>", lambda _e: self._finish(None))

    def start(
        self,
        shot: Image.Image,
        on_done: Callable[[tuple[int, int, int] | None], None],
    ) -> None:
        """Show the frozen screenshot full-screen and wait for one pick.
        `shot` must be the full virtual desktop (its pixel (x, y) is drawn at
        canvas (x, y)). Calls on_done((r, g, b)) or on_done(None) if cancelled.
        Tk thread only."""
        from PIL import ImageTk  # local: only needed for the eyedropper

        self._ImageTk = ImageTk
        self._shot = shot.convert("RGB")
        self._on_done = on_done

        left, top, width, height = virtual_screen_rect()
        self.geometry(f"{width}x{height}+{left}+{top}")
        self.canvas.delete("all")
        self._loupe_ids = []
        self._photo = ImageTk.PhotoImage(self._shot)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

        hint = "Move to the item text, then click to sample its colour    ·    Esc to cancel"
        self.canvas.create_rectangle(
            0, 0, width, 48, fill=BG, outline="", stipple="gray50",
        )
        self.canvas.create_text(width // 2, 24, text=hint, fill=TEXT, font=(FONT, 14, "bold"))

        self.deiconify()
        self.lift()
        force_foreground(self)
        self.focus_force()
        self.grab_set()

    def _on_motion(self, event: tk.Event) -> None:
        if self._shot is None:
            return
        for cid in self._loupe_ids:
            self.canvas.delete(cid)
        self._loupe_ids = []

        w, h = self._shot.size
        cx = min(max(event.x, 0), w - 1)
        cy = min(max(event.y, 0), h - 1)

        n, z = self._SAMPLE, self._ZOOM
        half = n // 2
        # Clamp the sample window inside the image; the target pixel's index
        # within the window then follows from where the window actually landed.
        sx = min(max(cx - half, 0), max(0, w - n))
        sy = min(max(cy - half, 0), max(0, h - n))
        crop = self._shot.crop((sx, sy, sx + n, sy + n)).resize((n * z, n * z), Image.NEAREST)
        self._loupe_photo = self._ImageTk.PhotoImage(crop)

        size = n * z
        # Place the loupe near the cursor, flipping to the other side if it
        # would run past the desktop edge.
        lx = cx + self._NUDGE
        ly = cy + self._NUDGE
        if lx + size > w:
            lx = cx - self._NUDGE - size
        if ly + size + 30 > h:
            ly = cy - self._NUDGE - size - 30
        lx = max(0, lx)
        ly = max(0, ly)

        img_id = self.canvas.create_image(lx, ly, anchor="nw", image=self._loupe_photo)
        frame_id = self.canvas.create_rectangle(lx, ly, lx + size, ly + size, outline=ACCENT, width=2)
        # Outline the exact target pixel inside the loupe.
        px = lx + (cx - sx) * z
        py = ly + (cy - sy) * z
        cell_id = self.canvas.create_rectangle(px, py, px + z, py + z, outline="#ffffff", width=2)

        r, g, b = self._shot.getpixel((cx, cy))[:3]
        hexs = "#%02x%02x%02x" % (r, g, b)
        swatch_id = self.canvas.create_rectangle(
            lx, ly + size + 4, lx + 28, ly + size + 28, fill=hexs, outline=ACCENT,
        )
        text_bg = self.canvas.create_rectangle(
            lx + 32, ly + size + 4, lx + size, ly + size + 28, fill=BG, outline="",
        )
        text_id = self.canvas.create_text(
            lx + 36, ly + size + 16, anchor="w",
            text=f"{hexs}  ({r},{g},{b})", fill=TEXT, font=(MONO, 10),
        )
        self._loupe_ids = [img_id, frame_id, cell_id, swatch_id, text_bg, text_id]

    def _on_click(self, event: tk.Event) -> None:
        if self._shot is None:
            self._finish(None)
            return
        w, h = self._shot.size
        x = min(max(event.x, 0), w - 1)
        y = min(max(event.y, 0), h - 1)
        r, g, b = self._shot.getpixel((x, y))[:3]
        self._finish((int(r), int(g), int(b)))

    def _finish(self, rgb: tuple[int, int, int] | None) -> None:
        on_done, self._on_done = self._on_done, None
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.canvas.delete("all")
        self._loupe_ids = []
        self._photo = None
        self._loupe_photo = None
        self._shot = None
        self.withdraw()
        if on_done is not None:
            on_done(rgb)


class HotkeyCaptureDialog(tk.Toplevel):
    """Modal: records the next key or modifier+key combo the user presses and
    hands it back in pynput GlobalHotKeys syntax via on_result(hotkey) - or
    on_result(None) if cancelled (Esc / window closed).

    Capture uses a short-lived pynput keyboard listener rather than Tk key
    events, so it records keys exactly the way the global hotkeys are matched
    (real function keys and modifiers) instead of wrestling with Tk's
    platform-specific modifier bitmasks. The listener runs on pynput's own
    thread, so every UI touch is marshalled back with self.after(0, ...) - the
    same cross-thread pattern the mouse calibrators use.
    """

    def __init__(self, parent: tk.Misc, title: str, on_result: Callable[[str | None], None]) -> None:
        super().__init__(parent)
        self._on_result = on_result
        self._done = False
        self._held: list[str] = []  # modifier tokens currently down

        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.attributes("-topmost", True)

        frame = tk.Frame(self, bg=BG)
        frame.pack(fill="both", expand=True, padx=24, pady=20)
        tk.Label(frame, text=title, bg=BG, fg=TEXT, font=(FONT, 11, "bold")).pack()
        self._prompt = tk.Label(
            frame, text="Press a key or combo…", bg=BG, fg=ACCENT, font=(FONT, 14, "bold")
        )
        self._prompt.pack(pady=(12, 8))
        tk.Label(
            frame, text="Esc to cancel", bg=BG, fg=TEXT_DIM, font=(FONT, 8)
        ).pack()

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        # Esc via Tk too, as a backstop in case the pynput listener misses it.
        self.bind("<Escape>", lambda _e: self._cancel())

        self.update_idletasks()
        self._center_on(parent)
        self.grab_set()

        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()

    def _center_on(self, parent: tk.Misc) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        px = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        self.geometry(f"+{max(0, px)}+{max(0, py)}")

    # --- pynput callbacks (other thread) - marshal every UI touch ---------
    def _on_press(self, key) -> None:
        token = _hk_key_token(key)
        if token is None:
            return
        if key in _HK_MOD_MAP:
            if token not in self._held:
                self._held.append(token)
                self.after(0, self._render_held)
            return
        # A non-modifier key ends the capture: combo = held modifiers + key,
        # modifiers in a stable canonical order.
        mods = [m for m in _HK_MOD_TOKEN_ORDER if m in self._held]
        self.after(0, lambda: self._finish("+".join(mods + [token])))

    def _on_release(self, key) -> None:
        token = _hk_key_token(key)
        if token in self._held:
            self._held.remove(token)
            self.after(0, self._render_held)

    def _render_held(self) -> None:
        if self._done:
            return
        if self._held:
            self._prompt.config(text=" + ".join(hotkey_label(m) for m in self._held) + " + …")
        else:
            self._prompt.config(text="Press a key or combo…")

    def _finish(self, hotkey: str) -> None:
        if self._done:
            return
        self._done = True
        self._teardown()
        self._on_result(hotkey)

    def _cancel(self) -> None:
        if self._done:
            return
        self._done = True
        self._teardown()
        self._on_result(None)

    def _teardown(self) -> None:
        try:
            self._listener.stop()
        except Exception:
            pass
        try:
            self.grab_release()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass


class ItemStatsPopup(tk.Toplevel):
    """A small, centred, dismissable window showing one item's market stats
    (from a manual search): 48h volume/avg/median/range plus the current sell
    book. `lines` is a list of (label, value) rows, or None when nothing could
    be fetched. Closed by the ✕, Escape, or clicking outside is not required -
    it stays put so the numbers can be read."""

    def __init__(self, parent: tk.Misc, name: str, lines: list[tuple[str, str]] | None) -> None:
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.98)
        except tk.TclError:
            pass

        border = tk.Frame(self, bg=BORDER)  # 1px hairline frame
        border.pack(fill="both", expand=True)
        card = tk.Frame(border, bg=SURFACE)
        card.pack(fill="both", expand=True, padx=1, pady=1)

        head = tk.Frame(card, bg=SURFACE)
        head.pack(fill="x", padx=14, pady=(12, 8))
        tk.Label(head, text=name, bg=SURFACE, fg=ACCENT, font=(FONT, 12, "bold")).pack(side="left")
        close = tk.Label(head, text="✕", bg=SURFACE, fg=TEXT_DIM, cursor="hand2", font=(FONT, 11))
        close.pack(side="right", padx=(16, 0))
        close.bind("<Button-1>", lambda _e: self._safe_destroy())

        tk.Frame(card, bg=BORDER, height=1).pack(fill="x", padx=14)

        body = tk.Frame(card, bg=SURFACE)
        body.pack(fill="both", expand=True, padx=14, pady=(8, 12))
        if not lines:
            tk.Label(
                body, text="No market data available for this item.",
                bg=SURFACE, fg=TEXT_DIM, font=(FONT, 10), wraplength=240, justify="left",
            ).pack(anchor="w")
        else:
            for label, value in lines:
                row = tk.Frame(body, bg=SURFACE)
                row.pack(fill="x", pady=3)
                tk.Label(row, text=label, bg=SURFACE, fg=TEXT_DIM, font=(FONT, 9)).pack(side="left")
                tk.Label(row, text=value, bg=SURFACE, fg=TEXT, font=(FONT, 10, "bold")).pack(side="right")

        # Centre on the primary monitor, not the (possibly hidden/behind-game)
        # main window, since this can be triggered by the hotkey search while
        # in-game. Grab foreground too - it appears after a network delay, by
        # which point focus may have returned to the game.
        self.update_idletasks()
        w, h = max(self.winfo_reqwidth(), 240), self.winfo_reqheight()
        sw, sh = primary_screen_size()
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")

        self.bind("<Escape>", lambda _e: self._safe_destroy())
        self.lift()
        force_foreground(self)
        self.focus_force()

    def _safe_destroy(self) -> None:
        try:
            self.destroy()
        except tk.TclError:
            pass


class QuickSearchPopup(tk.Toplevel):
    """A standalone, auto-focused search bar shown by the global search hotkey.

    Unlike the inline header search, this doesn't raise the whole app - it's a
    small floating bar centred on screen that grabs keyboard focus immediately
    (see scan.force_foreground), so you can type an item name, press Enter, and
    get the stats popup without ever clicking. Esc or clicking away closes it.
    """

    _WIDTH = 380

    def __init__(self, parent: tk.Misc, catalog: list, on_lookup: Callable[[str, str], None]) -> None:
        super().__init__(parent)
        self._catalog = catalog  # (name, slug, lowercased-name) rows
        self._on_lookup = on_lookup
        self._suggestions: list[tuple[str, str]] = []
        self._sugg_index = -1
        self._ready = False  # suppresses focus-out close during the focus grab

        self.overrideredirect(True)
        self.attributes("-topmost", True)

        border = tk.Frame(self, bg=ACCENT)  # accent edge signals "typing here"
        border.pack(fill="both", expand=True)
        card = tk.Frame(border, bg=SURFACE_HI)
        card.pack(fill="both", expand=True, padx=2, pady=2)

        row = tk.Frame(card, bg=SURFACE_HI)
        row.pack(fill="x")
        icon = tk.Canvas(row, width=26, height=26, bg=SURFACE_HI, highlightthickness=0)
        icon.create_oval(6, 6, 18, 18, outline=ACCENT, width=2)
        icon.create_line(17, 17, 23, 23, fill=ACCENT, width=2, capstyle="round")
        icon.pack(side="left", padx=(12, 6), pady=10)
        self._entry = tk.Entry(
            row, bg=SURFACE_HI, fg=TEXT, insertbackground=ACCENT,
            relief="flat", bd=0, highlightthickness=0, font=(FONT, 14),
        )
        self._entry.pack(side="left", fill="x", expand=True, padx=(0, 12), pady=10)

        self._list = tk.Listbox(
            card, activestyle="none", bg=SURFACE, fg=TEXT,
            selectbackground=ACCENT_DIM, selectforeground=TEXT, relief="flat", bd=0,
            highlightthickness=0, font=(FONT, 10),
        )
        self._list.bind("<ButtonRelease-1>", lambda _e: self._pick_clicked())

        self._entry.bind("<KeyRelease>", self._on_key)
        self._entry.bind("<Return>", lambda _e: self._submit())
        self._entry.bind("<Down>", lambda _e: self._move(1))
        self._entry.bind("<Up>", lambda _e: self._move(-1))
        self._entry.bind("<Escape>", lambda _e: self._close())
        self.bind("<FocusOut>", lambda _e: self.after(1, self._maybe_close))

        self._place()
        self.lift()
        force_foreground(self)
        self._entry.focus_force()
        # Windows sometimes hands focus over a beat late; re-assert once, then
        # arm the click-away close so startup focus churn can't self-dismiss it.
        self.after(40, self._entry.focus_force)
        self.after(300, lambda: setattr(self, "_ready", True))

    def reactivate(self) -> None:
        """Re-focus an already-open bar (search hotkey pressed again)."""
        self.lift()
        force_foreground(self)
        self._entry.focus_force()

    def _place(self) -> None:
        self.update_idletasks()
        h = self.winfo_reqheight()
        sw, sh = primary_screen_size()
        x = max(0, (sw - self._WIDTH) // 2)
        y = max(0, sh // 4)  # upper third, launcher-style
        self.geometry(f"{self._WIDTH}x{h}+{x}+{y}")

    # --- autocomplete ---
    def _on_key(self, event: tk.Event) -> None:
        if event.keysym in ("Down", "Up", "Return", "Escape", "Left", "Right"):
            return
        text = self._entry.get().strip()
        self._set_suggestions(_autocomplete(self._catalog, text) if text else [])

    def _set_suggestions(self, items: list[tuple[str, str]]) -> None:
        self._suggestions = items
        self._sugg_index = -1
        if not items:
            self._list.pack_forget()
        else:
            self._list.delete(0, "end")
            for name, _slug in items:
                self._list.insert("end", name)
            self._list.config(height=len(items))
            if not self._list.winfo_manager():
                self._list.pack(fill="x", padx=2, pady=(0, 2))
        self._place()

    def _move(self, delta: int) -> object:
        if not self._suggestions:
            return "break"
        self._sugg_index = max(0, min(len(self._suggestions) - 1, self._sugg_index + delta))
        self._list.selection_clear(0, "end")
        self._list.selection_set(self._sugg_index)
        self._list.see(self._sugg_index)
        return "break"

    def _resolve(self, text: str) -> tuple[str | None, str | None]:
        low = text.lower()
        for name, slug, l in self._catalog:
            if l == low:
                return name, slug
        matches = _autocomplete(self._catalog, low)
        return matches[0] if matches else (None, None)

    def _submit(self) -> None:
        text = self._entry.get().strip()
        if not text:
            return
        if 0 <= self._sugg_index < len(self._suggestions):
            name, slug = self._suggestions[self._sugg_index]
        else:
            name, slug = self._resolve(text)
        if slug is None:
            return  # no match - leave the bar open to keep typing
        self._close()
        self._on_lookup(slug, name)

    def _pick_clicked(self) -> None:
        sel = self._list.curselection()
        if not sel:
            return
        name, slug = self._suggestions[sel[0]]
        self._close()
        self._on_lookup(slug, name)

    def _maybe_close(self) -> None:
        # Close once focus has left the whole app (clicked away / alt-tabbed),
        # but not for focus moving to our own listbox, and not during startup.
        if not self._ready:
            return
        try:
            if self.focus_get() is None:
                self._close()
        except (tk.TclError, KeyError):
            pass

    def _close(self) -> None:
        try:
            self.destroy()
        except tk.TclError:
            pass
