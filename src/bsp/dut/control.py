"""DUT control backends (strategy): how a board is reset / put into FDL.

    gpio-relay      bsp gpio_relay on the same host (default)
    human           a person presses the buttons when asked (alias: manual). The request
                    shows in `bsp dut status/list` and as an event; the waiting reset/boot
                    continues when someone runs `bsp dut confirm <dut>` or when bsp sees the
                    board do it (download-mode console/fastboot, or the boot-ROM fingerprint)
    home-assistant  extension point (not wired up)

FDL strategies (control.fdl.strategy):
    strapped        the board always boots to BROM download mode: reset == enter FDL
    button          hold the FDL line, pulse reset, release (needs control.fdl.pin)
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable

from ..core.errors import HardwareTimeout, InvalidRequest, NotFound, BspError
from ..core.registry import Registry
from .catalog import DutSpec

Log = Callable[[str], None]
# How long a board reset by a person waits for them: long enough to see the request and walk to the board.
HUMAN_TIMEOUT_S = 300.0


def _span(seconds: float) -> str:
    return f"{seconds / 60:g} minutes" if seconds >= 120 else f"{seconds:g}s"
# returns a short description of the evidence once the requested state is visible, else None
Ready = Callable[[], "str | None"]


class Attention:
    """Requests for a person to act on a board, and their confirmations (one per DUT)."""

    def __init__(self, publish: Callable[..., None] = lambda *a, **k: None):
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._publish = publish

    def request(self, dut: str, action: str, message: str) -> threading.Event:
        ev = threading.Event()
        with self._lock:
            self._pending[dut] = {"action": action, "message": message, "since": time.time(), "event": ev}
        self._publish("attention", dut=dut, action=action, message=message)
        return ev

    def confirm(self, dut: str, note: str = "") -> dict[str, Any]:
        with self._lock:
            p = self._pending.get(dut)
        if not p:
            raise NotFound(f"{dut}: nothing is waiting for a person",
                           hint="confirm only after bsp asked for an action (see `bsp dut status`)")
        p["note"] = note
        p["event"].set()
        self._publish("attention.confirmed", dut=dut, action=p["action"], note=note)
        return {"dut": dut, "confirmed": p["action"], "waited_s": round(time.time() - p["since"], 1)}

    def clear(self, dut: str, ev: threading.Event) -> None:
        with self._lock:
            if self._pending.get(dut, {}).get("event") is ev:
                del self._pending[dut]

    def view(self, dut: str) -> dict[str, Any] | None:
        p = self._pending.get(dut)
        return {k: p[k] for k in ("action", "message")} | {"waiting_s": round(time.time() - p["since"])} if p else None


class ControlBackend(ABC):
    needs_human = False      # reset/enter_fdl block until a person acts (run them as jobs)

    def __init__(self, dut: DutSpec, services: Callable[[str], Any]):
        self.dut = dut
        self.cfg = dut.control
        self._services = services

    def setup(self) -> None:
        """Called at host start: prepare lines, register aliases."""

    @abstractmethod
    def reset(self, log: Log, ready: Ready | None = None) -> None: ...

    def enter_fdl(self, log: Log, ready: Ready | None = None) -> None:
        strategy = (self.cfg.get("fdl") or {}).get("strategy", "strapped")
        if strategy == "strapped":
            log("FDL is strapped on this board: a reset lands in BROM download mode")
            self.reset(log, ready)
            return
        if strategy == "button":
            self._fdl_button(log)
            return
        raise InvalidRequest(f"{self.dut.id}: unknown fdl strategy {strategy!r}", hint="use strapped or button")

    def _fdl_button(self, log: Log) -> None:
        raise InvalidRequest(f"{self.dut.id}: backend {type(self).__name__} cannot drive an FDL button")

    def describe(self) -> dict[str, Any]:
        return {"backend": self.cfg.get("backend")}


CONTROL_BACKENDS: Registry[type[ControlBackend]] = Registry("dut control backend")


@CONTROL_BACKENDS.register("gpio-relay")
class GpioRelayControl(ControlBackend):
    """Uses the host's gpio_relay service. Lines are registered as aliases
    `<dut>/reset` and `<dut>/fdl` (pass-through rule: callers may use either)."""

    def _gpio(self):
        return self._services("gpio_relay")

    def _alias(self, role: str) -> str:
        return f"{self.dut.id}/{role}"

    def setup(self) -> None:
        gpio = self._gpio()
        for role in ("reset", "fdl"):
            line = self.cfg.get(role) or {}
            if not line.get("pin"):
                continue
            changes = {k: line[k] for k in ("mode", "active_low", "drive") if k in line}
            changes.setdefault("mode", "relay")
            gpio.configure(line["pin"], changes)
            gpio.alias_add(self._alias(role), line["pin"], tags=f"dut,{self.dut.id},{role}",
                           owner="dut", note=f"{self.dut.id} {role} line", replace=True)
        if self.cfg.get("reset", {}).get("pin"):
            # idle = released; for push-pull relay mode that drives the inactive level
            gpio.switch(self._alias("reset"), "off")

    def reset(self, log: Log, ready: Ready | None = None) -> None:
        line = self.cfg.get("reset") or {}
        if not line.get("pin"):
            raise InvalidRequest(f"{self.dut.id}: control.reset.pin is not configured")
        ms = int(line.get("pulse_ms", 1000))
        log(f"reset: pulse {self._alias('reset')} ({line['pin']}) active for {ms} ms")
        self._gpio().pulse(self._alias("reset"), ms, None, True, False)

    def _fdl_button(self, log: Log) -> None:
        if not (self.cfg.get("fdl") or {}).get("pin"):
            raise InvalidRequest(f"{self.dut.id}: fdl strategy `button` needs control.fdl.pin")
        gpio = self._gpio()
        log(f"FDL: hold {self._alias('fdl')}, reset, release")
        gpio.switch(self._alias("fdl"), "on")
        try:
            self.reset(log)
            time.sleep(float(self.cfg["fdl"].get("hold_after_reset_s", 1.0)))  # strap sampling time
        finally:
            gpio.switch(self._alias("fdl"), "off")

    def describe(self) -> dict[str, Any]:
        gpio = self._gpio()
        out = {"backend": "gpio-relay"}
        for role in ("reset", "fdl"):
            if (self.cfg.get(role) or {}).get("pin"):
                try:
                    snap = gpio.get(self._alias(role))
                    out[role] = {"alias": self._alias(role), "pin": snap["target"], "state": snap.get("state")}
                except BspError as e:
                    out[role] = {"error": e.message}
        out["fdl_strategy"] = (self.cfg.get("fdl") or {}).get("strategy", "strapped")
        return out


@CONTROL_BACKENDS.register("manual")
@CONTROL_BACKENDS.register("human")
class HumanControl(ControlBackend):
    """A person resets the board. bsp asks, then waits for `bsp dut confirm <dut>` or for
    the board itself to show the requested state, whichever comes first.

    The whole request has one window (`human_timeout()`, 5 minutes by default): a person has to see the request,
    walk to the board and press it, so a relay's 30 seconds would fail them. Errors say a person was needed, so an
    agent reading them knows to ask one rather than to rewire the host.

    control.human (optional): {timeout_s: 300, reset: "<how to reset>", fdl: "<how to enter download mode>"}
    """

    needs_human = True

    def human_timeout(self) -> float:
        return float((self.cfg.get("human") or {}).get("timeout_s", HUMAN_TIMEOUT_S))

    def _ask(self, action: str, message: str, log: Log, ready: Ready | None) -> None:
        att: Attention = self._services("dut_attention")
        timeout = self.human_timeout()
        ev = att.request(self.dut.id, action, message)
        log(f"ACTION NEEDED: {message}")
        log(f"waiting for `bsp dut confirm {self.dut.id}`"
            + (" or for the board to show it on its own" if ready else "") + f" (timeout {timeout:g}s)")
        deadline = time.monotonic() + timeout
        try:
            while True:
                if ev.wait(0.5):
                    log(f"{action}: confirmed by a person")
                    return
                seen = ready() if ready else None
                if seen:
                    log(f"{action}: detected {seen}")
                    return
                if time.monotonic() > deadline:
                    raise HardwareTimeout(
                        f"{self.dut.id} is reset by a person (no relay), and nobody did '{action}' within "
                        f"{_span(timeout)}: {message}",
                        hint=f"ask a person at the board to do it (it was asked for on the board's page), then "
                             f"`bsp dut confirm {self.dut.id}`; or retry when someone is there. "
                             f"control.human.timeout_s sets the wait")
        finally:
            att.clear(self.dut.id, ev)

    def _text(self, key: str, default: str) -> str:
        return (self.cfg.get("human") or {}).get(key) or default

    def reset(self, log: Log, ready: Ready | None = None) -> None:
        self._ask("reset", self._text("reset", f"press RESET on {self.dut.id} ({self.dut.board or 'board'})"),
                  log, ready)

    def enter_fdl(self, log: Log, ready: Ready | None = None) -> None:
        strategy = (self.cfg.get("fdl") or {}).get("strategy", "strapped")
        default = (f"press RESET on {self.dut.id} ({self.dut.board or 'board'}); it boots into download mode"
                   if strategy == "strapped" else
                   f"put {self.dut.id} ({self.dut.board or 'board'}) into download mode: hold the "
                   f"download/BOOT button, press RESET, then release")
        self._ask("enter download mode", self._text("fdl", default), log, ready)

    def describe(self) -> dict[str, Any]:
        att: Attention = self._services("dut_attention")
        return {"backend": "human", "fdl_strategy": (self.cfg.get("fdl") or {}).get("strategy", "strapped"),
                "waiting_for": att.view(self.dut.id)}


@CONTROL_BACKENDS.register("home-assistant")
class HomeAssistantControl(ControlBackend):
    """Extension point: the old board-ctrl.sh called HA services
    (switch.turn_on <reset entity>, button.press <fastboot entity>). Re-implement
    here when the HA instance is back; config: control.ha.{url, token_env, reset, fastboot}."""

    def reset(self, log: Log, ready: Ready | None = None) -> None:
        raise InvalidRequest(
            f"{self.dut.id}: home-assistant control backend is not wired up on this host",
            hint="switch control.backend to gpio-relay, or implement HomeAssistantControl.reset",
        )


def make_control(dut: DutSpec, services: Callable[[str], Any]) -> ControlBackend:
    return CONTROL_BACKENDS.get(dut.control.get("backend", "gpio-relay"))(dut, services)
