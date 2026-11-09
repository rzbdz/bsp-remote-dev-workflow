"""GpioRelayService: the facade every front-end talks to."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from ..core.alias import Alias, AliasTable
from ..core.errors import Conflict, Forbidden, InvalidRequest, NotFound, BspError
from ..core.events import EventBus
from .backends.base import GpioBackend
from .board import BoardProfile, HeaderPin, PinKind
from .channels import ButtonChannel, Channel, ChannelConfig, ChannelRuntime, GpioChannel, Mode, SwitchChannel, create_channel
from .pinmux import PinmuxInspector

_GPIO_REF = re.compile(r"^gpio[:_]?(\d+)$", re.I)
_PIN_REF = re.compile(r"^(?:p|pin)?:?(\d+)$", re.I)
_HEADER_PIN_REF = re.compile(r"^([A-Za-z][\w-]*):(?:p|pin)?(\d+)$", re.I)
TARGET_FORMS = "33 | P33 | J40:33 (physical pin) | GPIO127 (Linux gpio) | alias such as DUT3/reset"


def is_native_ref(t: str) -> bool:
    return bool(_GPIO_REF.match(t) or _PIN_REF.match(t) or _HEADER_PIN_REF.match(t))


class ConfigStore:
    """Runtime channel-config overlay (JSON) layered over the host-config baseline."""

    def __init__(self, path: Path | None):
        self.path = path

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path or not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8")).get("channels", {})

    def save(self, channels: dict[str, dict[str, Any]]) -> None:
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"channels": channels}, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


class GpioRelayService:
    LOCK_WAIT_S = 0.5

    def __init__(
        self,
        board: BoardProfile,
        backend: GpioBackend,
        bus: EventBus,
        *,
        baseline: dict[str, dict[str, Any]] | None = None,
        aliases: dict[str, Any] | None = None,
        store: ConfigStore | None = None,
        alias_path: Path | None = None,
        protected: Iterable[str | int] = (),
        pinmux: PinmuxInspector | None = None,
        max_pulse_ms: int = 60_000,
    ):
        self.board = board
        self.backend = backend
        self.bus = bus
        self.store = store or ConfigStore(None)
        self.pinmux = pinmux or PinmuxInspector(None)
        self.max_pulse_ms = max_pulse_ms
        self._pin_locks: dict[str, threading.Lock] = {}
        self._runtime: dict[str, ChannelRuntime] = {}
        self._channels: dict[str, Channel] = {}
        self.aliases = AliasTable(
            self._canonical_gpio_key, path=alias_path, reserved=is_native_ref, describe=self._describe_key
        )

        self._baseline = {self._native_pin(k).key: ChannelConfig.from_dict(v) for k, v in (baseline or {}).items()}
        self._overlay = {k: ChannelConfig.from_dict(v) for k, v in self.store.load().items()}
        self.protected = {self._native_pin(p).key for p in protected}
        for pin in board.gpio_pins():
            self._pin_locks[pin.key] = threading.Lock()
            self._runtime[pin.key] = ChannelRuntime()
            self._channels[pin.key] = create_channel(pin, backend, self._config_for(pin.key))
        self.aliases.load_baseline(aliases or {})

    # ================================================================ lookup
    def _config_for(self, key: str) -> ChannelConfig:
        return self._overlay.get(key) or self._baseline.get(key) or ChannelConfig()

    def _origin(self, key: str) -> str:
        return "runtime" if key in self._overlay else ("config" if key in self._baseline else "default")

    def _pin_by_key(self, key: str) -> HeaderPin:
        h, n = key.split(":")
        return self.board.pin(int(n), h)

    def _describe_key(self, key: str) -> str:
        p = self._pin_by_key(key)
        return f"{self._target_name(p)} ({p.label})"

    def _native_pin(self, ref: str | int) -> HeaderPin:
        t = str(ref).strip()
        m = _GPIO_REF.match(t)
        if m:
            return self.board.pin_by_gpio(int(m.group(1)))
        m = _PIN_REF.match(t)
        if m:
            return self.board.pin(int(m.group(1)))
        m = _HEADER_PIN_REF.match(t)
        if m:
            return self.board.pin(int(m.group(2)), m.group(1))
        raise NotFound(f"{t!r} is not a pin reference", hint=f"targets: {TARGET_FORMS}")

    def _canonical_gpio_key(self, ref: str) -> str:
        """Alias targets must be GPIO pins; aliases of aliases are flattened."""
        pin = self.resolve(ref)
        if pin.kind is not PinKind.GPIO:
            raise InvalidRequest(
                f"cannot alias pin {pin.number}: it is {pin.label} ({pin.kind.value}), not a GPIO",
                hint=f"GPIO pins on this header: {', '.join(self._target_name(p) for p in self.board.gpio_pins())}",
            )
        return pin.key

    def resolve(self, target: str | int) -> HeaderPin:
        t = str(target).strip().strip("/")
        if is_native_ref(t):
            return self._native_pin(t)
        a = self.aliases.get(t)
        if a:
            return self._pin_by_key(a.target)
        sug = self.aliases.suggest(t)
        sug["details"]["target_forms"] = TARGET_FORMS
        raise NotFound(
            f"no pin or alias matches {t!r}",
            hint=sug["hint"] or f"targets: {TARGET_FORMS}; list aliases with `bsp gpio alias list`",
            details=sug["details"],
        )

    def channel(self, target: str | int) -> Channel:
        pin = self.resolve(target)
        if pin.kind is not PinKind.GPIO:
            near = [p for p in self.board.gpio_pins() if abs(p.number - pin.number) <= 2]
            raise InvalidRequest(
                f"pin {pin.number} is {pin.label} ({pin.kind.value}), not a GPIO",
                hint=f"nearby GPIO pins: {', '.join(f'{self._target_name(p)}={p.label}' for p in near)}",
            )
        return self._channels[pin.key]

    # ============================================================== snapshots
    def _target_name(self, pin: HeaderPin) -> str:
        return f"P{pin.number}" if pin.header == self.board.default_header.id else pin.key

    def snapshot(self, pin: HeaderPin) -> dict[str, Any]:
        d = pin.to_dict()
        d["target"] = self._target_name(pin)
        if pin.kind is not PinKind.GPIO:
            return d
        ch = self._channels[pin.key]
        rt = self._runtime[pin.key]
        d.update(
            aliases=[a.name for a in sorted(self.aliases.for_target(pin.key), key=lambda a: a.name.lower())],
            channel=ch.effective_config(),
            origin=self._origin(pin.key),
            actions=list(ch.actions),
            busy=rt.busy,
            protected=pin.key in self.protected,
            pinmux=self.pinmux.get(pin.gpio),  # type: ignore[arg-type]
            last_action=rt.last_action,
        )
        try:
            st = self.backend.status(ch.gpio)
            d.update(line=st.to_dict(), state=ch.describe(st), error=None)
        except BspError as e:
            d.update(line=None, state="error", error=e.to_dict())
        return d

    def list_pins(self, gpio_only: bool = False) -> list[dict[str, Any]]:
        pins = self.board.gpio_pins() if gpio_only else self.board.pins()
        return [self.snapshot(p) for p in pins]

    def get(self, target: str | int) -> dict[str, Any]:
        return self.snapshot(self.resolve(target))

    def board_info(self) -> dict[str, Any]:
        d = self.board.to_dict()
        d["backend"] = self.backend.info()
        d["pinmux"] = self.pinmux.summary()
        d["modes"] = {m.value: {"states": list(c.states), "actions": list(c.actions)} for m, c in
                      ((Mode.GPIO, GpioChannel), (Mode.RELAY, SwitchChannel), (Mode.BUTTON, ButtonChannel))}
        return d

    # ============================================================== mutations
    def _guard(self, ch: Channel, force: bool) -> None:
        if ch.pin.key in self.protected:
            raise Forbidden(
                f"pin {ch.pin.number} ({ch.pin.label}) is protected by the host config",
                hint="protected pins can only be released by editing `protected:` in the host config",
            )
        mux = self.pinmux.get(ch.gpio)
        if mux and mux.get("conflict") and not force:
            raise Conflict(
                f"pin {ch.pin.number} pad is muxed to {mux['mux_owner']} ({mux.get('function')}); GPIO writes will not reach the pin",
                hint="pass force=true to write anyway, or free the pad from that driver / device tree",
                details=mux,
            )

    def _acquire(self, ch: Channel) -> threading.Lock:
        lock = self._pin_locks[ch.pin.key]
        if not lock.acquire(timeout=self.LOCK_WAIT_S):
            busy = self._runtime[ch.pin.key].busy or "operation in progress"
            raise Conflict(
                f"pin {ch.pin.number} is busy ({busy})",
                hint="retry after the running pulse finishes, or use wait=true to serialize pulses",
            )
        return lock

    def _record(self, ch: Channel, action: str, **extra: Any) -> dict[str, Any]:
        rt = self._runtime[ch.pin.key]
        rt.last_action = {"action": action, "ts": time.time(), **extra}
        snap = self.snapshot(ch.pin)
        self.bus.publish(
            "gpio.changed",
            target=snap["target"], key=ch.pin.key, gpio=ch.gpio, aliases=snap.get("aliases") or None,
            mode=ch.mode.value, action=action, state=snap.get("state"), **extra,
        )
        return snap

    def _apply(self, ch: Channel, state: str, action: str, force: bool) -> dict[str, Any]:
        self._guard(ch, force)
        lock = self._acquire(ch)
        try:
            ch.apply(state)
        except BspError as e:
            self.bus.publish("gpio.error", key=ch.pin.key, gpio=ch.gpio, action=action, error=e.to_dict())
            raise
        finally:
            lock.release()
        return self._record(ch, action)

    def _wrong_mode(self, ch: Channel, target: Any, action: str, need: str) -> InvalidRequest:
        return InvalidRequest(
            f"{action} needs {need} mode, but pin {ch.pin.number} ({target}) is in {ch.mode.value} mode",
            hint=f"in {ch.mode.value} mode use: {', '.join(ch.actions)}; or switch: `bsp gpio config {target} --mode {need.split(' ')[0]}`",
            details={"mode": ch.mode.value, "actions": list(ch.actions)},
        )

    def set_level(self, target: str | int, level: str, force: bool = False) -> dict[str, Any]:
        ch = self.channel(target)
        if not isinstance(ch, GpioChannel):
            raise self._wrong_mode(ch, target, f"raw level '{level}'", "gpio")
        return self._apply(ch, level, level, force)

    def switch(self, target: str | int, action: str, force: bool = False) -> dict[str, Any]:
        """on/off/toggle (relay) and press/release (button)."""
        ch = self.channel(target)
        if not isinstance(ch, SwitchChannel):
            raise self._wrong_mode(ch, target, action, "relay or button")
        state = {"on": "on", "off": "off", "press": "on", "release": "off"}.get(action)
        if action == "toggle":
            state = "off" if ch.describe(self.backend.status(ch.gpio)) in ("on", "pressed") else "on"
        if state is None:
            raise InvalidRequest(f"unknown action {action!r}", details={"actions": list(ch.actions)})
        return self._apply(ch, state, action, force)

    def pulse(
        self, target: str | int, ms: int, state: str | None = None, wait: bool = True, force: bool = False
    ) -> dict[str, Any]:
        ch = self.channel(target)
        if not 1 <= ms <= self.max_pulse_ms:
            raise InvalidRequest(
                f"pulse width {ms} ms out of range", hint=f"use 1..{self.max_pulse_ms} ms (host config max_pulse_ms)"
            )
        self._guard(ch, force)
        lock = self._acquire(ch)
        rt = self._runtime[ch.pin.key]
        rt.busy = f"pulse {ms}ms"
        self.bus.publish("gpio.pulse.start", key=ch.pin.key, gpio=ch.gpio, ms=ms)

        def run() -> dict[str, Any]:
            try:
                active, restore = ch.pulse(ms, state)
            except BspError as e:
                self.bus.publish("gpio.error", key=ch.pin.key, gpio=ch.gpio, action="pulse", error=e.to_dict())
                raise
            finally:
                rt.busy = None
                lock.release()
            return self._record(ch, "pulse", ms=ms, active=active, restore=restore)

        if wait:
            return run()
        threading.Thread(target=self._swallow, args=(run,), name=f"pulse-{ch.pin.key}", daemon=True).start()
        snap = self.snapshot(ch.pin)
        snap["pulse_started"] = True
        return snap

    @staticmethod
    def _swallow(fn) -> None:
        try:
            fn()
        except BspError:
            pass  # already published as gpio.error

    def free(self, target: str | int) -> dict[str, Any]:
        ch = self.channel(target)
        if ch.pin.key in self.protected:
            raise Forbidden(f"pin {ch.pin.number} is protected by the host config")
        lock = self._acquire(ch)
        try:
            self.backend.free(ch.gpio)
        finally:
            lock.release()
        return self._record(ch, "free")

    # ================================================================ config
    def configure(self, target: str | int, changes: dict[str, Any], reset: bool = False) -> dict[str, Any]:
        """Update a pin's channel config (runtime overlay, persisted). Mode switch
        swaps the channel object; switching to button releases the line (hi-Z)."""
        ch = self.channel(target)
        key = ch.pin.key
        if reset:
            self._overlay.pop(key, None)
            new_cfg = self._baseline.get(key) or ChannelConfig()
        else:
            merged = ch.config.to_dict()
            if "mode" in changes and changes["mode"] and changes["mode"] != ch.mode.value:
                for k in ("active_low", "drive", "initial"):  # mode-specific options don't carry over
                    merged.pop(k, None)
            for k, v in changes.items():
                if v is None:
                    continue
                if v == "" and k in ("drive", "initial", "description"):
                    merged.pop(k, None)    # empty string clears a field
                else:
                    merged[k] = v
            new_cfg = ChannelConfig.from_dict(merged)
        lock = self._acquire(ch)
        try:
            new_ch = create_channel(ch.pin, self.backend, new_cfg)
            self._channels[key] = new_ch
            if not reset:
                self._overlay[key] = new_cfg
            self.store.save({k: v.to_dict() for k, v in self._overlay.items()})
            if isinstance(new_ch, ButtonChannel) and not isinstance(ch, ButtonChannel) and key not in self.protected:
                new_ch.apply("off")
        finally:
            lock.release()
        self.bus.publish("gpio.config", key=key, gpio=ch.gpio, config=new_ch.effective_config(), origin=self._origin(key))
        return self._record(new_ch, "config")

    def export_config(self) -> dict[str, Any]:
        """Channel configs + runtime aliases in host-config YAML shape, ready to commit."""
        chans = {self._target_name(ch.pin): ch.config.to_dict() for key, ch in self._channels.items()
                 if self._origin(key) != "default"}
        aliases: dict[str, Any] = {}
        for a in self.aliases.list():
            tgt = self._target_name(self._pin_by_key(a.target))
            aliases[a.name] = {"target": tgt, **({"tags": a.tags} if a.tags else {}), **({"note": a.note} if a.note else {})} \
                if (a.tags or a.note) else tgt
        return {"channels": chans, "aliases": aliases}

    # ================================================================ aliases
    def _alias_view(self, a: Alias) -> dict[str, Any]:
        d = a.to_dict()
        pin = self._pin_by_key(a.target)
        d.update(pin=self._target_name(pin), gpio=pin.gpio, label=pin.label, mode=self._channels[pin.key].mode.value)
        return d

    def alias_list(self, prefix: str | None = None, tag: str | None = None, target: str | None = None) -> list[dict[str, Any]]:
        key = self.resolve(target).key if target else None
        return [self._alias_view(a) for a in self.aliases.list(prefix, tag, key)]

    def alias_add(self, name: str, target: str, tags: str | None = None, note: str = "",
                  owner: str | None = None, replace: bool = False) -> dict[str, Any]:
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
        a = self.aliases.add(name, target, tags=tag_list, note=note or "", owner=owner, replace=replace)
        view = self._alias_view(a)
        self.bus.publish("gpio.alias.added", alias=a.name, pin=view["pin"], gpio=view["gpio"], tags=a.tags or None, owner=owner)
        return view

    def alias_delete(self, name: str, prefix: bool = False) -> list[dict[str, Any]]:
        gone = self.aliases.delete(name, prefix)
        for a in gone:
            self.bus.publish("gpio.alias.deleted", alias=a.name, target=a.target)
        return [a.to_dict() for a in gone]

    def alias_resolve(self, name: str) -> dict[str, Any]:
        pin = self.resolve(name)
        a = self.aliases.get(name)
        return {"input": name, "alias": a.to_dict() if a else None, "native": a is None, "pin": self.snapshot(pin)}

    # =============================================================== lifecycle
    def startup(self) -> None:
        self.pinmux.refresh()
        for key, ch in self._channels.items():
            if self._origin(key) == "default" or not ch.initial or key in self.protected:
                continue
            try:
                ch.apply(ch.initial)
                self._record(ch, "initial", state_applied=ch.initial)
            except BspError as e:
                self.bus.publish("gpio.error", key=key, gpio=ch.gpio, action="initial", error=e.to_dict())

    def shutdown(self) -> None:
        self.backend.close()

    def refresh_pinmux(self) -> dict[str, Any]:
        s = self.pinmux.refresh()
        self.bus.publish("gpio.pinmux", **s)
        conflicts = {}
        for pin in self.board.gpio_pins():
            mux = self.pinmux.get(pin.gpio)  # type: ignore[arg-type]
            if mux and mux.get("conflict"):
                conflicts[self._target_name(pin)] = mux
        return {**s, "conflicts": conflicts}
