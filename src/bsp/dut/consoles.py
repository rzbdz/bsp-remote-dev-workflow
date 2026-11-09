"""Serial consoles on a host: serial-mux daemons run as their own systemd units.

A daemon started from inside bsp-hostd would live in the service's cgroup and be
killed whenever the service restarts. Each console is
therefore a transient unit `bsp-console-<alias>` with Restart=always, created
with systemd-run. Consoles already running some other way (e.g. a daemon an
operator started by hand) are left alone.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from ..core.errors import HardwareError, InvalidRequest


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


class ConsoleManager:
    def __init__(self, serial_mux: str, log_dir: str):
        self.serial_mux = serial_mux
        self.run_dir = Path(log_dir).parent / "run"

    def info(self, alias: str) -> dict | None:
        p = self.run_dir / f"{alias}.json"
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None

    def running(self, alias: str) -> bool:
        i = self.info(alias)
        return bool(i and _alive(i.get("pid")))

    def socket(self, alias: str) -> Path:
        """The daemon's Unix socket (serial-mux protocol): from its run record, else where serial-mux puts it."""
        i = self.info(alias) or {}
        return Path(i.get("socket") or self.run_dir.parent / "sock" / f"{alias}.sock")

    def up(self, alias: str) -> bool:
        """A daemon serves `alias` and its socket accepts clients."""
        return self.running(alias) and self.socket(alias).exists()

    @staticmethod
    def unit(alias: str) -> str:
        return "bsp-console-" + re.sub(r"[^A-Za-z0-9_.-]", "_", alias)

    def ensure(self, alias: str, device: str | None = None, only_if_known: bool = False) -> str:
        """Make sure a serial-mux daemon serves `alias`. Returns how it is served."""
        if self.running(alias):
            return "running"
        known = self.info(alias) is not None
        if only_if_known and not known and not device:
            return "unknown"
        if not known and not device:
            raise InvalidRequest(f"serial-mux has no alias {alias!r} and no device was given",
                                 hint="pass the tty (console.device) or start it: serial-mux start <tty> --alias NAME")
        unit = self.unit(alias)
        subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)
        subprocess.run(["systemctl", "stop", unit], capture_output=True)
        cmd = [self.serial_mux, "start", "--alias", alias, "--foreground"]
        if device and not known:
            cmd.insert(2, device)
        r = subprocess.run(["systemd-run", f"--unit={unit}", "--collect", "-p", "Restart=always",
                            "-p", "RestartSec=2", *cmd], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise HardwareError(f"could not start console {alias}: {(r.stderr or r.stdout).strip()[-300:]}",
                                hint=f"try by hand: {' '.join(cmd)}")
        for _ in range(30):
            if self.running(alias):
                return "started"
            time.sleep(0.2)
        raise HardwareError(f"console {alias} did not come up", hint=f"journalctl -u {unit}")

    def stop(self, alias: str) -> None:
        """Stop the console's daemon and forget it (serial-mux drops its record)."""
        subprocess.run(["systemctl", "stop", self.unit(alias)], capture_output=True)
        subprocess.run([self.serial_mux, "stop", alias], capture_output=True, timeout=30)

    def rename(self, old: str, new: str, device: str | None) -> None:
        info = self.info(old) or {}
        dev = device or info.get("device")
        subprocess.run(["systemctl", "stop", self.unit(old)], capture_output=True)
        subprocess.run([self.serial_mux, "stop", old], capture_output=True, timeout=30)
        for _ in range(25):
            if not self.running(old):
                break
            time.sleep(0.2)
        self.ensure(new, dev)
