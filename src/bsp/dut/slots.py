"""Slots of a board host: the places boards sit.

A slot is a serial console. Every UART on the host (a USB-UART adapter in one physical USB port) is a slot
with its own serial-mux daemon, which follows that physical port across reconnects, so a slot's console can
be watched and typed into before anything else is known about it. Everything else is optional and attached as
it is learned: the reset line that restarts the board in it (a gpio_relay pin, or a person), the USB port its
download mode enumerates on, and the DUT, the board identity registered there.

Slots outlive the boards in them. Removing a DUT leaves its slot; registering a board on the slot's console
puts it back. Each slot remembers which reset lines, download ports and boards it had (its history), which the
probe uses to try likely reset lines first. A slot whose adapter was unplugged stays known as offline until it
comes back, or is forgotten.

References: `s3` on its host, `<host>/s3` across the fleet.
"""

from __future__ import annotations

import fnmatch
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..core.errors import Conflict, InvalidRequest, NotFound

SLOT_ID = re.compile(r"^s\d+$")
HISTORY = 20


def port_key(device: str | None, fallback: str | None = None) -> str | None:
    """The physical USB port (controller, port path, interface) of a tty, as serial-mux records it."""
    if device:
        try:
            from serial_mux.state import inspect_usb_device   # type: ignore[import-not-found]

            port = inspect_usb_device(device).get("usb_port")
            if port:
                return port
        except Exception:  # noqa: BLE001 - serial-mux absent here: fall back to what the hook says
            pass
    return fallback or device


class SlotTable:
    """`list_consoles()` returns the probe/consoles hook's list; `start(alias, device)` runs a daemon."""

    def __init__(self, path: Path, list_consoles: Callable[[], list[dict[str, Any]]],
                 start: Callable[[str, str], Any], cfg: dict[str, Any] | None = None):
        self.path = Path(path)
        self._list = list_consoles
        self._start = start
        cfg = cfg or {}
        self.auto_console = bool(cfg.get("auto_console", True))
        self.exclude = list(cfg.get("exclude") or [])
        self._lock = threading.RLock()
        self._live: dict[str, dict[str, Any]] = {}
        data: dict[str, Any] = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
            except ValueError:
                data = {}
        self._slots: dict[str, dict[str, Any]] = data.get("slots") or {}
        self._next = int(data.get("next") or 1)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"next": self._next, "slots": self._slots}, indent=2))
        tmp.replace(self.path)

    def _new(self, **fields: Any) -> dict[str, Any]:
        sid = f"s{self._next}"
        self._next += 1
        slot = {"id": sid, "alias": None, "port": None, "device": None, "reset": None, "control": None, "usb": None,
                "history": [], "created": time.time(), **fields}
        self._slots[sid] = slot
        return slot

    def _excluded(self, device: str | None) -> bool:
        return bool(device) and any(fnmatch.fnmatch(device, pat) for pat in self.exclude)

    # ------------------------------------------------------------- discovery --
    def refresh(self) -> dict[str, Any]:
        """Match the host's consoles to slots: new adapters become new slots (with a serial-mux daemon when
        `auto_console`), known ones are online again, missing ones offline. Returns what changed."""
        consoles = self._list()
        added, started, failed = [], [], []
        with self._lock:
            live: dict[str, dict[str, Any]] = {}
            for c in consoles:
                device = c.get("device")
                if self._excluded(device):
                    continue
                if c.get("kind") == "serial-mux":
                    alias, port = c.get("alias") or c["id"], c.get("usb_port")
                    slot = self.by_alias(alias) or (self._by_port(port) if port else None)
                    if not slot:
                        slot = self._new(alias=alias, port=port)
                        added.append(slot["id"])
                    slot["alias"] = alias
                    if port:
                        slot["port"] = port
                    if device:
                        slot["device"] = device
                    live[slot["id"]] = {"online": bool(device) and bool(c.get("busy")), "device": device,
                                        "log": c.get("log"), "daemon": True}
                    continue
                port = port_key(device, c.get("usb_port"))
                slot = self._by_port(port)
                if not slot:
                    slot = self._new(port=port, device=device)
                    added.append(slot["id"])
                slot["device"] = device
                if not slot.get("alias"):
                    slot["alias"] = slot["id"]
                live[slot["id"]] = {"online": False, "device": device, "daemon": False}
                if self.auto_console:
                    try:
                        self._start(slot["alias"], device)
                        started.append(slot["id"])
                        live[slot["id"]].update(online=True, daemon=True)
                    except Exception as e:  # noqa: BLE001 - a console that will not start is reported, not fatal
                        failed.append({"slot": slot["id"], "device": device, "error": str(e)[:200]})
            self._live = live
            if added or started:
                self._save()
        return {"added": added, "started": started, "failed": failed}

    def _by_port(self, port: str | None) -> dict[str, Any] | None:
        return next((s for s in self._slots.values() if port and s.get("port") == port), None)

    def by_alias(self, alias: str | None) -> dict[str, Any] | None:
        return next((s for s in self._slots.values() if alias and s.get("alias") == alias), None)

    # ------------------------------------------------------------------ reads --
    def get(self, ref: str) -> dict[str, Any]:
        sid = ref.rsplit("/", 1)[-1]
        with self._lock:
            slot = self._slots.get(sid)
        if not slot:
            raise NotFound(f"no slot {ref!r}", hint=f"slots here: {', '.join(sorted(self._slots, key=_order)) or '(none)'}")
        return slot

    def view(self, slot: dict[str, Any]) -> dict[str, Any]:
        live = self._live.get(slot["id"]) or {}
        return {k: slot.get(k) for k in ("id", "alias", "port", "reset", "control", "usb")} | {
            "device": live.get("device"), "online": bool(live.get("online")), "daemon": bool(live.get("daemon")),
            "last_device": slot.get("device"), "history": slot.get("history", [])[-5:]}

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.view(s) for s in sorted(self._slots.values(), key=lambda s: _order(s["id"]))]

    def history_pins(self) -> list[str]:
        """Reset lines any slot ever had, most recent first."""
        seen: dict[str, float] = {}
        with self._lock:
            for s in self._slots.values():
                for h in s.get("history", []):
                    if h.get("reset"):
                        seen[h["reset"]] = max(seen.get(h["reset"], 0), h.get("at", 0))
                if s.get("reset"):
                    seen.setdefault(s["reset"], s.get("created", 0))
        return [p for p, _ in sorted(seen.items(), key=lambda kv: -kv[1])]

    def last_dut(self, sid: str) -> str | None:
        """The name of the board last registered in a slot."""
        hist = [h for h in self.get(sid).get("history", []) if h.get("dut")]
        return hist[-1]["dut"] if hist else None

    # ----------------------------------------------------------------- writes --
    def note(self, alias: str | None, event: str, **data: Any) -> str | None:
        """Record what happened in the slot of a console alias; attach the reset line and download port learned."""
        with self._lock:
            slot = self.by_alias(alias)
            if not slot:
                return None
            for key in ("reset", "control", "usb"):
                if data.get(key):
                    slot[key] = data[key]
            slot.setdefault("history", []).append({"event": event, "at": time.time(), **{k: v for k, v in data.items() if v is not None}})
            slot["history"] = slot["history"][-HISTORY:]
            self._save()
            return slot["id"]

    def rename_alias(self, old: str, new: str) -> None:
        with self._lock:
            slot = self.by_alias(old)
            if slot:
                slot["alias"] = new
                self._save()

    def forget(self, ref: str, in_use: Callable[[str], str | None]) -> dict[str, Any]:
        slot = self.get(ref)
        user = in_use(slot["alias"])
        if user:
            raise Conflict(f"slot {slot['id']} holds {user}", hint=f"remove the DUT first (`bsp dut remove {user}`)")
        if (self._live.get(slot["id"]) or {}).get("online"):
            raise InvalidRequest(f"slot {slot['id']} is online ({slot.get('device')})",
                                 hint="only slots whose adapter is gone can be forgotten")
        with self._lock:
            self._slots.pop(slot["id"], None)
            self._save()
        return {"forgotten": slot["id"], "alias": slot.get("alias")}


def _order(sid: str) -> int:
    m = re.match(r"s(\d+)$", sid)
    return int(m.group(1)) if m else 1 << 30


def is_slot_ref(ref: str | None) -> bool:
    return bool(ref) and bool(SLOT_ID.match(ref.rsplit("/", 1)[-1])) and ref.count("/") <= 1


def render_slots(slots: list[dict[str, Any]]) -> str:
    if not slots:
        return "(no slots: no serial adapter on this host)"
    rows = [("SLOT", "CONSOLE", "STATE", "DUT", "RESET", "DOWNLOAD", "PORT")]
    for s in slots:
        state = "online" if s["online"] else "offline"
        rows.append((s["ref"], s.get("alias") or "-", state + ("" if s.get("console_up") or not s["online"] else " (console down)"),
                     s.get("dut") or "-", s.get("reset") or "-", s.get("usb") or "-",
                     s.get("device") or s.get("last_device") or (s.get("port") or "-")[-40:]))
    w = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in rows)
