from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class TimePicker(ttk.Frame):
    """Read-only hour/minute selector with a stable HH:MM:00 value."""

    def __init__(self, master, value: str = "06:00:00", **kwargs) -> None:
        super().__init__(master, **kwargs)
        self.hour_var = tk.StringVar()
        self.minute_var = tk.StringVar()
        self.hour_box = ttk.Combobox(
            self,
            textvariable=self.hour_var,
            values=[f"{value:02d}" for value in range(24)],
            state="readonly",
            width=4,
        )
        self.minute_box = ttk.Combobox(
            self,
            textvariable=self.minute_var,
            values=[f"{value:02d}" for value in range(60)],
            state="readonly",
            width=4,
        )
        self.hour_box.pack(side=tk.LEFT)
        ttk.Label(self, text=":").pack(side=tk.LEFT, padx=3)
        self.minute_box.pack(side=tk.LEFT)
        ttk.Label(self, text=":00").pack(side=tk.LEFT, padx=(3, 0))
        self.set(value)

    def get(self) -> str:
        return f"{self.hour_var.get()}:{self.minute_var.get()}:00"

    def set(self, value: str) -> None:
        parts = str(value or "06:00:00").split(":")
        try:
            hour = min(max(int(parts[0]), 0), 23)
            minute = min(max(int(parts[1]), 0), 59)
        except (ValueError, IndexError):
            hour, minute = 6, 0
        self.hour_var.set(f"{hour:02d}")
        self.minute_var.set(f"{minute:02d}")

    def configure_state(self, enabled: bool) -> None:
        state = "readonly" if enabled else "disabled"
        self.hour_box.configure(state=state)
        self.minute_box.configure(state=state)
