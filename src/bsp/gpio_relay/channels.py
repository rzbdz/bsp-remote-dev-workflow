"""Channel modes: the semantic layer on top of a raw GPIO line.

    gpio    raw level control: high / low / input (hi-Z)
    relay   switch semantics: on / off, configurable polarity and drive
    button  momentary switch wired to a DUT button net with a shared GND:
            press = drive low, release = hi-Z (open-drain emulation, safe even if
            the DUT net uses a lower I/O voltage than the host)

Each mode is a subclass registered in ``CHANNEL_MODES``; the mode of a pin can be
switched at runtime by swapping its channel object (state pattern).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

from ..core.errors import InvalidRequest
from ..core.registry import Registry
from .backends.base import GpioBackend, LineStatus
from .board import HeaderPin


class Mode(str, Enum):
    GPIO = "gpio"
    RELAY = "relay"
    BUTTON = "button"


class Drive(str, Enum):
    PUSH_PULL = "push_pull"     # inactive level is actively driven
    OPEN_DRAIN = "open_drain"   # inactive = input / hi-Z


@dataclass
class ChannelConfig:
    mode: Mode = Mode.GPIO
    active_low: bool | None = None   # None -> mode default
    drive: Drive | None = None       # None -> mode default
    initial: str | None = None       # applied at service start; None = leave line untouched
    description: str = ""

    FIELDS: ClassVar[tuple[str, ...]] = ("mode", "active_low", "drive", "initial", "description")

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ChannelConfig":
        d = dict(d or {})
        unknown = set(d) - set(cls.FIELDS)
        if unknown:
            raise InvalidRequest(
                f"unknown channel config field(s): {sorted(unknown)}",
                hint="names are aliases now: `bsp gpio alias add <name> <pin>`" if "name" in unknown else None,
                details={"allowed": cls.FIELDS},
            )
        try:
            mode = Mode(d.get("mode") or "gpio")
            drive = Drive(d["drive"]) if d.get("drive") else None
        except ValueError as e:
            raise InvalidRequest(str(e), details={"modes": [m.value for m in Mode], "drives": [x.value for x in Drive]}) from None
        cfg = cls(mode, d.get("active_low"), drive, d.get("initial") or None, d.get("description") or "")
        CHANNEL_MODES.get(mode.value).validate_initial(cfg.initial)
        return cfg

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"mode": self.mode.value}
        if self.active_low is not None:
            d["active_low"] = self.active_low
        if self.drive is not None:
            d["drive"] = self.drive.value
        if self.initial:
            d["initial"] = self.initial
        if self.description:
            d["description"] = self.description
        return d


class Channel(ABC):
    mode: ClassVar[Mode]
    states: ClassVar[tuple[str, ...]]           # states accepted by apply()
    actions: ClassVar[tuple[str, ...]]          # verbs offered to UIs
    default_initial: ClassVar[str | None] = None

    def __init__(self, pin: HeaderPin, backend: GpioBackend, config: ChannelConfig):
        assert pin.gpio is not None
        self.pin = pin
        self.backend = backend
        self.config = config

    @property
    def gpio(self) -> int:
        return self.pin.gpio  # type: ignore[return-value]

    @classmethod
    def validate_initial(cls, initial: str | None) -> None:
        if initial is not None and initial not in cls.states:
            raise InvalidRequest(
                f"initial state for mode {cls.mode.value} must be one of {list(cls.states)}, got {initial!r}",
                details={"mode": cls.mode.value, "states": list(cls.states)},
            )

    @property
    def initial(self) -> str | None:
        return self.config.initial or self.default_initial

    def effective_config(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "initial": self.initial,
            "description": self.config.description,
        }

    @abstractmethod
    def apply(self, state: str) -> None: ...

    @abstractmethod
    def describe(self, st: LineStatus) -> str: ...

    @abstractmethod
    def pulse_plan(self, st: LineStatus, state: str | None) -> tuple[str, str]:
        """(state during pulse, state to restore afterwards)."""

    def pulse(self, ms: int, state: str | None = None) -> tuple[str, str]:
        active, restore = self.pulse_plan(self.backend.status(self.gpio), state)
        self.apply(active)
        try:
            time.sleep(ms / 1000.0)
        finally:
            self.apply(restore)
        return active, restore

    def _check_state(self, state: str) -> None:
        if state not in self.states:
            raise InvalidRequest(
                f"pin {self.pin.number} is in {self.mode.value} mode, which accepts {list(self.states)}, not {state!r}",
                hint=f"use one of {list(self.states)}, or change the mode: `bsp gpio config P{self.pin.number} --mode <gpio|relay|button>`",
                details={"mode": self.mode.value, "states": list(self.states), "actions": list(self.actions)},
            )


CHANNEL_MODES: Registry[type[Channel]] = Registry("channel mode")


@CHANNEL_MODES.register(Mode.GPIO.value)
class GpioChannel(Channel):
    mode = Mode.GPIO
    states = ("high", "low", "input")
    actions = ("high", "low", "input", "pulse", "free")

    def apply(self, state: str) -> None:
        self._check_state(state)
        if state == "input":
            self.backend.set_input(self.gpio)
        else:
            self.backend.set_output(self.gpio, 1 if state == "high" else 0)

    def describe(self, st: LineStatus) -> str:
        if not st.claimed:
            return "unclaimed"
        if st.direction == "in":
            return "input"
        return "high" if st.value else "low"

    def pulse_plan(self, st: LineStatus, state: str | None) -> tuple[str, str]:
        active = state or "low"
        self._check_state(active)
        current = self.describe(st)
        return active, current if current in self.states else "input"


@CHANNEL_MODES.register(Mode.RELAY.value)
class SwitchChannel(Channel):
    mode = Mode.RELAY
    states = ("on", "off")
    actions = ("on", "off", "toggle", "pulse", "free")
    default_active_low = False
    default_drive = Drive.PUSH_PULL

    @property
    def active_low(self) -> bool:
        return self.default_active_low if self.config.active_low is None else self.config.active_low

    @property
    def drive(self) -> Drive:
        return self.config.drive or self.default_drive

    @property
    def active_level(self) -> int:
        return 0 if self.active_low else 1

    def effective_config(self) -> dict[str, Any]:
        d = super().effective_config()
        d.update(active_low=self.active_low, drive=self.drive.value)
        return d

    def apply(self, state: str) -> None:
        self._check_state(state)
        if state == "on":
            self.backend.set_output(self.gpio, self.active_level)
        elif self.drive is Drive.OPEN_DRAIN:
            self.backend.set_input(self.gpio)
        else:
            self.backend.set_output(self.gpio, 1 - self.active_level)

    def describe(self, st: LineStatus) -> str:
        if not st.claimed:
            return "unknown"
        if st.direction == "in":
            return "off" if self.drive is Drive.OPEN_DRAIN else "floating"
        return "on" if st.value == self.active_level else "off"

    def pulse_plan(self, st: LineStatus, state: str | None) -> tuple[str, str]:
        active = state or "on"
        self._check_state(active)
        return active, "off" if active == "on" else "on"


@CHANNEL_MODES.register(Mode.BUTTON.value)
class ButtonChannel(SwitchChannel):
    mode = Mode.BUTTON
    actions = ("press", "release", "click", "free")
    default_active_low = True
    default_drive = Drive.OPEN_DRAIN
    default_initial = "off"   # released (hi-Z) is always safe

    def describe(self, st: LineStatus) -> str:
        s = super().describe(st)
        return {"on": "pressed", "off": "released"}.get(s, s)


def create_channel(pin: HeaderPin, backend: GpioBackend, config: ChannelConfig) -> Channel:
    return CHANNEL_MODES.get(config.mode.value)(pin, backend, config)


@dataclass
class ChannelRuntime:
    """Mutable runtime bits the service tracks per pin (busy pulses etc.)."""

    busy: str | None = None
    last_action: dict[str, Any] | None = field(default=None)
