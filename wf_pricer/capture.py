"""Screen capture session management and global hotkey binding.

A CaptureSession represents one "run" of screenshots: start it, take one or
more shots with capture_one(), stop it, and hand session_dir to the pipeline.

capture_one() only does the part that has to happen the instant the hotkey
is pressed (grabbing the current frame); encoding it to PNG and writing it to
disk is handed off to a background thread so back-to-back hotkey presses
don't have to wait on disk/CPU-bound work. Call wait_for_pending_saves()
before reading the session folder (the pipeline does this) to make sure
every screenshot has actually finished writing.
"""
from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from PIL import ImageGrab
from pynput import keyboard

from . import config

log = logging.getLogger(__name__)


class CaptureSession:
    def __init__(self) -> None:
        self.active = False
        self.session_id: Optional[str] = None
        self.session_dir: Optional[Path] = None
        self.count = 0
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wf-capture-save")
        self._pending: list[Future] = []

    def start(self) -> str:
        self.session_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = config.CAPTURES_DIR / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self._pending.clear()
        self.active = True
        log.info("Capture session %s started", self.session_id)
        return self.session_id

    def stop(self) -> None:
        self.active = False
        log.info("Capture session %s stopped (%d screenshot(s))", self.session_id, self.count)

    def capture_one(self) -> int:
        """Grabs the screen right now (synchronous - has to happen at the
        moment the hotkey fires) and queues the PNG write in the background.
        Returns the 1-based shot number, or 0 if capture mode isn't active.
        """
        if not self.active or self.session_dir is None:
            return 0
        self.count += 1
        index = self.count
        path = self.session_dir / f"{index:03d}.png"
        image = ImageGrab.grab(all_screens=config.CAPTURE_ALL_MONITORS)

        def _save() -> None:
            image.save(path)
            log.info("Captured screenshot #%d -> %s", index, path.name)

        self._pending.append(self._executor.submit(_save))
        return index

    def wait_for_pending_saves(self, timeout: Optional[float] = None) -> None:
        pending, self._pending = self._pending, []
        for future in pending:
            future.result(timeout=timeout)


class HotkeyListener:
    """Global hotkeys that work even while a fullscreen/borderless game
    window has focus (as long as it isn't exclusive-fullscreen with input
    capture, in which case borderless-window mode in Warframe's display
    settings is recommended).
    """

    def __init__(
        self,
        on_capture: Callable[[], None],
        on_toggle: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._listener = keyboard.GlobalHotKeys(
            {
                config.HOTKEY_CAPTURE: on_capture,
                config.HOTKEY_TOGGLE: on_toggle,
                config.HOTKEY_QUIT: on_quit,
            }
        )

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()
