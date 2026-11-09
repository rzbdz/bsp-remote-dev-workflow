"""Legacy sysfs backend (/sys/class/gpio). Line state persists across service restarts."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from ...core.errors import HardwareError
from . import BACKENDS
from .base import GpioBackend, LineStatus


@BACKENDS.register("sysfs")
class SysfsBackend(GpioBackend):
    name = "sysfs"

    def __init__(self, root: str = "/sys/class/gpio", export_timeout: float = 2.0, unexport_foreign: bool = False):
        self.root = Path(root)
        self.export_timeout = export_timeout
        self.unexport_foreign = unexport_foreign
        self._exported_by_us: set[int] = set()
        self._lock = threading.Lock()
        if not (self.root / "export").exists():
            raise HardwareError(f"sysfs GPIO interface not available at {self.root}")

    # ------------------------------------------------------------ helpers --
    def _dir(self, gpio: int) -> Path:
        return self.root / f"gpio{gpio}"

    def _write(self, path: Path, data: str) -> None:
        try:
            with open(path, "w") as f:
                f.write(data)
        except OSError as e:
            raise HardwareError(f"write '{data}' to {path} failed: {e.strerror or e}") from e

    def _read(self, path: Path) -> str:
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError as e:
            raise HardwareError(f"read {path} failed: {e.strerror or e}") from e

    def _ensure_exported(self, gpio: int) -> Path:
        d = self._dir(gpio)
        if d.exists():
            return d
        with self._lock:
            if not d.exists():
                self._write(self.root / "export", str(gpio))
                self._exported_by_us.add(gpio)
        deadline = time.monotonic() + self.export_timeout
        while not (d / "direction").exists() or not os.access(d / "direction", os.W_OK):
            if time.monotonic() > deadline:
                raise HardwareError(f"gpio{gpio} did not appear after export")
            time.sleep(0.01)
        return d

    # ---------------------------------------------------------- interface --
    def set_output(self, gpio: int, value: int) -> None:
        d = self._ensure_exported(gpio)
        # "high"/"low" sets direction=out and value atomically (no glitch)
        self._write(d / "direction", "high" if value else "low")

    def set_input(self, gpio: int) -> None:
        d = self._ensure_exported(gpio)
        self._write(d / "direction", "in")

    def status(self, gpio: int) -> LineStatus:
        d = self._dir(gpio)
        if not d.exists():
            return LineStatus(gpio, claimed=False)
        direction = self._read(d / "direction")
        value = int(self._read(d / "value"))
        if self._read(d / "active_low") == "1":  # report physical level
            value ^= 1
        owner = "bsp" if gpio in self._exported_by_us else "sysfs"
        return LineStatus(gpio, True, direction, value, owner)

    def free(self, gpio: int) -> None:
        d = self._dir(gpio)
        if not d.exists():
            return
        self._write(d / "direction", "in")
        if gpio in self._exported_by_us or self.unexport_foreign:
            self._write(self.root / "unexport", str(gpio))
            self._exported_by_us.discard(gpio)

    def info(self) -> dict:
        return {"name": self.name, "root": str(self.root), "exported_by_bsp": sorted(self._exported_by_us)}
