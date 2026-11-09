"""GPIO backend contract (strategy). Backends speak Linux-global GPIO numbers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class LineStatus:
    gpio: int
    claimed: bool                 # requested/exported (by us or someone else)
    direction: str | None = None  # "in" | "out" | None (unknown)
    value: int | None = None      # physical level 0/1, None if not readable without claiming
    owner: str | None = None      # consumer / "sysfs" when known

    def to_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed,
            "direction": self.direction,
            "value": self.value,
            "owner": self.owner,
        }


class GpioBackend(ABC):
    """Physical-level line access. Polarity, drive emulation and semantics
    (relay/button) live in channels, not here."""

    name: str = "abstract"

    @abstractmethod
    def set_output(self, gpio: int, value: int) -> None:
        """Claim if needed and drive the line as output at ``value`` (glitch-free)."""

    @abstractmethod
    def set_input(self, gpio: int) -> None:
        """Claim if needed and switch the line to input (high impedance)."""

    @abstractmethod
    def status(self, gpio: int) -> LineStatus:
        """Inspect the line without claiming it or changing anything."""

    @abstractmethod
    def free(self, gpio: int) -> None:
        """Give the line back (input first, then unexport/release)."""

    def close(self) -> None:
        """Service shutdown. Default keeps lines as they are so DUT state survives restarts."""

    def info(self) -> dict[str, Any]:
        return {"name": self.name}
