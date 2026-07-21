"""The visible app window.

Tkinter isn't thread-safe, so every widget mutation has to happen on the Tk
thread. Other threads (the pynput hotkey thread, the background processing
thread) only ever call the thread-safe methods here (log/set_status/etc),
which either push onto a queue.Queue the Tk mainloop polls, or schedule a
callback via root.after(0, ...).
"""
from __future__ import annotations

import queue
import tkinter as tk
from tkinter import ttk
from typing import Callable


class AppWindow:
    def __init__(
        self,
        on_toggle_capture: Callable[[], None],
        on_capture_now: Callable[[], None],
        on_open_output: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._log_queue: "queue.Queue[str]" = queue.Queue()

        self.root = tk.Tk()
        self.root.title("WF-PriceTracker")
        self.root.geometry("460x420")
        self.root.minsize(360, 300)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.status_var = tk.StringVar(value="Idle")
        self.count_var = tk.StringVar(value="")
        self.topmost_var = tk.BooleanVar(value=False)

        self._on_toggle_capture = on_toggle_capture
        self._on_capture_now = on_capture_now
        self._on_open_output = on_open_output
        self._on_quit = on_quit

        self._build_widgets()
        self._poll_queue()

    def _build_widgets(self) -> None:
        pad = {"padx": 10, "pady": 6}

        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill="x", **pad)
        ttk.Label(status_frame, textvariable=self.status_var, font=("Segoe UI", 14, "bold")).pack(side="left")
        ttk.Label(status_frame, textvariable=self.count_var, font=("Segoe UI", 10)).pack(side="right")

        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", **pad)
        self.toggle_btn = ttk.Button(btn_frame, text="Start Capture (F10)", command=self._on_toggle_capture)
        self.toggle_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Button(btn_frame, text="Capture Now (F9)", command=self._on_capture_now).pack(
            side="left", expand=True, fill="x", padx=(4, 0)
        )

        ttk.Button(self.root, text="Open Output Folder", command=self._on_open_output).pack(fill="x", **pad)

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
        ttk.Label(bottom, text="F10: toggle capture   F9: snap   Ctrl+F10: quit", foreground="gray").pack(
            side="right", padx=8
        )

    def _apply_topmost(self) -> None:
        self.root.attributes("-topmost", self.topmost_var.get())

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

    def set_count(self, text: str) -> None:
        self.call_soon(lambda: self.count_var.set(text))

    def set_toggle_label(self, text: str) -> None:
        self.call_soon(lambda: self.toggle_btn.config(text=text))

    def show(self) -> None:
        def _show() -> None:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()

        self.call_soon(_show)

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
