"""Screen-capture helpers shared by every scan mode: grab an arbitrary
region (multi-select / relic), a stack of rapid frames (grid voting), or the
whole virtual desktop (the on-screen colour eyedropper), plus the global
hotkey listener. All coordinates are physical pixels on the virtual desktop
(the process is per-monitor DPI aware; see main._set_dpi_aware).
"""
from __future__ import annotations

import ctypes
import logging
import time
from typing import Callable

from PIL import Image, ImageGrab
from pynput import keyboard, mouse

from . import config

log = logging.getLogger(__name__)

_mouse_controller = mouse.Controller()


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the full virtual desktop (all monitors
    combined, including monitors placed left of / above the primary, which
    have negative coordinates).
    """
    user32 = ctypes.windll.user32
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def get_cursor_position() -> tuple[int, int]:
    x, y = _mouse_controller.position
    return int(x), int(y)


def primary_screen_size() -> tuple[int, int]:
    """(width, height) of the PRIMARY monitor in physical pixels - used to
    centre the quick-search / stats popups predictably regardless of where the
    (possibly hidden) main window is."""
    user32 = ctypes.windll.user32
    SM_CXSCREEN, SM_CYSCREEN = 0, 1
    return user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)


def force_foreground(window) -> None:
    """Best-effort: make a Tk window the Windows FOREGROUND window so it can
    take keyboard input immediately - even when opened from a global hotkey
    while a game holds focus.

    Windows blocks a background process from calling SetForegroundWindow on its
    own, so this uses the standard AttachThreadInput trick: briefly share input
    state with whatever window is currently in front, then raise ours. Wrapped
    in try/finally so the attach is always undone, and swallows failures (worst
    case the user clicks the bar once, i.e. today's behaviour).
    """
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        GA_ROOT = 2
        hwnd = user32.GetAncestor(int(window.winfo_id()), GA_ROOT)
        if not hwnd:
            return
        foreground = user32.GetForegroundWindow()
        if not foreground or foreground == hwnd:
            user32.SetForegroundWindow(hwnd)
            return
        target_thread = user32.GetWindowThreadProcessId(foreground, None)
        our_thread = kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(our_thread, target_thread, True)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            user32.AttachThreadInput(our_thread, target_thread, False)
    except Exception:
        log.debug("force_foreground failed", exc_info=True)


def grab_virtual_screen() -> tuple[Image.Image, tuple[int, int]]:
    """Grab the ENTIRE virtual desktop (all monitors) as one image, returning
    (image, (left, top)) where (left, top) is the desktop's top-left in screen
    coords. Pixel (px, py) in the image is at screen (left + px, top + py) - the
    mapping the colour eyedropper uses to turn a click into a sampled pixel.
    all_screens=True is required or secondary/left/above monitors are missed.
    """
    vleft, vtop, vwidth, vheight = virtual_screen_rect()
    img = ImageGrab.grab(bbox=(vleft, vtop, vleft + vwidth, vtop + vheight), all_screens=True)
    return img, (vleft, vtop)


def grab_region(left: int, top: int, right: int, bottom: int) -> Image.Image:
    """Grabs an arbitrary screen region (e.g. a user-dragged multi-select
    box), clamped to the virtual desktop's edges.
    """
    vleft, vtop, vwidth, vheight = virtual_screen_rect()
    vright, vbottom = vleft + vwidth, vtop + vheight
    left = max(vleft, left)
    top = max(vtop, top)
    right = min(vright, right)
    bottom = min(vbottom, bottom)
    return ImageGrab.grab(bbox=(left, top, right, bottom))


def capture_frames(left: int, top: int, right: int, bottom: int, n: int, delay: float) -> list[Image.Image]:
    """Grabs the same screen region n times, sleeping `delay` seconds between
    grabs. Used by Grid Scan to capture a few rapid frames to vote across -
    Warframe's animated item-card backgrounds render slightly differently
    each frame, so voting across them beats background-induced OCR errors.
    """
    frames = []
    for i in range(max(1, n)):
        frames.append(grab_region(left, top, right, bottom))
        if i < n - 1:
            time.sleep(delay)
    return frames


class HotkeyListener:
    """Global hotkeys that work even while a fullscreen/borderless game
    window has focus (as long as it isn't exclusive-fullscreen with input
    capture, in which case borderless-window mode in Warframe's display
    settings is recommended).
    """

    def __init__(
        self,
        on_scan: Callable[[], None],
        on_toggle_scan: Callable[[], None],
        on_quit: Callable[[], None],
        on_search: Callable[[], None],
    ) -> None:
        self._on_scan = on_scan
        self._on_toggle_scan = on_toggle_scan
        self._on_quit = on_quit
        self._on_search = on_search
        self._listener: keyboard.GlobalHotKeys | None = None
        self._build()

    def _build(self) -> None:
        # Reads the current config.HOTKEY_* values, so rebinding is just
        # save-then-restart. A pynput listener is single-use (a stopped one
        # can't be restarted), so restart() always constructs a fresh one.
        self._listener = keyboard.GlobalHotKeys(
            {
                config.HOTKEY_SCAN: self._on_scan,
                config.HOTKEY_TOGGLE_SCAN: self._on_toggle_scan,
                config.HOTKEY_QUIT: self._on_quit,
                config.HOTKEY_SEARCH: self._on_search,
            }
        )

    def start(self) -> None:
        if self._listener is not None:
            self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def restart(self) -> None:
        """Rebind to the current config.HOTKEY_* values."""
        self.stop()
        self._build()
        self.start()
