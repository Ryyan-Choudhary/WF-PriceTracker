"""Single-item scanning: grab a fixed-size box centered on the cursor and
hand it off for OCR + pricing. Replaces the old multi-screenshot capture
flow - one hotkey press always means exactly one item, so there's no room
for a batch of screenshots to produce more matches than there are items.
"""
from __future__ import annotations

import ctypes
import logging
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


def grab_box_at(cx: int, cy: int, box_w: int, box_h: int) -> Image.Image:
    """Grabs a box_w x box_h region of the screen centered on (cx, cy),
    clamped so it never reaches past the virtual desktop's edges.
    """
    vleft, vtop, vwidth, vheight = virtual_screen_rect()
    vright, vbottom = vleft + vwidth, vtop + vheight

    left = cx - box_w // 2
    top = cy - box_h // 2
    left = max(vleft, min(left, vright - box_w))
    top = max(vtop, min(top, vbottom - box_h))
    return ImageGrab.grab(bbox=(left, top, left + box_w, top + box_h))


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


class CursorTracker:
    """Continuously reports the cursor's position via on_move while active -
    used to keep the scan-mode box outline glued to the mouse. Not a
    persistent global hook: it's only started while scan mode is toggled
    on, and stopped as soon as it's toggled off.
    """

    def __init__(self, on_move: Callable[[int, int], None]) -> None:
        self._on_move = on_move
        self._listener: mouse.Listener | None = None

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = mouse.Listener(on_move=lambda x, y: self._on_move(x, y))
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


class DragSelectWatcher:
    """Watches for left-click-drags anywhere on screen and reports each
    completed drag's bounding box - used for multi-select scan mode. Unlike
    the one-shot BoxSizeCalibrator, this keeps listening for repeated drags
    until explicitly stopped (i.e. for as long as multi-select scan mode
    stays toggled on), so you can select and scan several regions in a row
    without re-arming anything.
    """

    MIN_DRAG_PX = 10

    def __init__(
        self,
        on_drag_start: Callable[[int, int], None],
        on_drag_update: Callable[[int, int], None],
        on_drag_end: Callable[[int, int, int, int], None],
    ) -> None:
        self._on_drag_start = on_drag_start
        self._on_drag_update = on_drag_update
        self._on_drag_end = on_drag_end
        self._start: tuple[int, int] | None = None
        self._listener: mouse.Listener | None = None

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = mouse.Listener(on_click=self._on_click, on_move=self._on_move)
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        self._start = None

    def _on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        if button != mouse.Button.left:
            return
        if pressed:
            self._start = (x, y)
            self._on_drag_start(x, y)
            return

        start = self._start
        self._start = None
        if start is None:
            return
        x0, y0 = start
        if abs(x - x0) < self.MIN_DRAG_PX or abs(y - y0) < self.MIN_DRAG_PX:
            return  # too small - treat as a stray click, not a real selection
        left, right = sorted((x0, x))
        top, bottom = sorted((y0, y))
        self._on_drag_end(left, top, right, bottom)

    def _on_move(self, x: int, y: int) -> None:
        if self._start is not None:
            self._on_drag_update(x, y)


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
    ) -> None:
        self._listener = keyboard.GlobalHotKeys(
            {
                config.HOTKEY_SCAN: on_scan,
                config.HOTKEY_TOGGLE_SCAN: on_toggle_scan,
                config.HOTKEY_QUIT: on_quit,
            }
        )

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()
