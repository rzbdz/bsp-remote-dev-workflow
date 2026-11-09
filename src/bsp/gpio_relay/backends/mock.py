"""In-memory backend for development, tests and dry runs (no hardware)."""

from __future__ import annotations

import threading

from . import BACKENDS
from .base import GpioBackend, LineStatus


@BACKENDS.register("mock")
class MockBackend(GpioBackend):
    name = "mock"

    def __init__(self, pull: int = 1):
        self.pull = pull  # level read on an input line (simulated external pull-up)
        self._lines: dict[int, tuple[str, int]] = {}
        self._lock = threading.Lock()
        self.history: list[tuple[int, str, int | None]] = []

    def set_output(self, gpio: int, value: int) -> None:
        with self._lock:
            self._lines[gpio] = ("out", 1 if value else 0)
            self.history.append((gpio, "out", 1 if value else 0))

    def set_input(self, gpio: int) -> None:
        with self._lock:
            self._lines[gpio] = ("in", self.pull)
            self.history.append((gpio, "in", None))

    def status(self, gpio: int) -> LineStatus:
        with self._lock:
            if gpio not in self._lines:
                return LineStatus(gpio, claimed=False)
            d, v = self._lines[gpio]
            return LineStatus(gpio, True, d, v, "bsp")

    def free(self, gpio: int) -> None:
        with self._lock:
            self._lines.pop(gpio, None)
            self.history.append((gpio, "free", None))
