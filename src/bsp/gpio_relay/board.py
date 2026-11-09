"""Board profiles: physical header pin maps, decoupled from code.

A profile is pure data (``<site>/boards/<id>.yaml``). Adding a new board means
adding a YAML file; the pin layout geometry is a pluggable strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator

from ..core.errors import InvalidRequest, NotFound
from ..core.registry import Registry


class PinKind(str, Enum):
    GPIO = "gpio"
    POWER = "power"
    GND = "gnd"
    NC = "nc"
    OTHER = "other"


@dataclass(frozen=True)
class HeaderPin:
    header: str
    number: int
    kind: PinKind
    label: str
    gpio: int | None = None
    alt: str | None = None      # board-level annotation, e.g. "R.UART0 TXD"
    row: int = 0
    col: int = 0

    @property
    def key(self) -> str:
        return f"{self.header}:{self.number}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "header": self.header,
            "number": self.number,
            "kind": self.kind.value,
            "label": self.label,
            "gpio": self.gpio,
            "alt": self.alt,
            "row": self.row,
            "col": self.col,
        }


# ------------------------------------------------------------ layouts -----
# A layout maps (pin number, pin count) -> (row, col). Strategy registry so a
# board with a different connector geometry only needs a new entry here.
Layout = Callable[[int, int], "tuple[int, int]"]
LAYOUTS: Registry[Layout] = Registry("header layout")
LAYOUTS.register("dual-row", lambda n, total: ((n - 1) // 2, (n - 1) % 2))        # 1|2, 3|4 ... (RPi style)
LAYOUTS.register("dual-row-split", lambda n, total: ((n - 1) % (total // 2), (n - 1) // (total // 2)))  # 1..N/2 | N/2+1..N
LAYOUTS.register("single-row", lambda n, total: (n - 1, 0))


@dataclass
class Header:
    id: str
    title: str
    layout: str
    pins: dict[int, HeaderPin]
    rows: int
    cols: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "layout": self.layout,
            "rows": self.rows,
            "cols": self.cols,
            "pin_count": len(self.pins),
        }


@dataclass
class BoardProfile:
    id: str
    name: str
    description: str = ""
    headers: list[Header] = field(default_factory=list)
    pinctrl: dict[str, Any] = field(default_factory=dict)
    gpio: dict[str, Any] = field(default_factory=dict)

    # -------------------------------------------------------- lookups --
    @property
    def default_header(self) -> Header:
        return self.headers[0]

    def header(self, hid: str) -> Header:
        for h in self.headers:
            if h.id.lower() == hid.lower():
                return h
        raise NotFound(f"board {self.id} has no header '{hid}'", details={"headers": [h.id for h in self.headers]})

    def pins(self) -> Iterator[HeaderPin]:
        for h in self.headers:
            yield from (h.pins[n] for n in sorted(h.pins))

    def gpio_pins(self) -> Iterator[HeaderPin]:
        return (p for p in self.pins() if p.kind is PinKind.GPIO)

    def pin(self, number: int, header: str | None = None) -> HeaderPin:
        h = self.header(header) if header else self.default_header
        try:
            return h.pins[number]
        except KeyError:
            raise NotFound(f"header {h.id} has no pin {number}", hint=f"valid pins: 1..{len(h.pins)}") from None

    def pin_by_gpio(self, gpio: int) -> HeaderPin:
        for p in self.gpio_pins():
            if p.gpio == gpio:
                return p
        raise NotFound(
            f"GPIO{gpio} is not routed to any header of board {self.id}",
            details={"routed": sorted(p.gpio for p in self.gpio_pins())},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "headers": [h.to_dict() for h in self.headers],
            "pins": [p.to_dict() for p in self.pins()],
        }

    # -------------------------------------------------------- loading --
    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BoardProfile":
        headers = []
        for hd in d.get("headers", []):
            hid = str(hd["id"])
            raw_pins = {int(k): (v or {}) for k, v in (hd.get("pins") or {}).items()}
            total = int(hd.get("pin_count", max(raw_pins) if raw_pins else 0))
            layout_name = hd.get("layout", "dual-row")
            layout = LAYOUTS.get(layout_name)
            pins: dict[int, HeaderPin] = {}
            for n in range(1, total + 1):
                spec = raw_pins.get(n, {"kind": "nc"})
                if isinstance(spec, int):           # shorthand: `3: 83` means GPIO83
                    spec = {"gpio": spec}
                gpio = spec.get("gpio")
                kind = PinKind(spec.get("kind", "gpio" if gpio is not None else "nc"))
                if kind is PinKind.GPIO and gpio is None:
                    raise InvalidRequest(f"board {d.get('id')}: header {hid} pin {n} is gpio without a gpio number")
                label = spec.get("label") or (f"GPIO{gpio}" if kind is PinKind.GPIO else kind.value.upper())
                row, col = layout(n, total)
                pins[n] = HeaderPin(hid, n, kind, str(label), gpio, spec.get("alt"), row, col)
            rows = max((p.row for p in pins.values()), default=-1) + 1
            cols = max((p.col for p in pins.values()), default=-1) + 1
            headers.append(Header(hid, hd.get("title", hid), layout_name, pins, rows, cols))
        gpios = [p.gpio for h in headers for p in h.pins.values() if p.gpio is not None]
        dup = {g for g in gpios if gpios.count(g) > 1}
        if dup:
            raise InvalidRequest(f"board {d.get('id')}: GPIO(s) {sorted(dup)} routed to multiple pins")
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            description=d.get("description", ""),
            headers=headers,
            pinctrl=d.get("pinctrl") or {},
            gpio=d.get("gpio") or {},
        )

    @classmethod
    def load(cls, path: Path) -> "BoardProfile":
        import yaml

        with open(path, encoding="utf-8") as f:
            return cls.from_dict(yaml.safe_load(f))


class BoardCatalog:
    """All board profiles found in a directory, keyed by profile id."""

    def __init__(self, directories: "Path | list[Path]"):
        dirs = directories if isinstance(directories, (list, tuple)) else [directories]
        self._boards: Registry[BoardProfile] = Registry("board profile")
        for d in dirs:                                   # later dirs override earlier ones
            for f in sorted(Path(d).glob("*.y*ml")):
                b = BoardProfile.load(f)
                self._boards.register(b.id, b)

    def get(self, board_id: str) -> BoardProfile:
        return self._boards.get(board_id)

    def ids(self) -> list[str]:
        return self._boards.names()
