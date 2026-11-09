"""Watch a host for serial and USB devices coming and going.

A new USB-UART (`/dev/ttyUSB*`, `/dev/ttyACM*`) or USB device (a board's download port) is
how a newly cabled board first shows up. The watcher polls the device nodes and sysfs (no
udev dependency) and reports each change; the dut module publishes it as `dut.ports`, and a
host that announces itself to a registry tells the registry at once.
"""

from __future__ import annotations

import glob
import os
import threading
from pathlib import Path
from typing import Callable

TTY_GLOBS = ("/dev/ttyUSB*", "/dev/ttyACM*")


def snapshot(tty_globs=TTY_GLOBS, usb_root: str = "/sys/bus/usb/devices") -> dict[str, list[str]]:
    """Serial device nodes, and USB devices as `usb:<path> <vid>:<pid>` (interfaces and hubs' roots skipped)."""
    ttys = sorted({p for g in tty_globs for p in glob.glob(g)})
    usb = []
    root = Path(usb_root)
    if root.is_dir():
        for d in sorted(root.iterdir()):
            vid = d / "idVendor"
            if ":" in d.name or d.name.startswith("usb") or not vid.exists():
                continue
            try:
                usb.append(f"usb:{d.name} {vid.read_text().strip()}:{(d / 'idProduct').read_text().strip()}")
            except OSError:
                continue
    return {"tty": ttys, "usb": usb}


class PortWatcher:
    def __init__(self, on_change: Callable[[dict, dict], None], interval: float = 2.0,
                 probe: Callable[[], dict] = snapshot):
        self.on_change, self.interval, self.probe = on_change, max(0.2, float(interval)), probe
        self.current = probe()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ports", daemon=True)

    def check(self) -> dict | None:
        """Look once; report and return the change, if any."""
        now = self.probe()
        added = {k: sorted(set(now[k]) - set(self.current.get(k, []))) for k in now}
        removed = {k: sorted(set(self.current.get(k, [])) - set(now[k])) for k in now}
        if not any(added.values()) and not any(removed.values()):
            return None
        self.current = now
        change = {"added": added, "removed": removed}
        self.on_change(change, now)
        return change

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.check()
            except Exception:  # noqa: BLE001 - keep watching
                pass

    def start(self) -> "PortWatcher":
        if os.environ.get("BSP_WATCH_PORTS", "1") != "0":
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
