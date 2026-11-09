"""Pin-mux inspection from debugfs, so the UI can flag header pins whose pad is
owned by another function (e.g. I2C) and therefore will not follow GPIO writes.

Only ``pinmux-pins`` and ``gpio-ranges`` are read. Reads run in a daemon thread
with a timeout and at most one read is ever in flight: if the kernel blocks the
read (seen after a pinctrl driver oops) we report ``unavailable`` instead of
piling up stuck threads.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

_PIN_RE = re.compile(
    r"^pin (\d+) \(([^)]*)\): (\(MUX UNCLAIMED\)|\S+) (\(GPIO UNCLAIMED\)|\S+)(?: function (\S+) group (\S+))?"
)
_RANGE_RE = re.compile(r"^\d+: (\S+) GPIOS \[(\d+) - (\d+)\] PINS \[(\d+) - (\d+)\]")


def parse_gpio_ranges(text: str) -> dict[int, int]:
    """pinctrl pin number -> global GPIO number."""
    pin2gpio: dict[int, int] = {}
    for line in text.splitlines():
        m = _RANGE_RE.match(line.strip())
        if m:
            g0, g1, p0, p1 = (int(x) for x in m.groups()[1:])
            for i in range(min(g1 - g0, p1 - p0) + 1):
                pin2gpio[p0 + i] = g0 + i
    return pin2gpio


def parse_pinmux_pins(text: str, pin2gpio: dict[int, int] | None = None) -> dict[int, dict[str, Any]]:
    """GPIO number -> {pad, mux_owner, gpio_owner, function, group, conflict}."""
    out: dict[int, dict[str, Any]] = {}
    for line in text.splitlines():
        m = _PIN_RE.match(line.strip())
        if not m:
            continue
        pin, pad, mux, gpio_owner, func, group = m.groups()
        pin = int(pin)
        if pin2gpio:
            # pin numbers need not equal GPIO numbers (e.g. GPIO70 = pin 71); pins
            # outside the ranges have no GPIO and must not be guessed
            if pin not in pin2gpio:
                continue
            gpio = pin2gpio[pin]
        else:
            gpio = pin
        mux_owner = None if mux == "(MUX UNCLAIMED)" else mux
        out[gpio] = {
            "pad": pad,
            "mux_owner": mux_owner,
            "gpio_owner": None if gpio_owner == "(GPIO UNCLAIMED)" else gpio_owner,
            "function": func,
            "group": group,
            # a device (not the gpio controller) owns the pad mux -> GPIO writes won't reach the pin
            "conflict": bool(mux_owner) and ".gpio" not in mux_owner,
        }
    return out


class PinmuxInspector:
    def __init__(self, debugfs_dir: str | None, timeout: float = 2.0):
        self.dir = Path(debugfs_dir) if debugfs_dir else None
        self.timeout = timeout
        self._lock = threading.Lock()
        self._inflight: threading.Thread | None = None
        self.data: dict[int, dict[str, Any]] = {}
        self.state = "disabled" if self.dir is None else "unknown"
        self.error: str | None = None
        self.updated: float | None = None

    def refresh(self) -> dict[str, Any]:
        if self.dir is None:
            return self.summary()
        with self._lock:
            if self._inflight is not None and self._inflight.is_alive():
                self.state, self.error = "unavailable", "previous debugfs read is still blocked in the kernel"
                return self.summary()
            box: dict[str, Any] = {}
            t = threading.Thread(target=self._read, args=(box,), name="pinmux-read", daemon=True)
            self._inflight = t
            t.start()
        t.join(self.timeout)
        if t.is_alive():
            self.state, self.error = "unavailable", f"debugfs read timed out after {self.timeout}s (pinctrl lock held?)"
        elif "error" in box:
            self.state, self.error = "unavailable", box["error"]
        else:
            self.data, self.state, self.error, self.updated = box["data"], "ok", None, time.time()
        return self.summary()

    def _read(self, box: dict[str, Any]) -> None:
        try:
            ranges = (self.dir / "gpio-ranges").read_text()
            pins = (self.dir / "pinmux-pins").read_text()
            box["data"] = parse_pinmux_pins(pins, parse_gpio_ranges(ranges))
        except OSError as e:
            box["error"] = f"{e.strerror or e}"

    def get(self, gpio: int) -> dict[str, Any] | None:
        return self.data.get(gpio)

    def summary(self) -> dict[str, Any]:
        return {"state": self.state, "error": self.error, "updated": self.updated, "source": str(self.dir) if self.dir else None}
