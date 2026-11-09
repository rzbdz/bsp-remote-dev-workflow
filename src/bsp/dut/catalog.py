"""DUT catalog: one YAML per board under <site>/duts/, shared by the CLI (to route
`bsp dut ... <id>` to the right host) and by each host (to drive its own boards).

A DUT is the 4-tuple from the design: host + control (reset / FDL) + fastboot USB
port + serial console, plus what it can run (sdks).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import InvalidRequest, NotFound


@dataclass
class DutSpec:
    id: str
    host: str
    board: str
    soc: str
    description: str
    console: dict[str, Any]
    fastboot: dict[str, Any]
    control: dict[str, Any]
    sdks: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def usb(self) -> str:
        return self.fastboot["usb"]

    @property
    def console_alias(self) -> str:
        return self.console["alias"]

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id, "host": self.host, "board": self.board, "soc": self.soc,
            "description": self.description, "usb": self.usb, "console": self.console_alias,
            "control": self.control.get("backend"), "fdl": (self.control.get("fdl") or {}).get("strategy"),
            "sdks": self.sdks,
        }


def spec_from_dict(d: dict[str, Any], where: str = "DUT") -> DutSpec:
    for key in ("id", "host", "console", "fastboot", "control"):
        if key not in d:
            raise InvalidRequest(f"{where}: DUT is missing `{key}`")
    if "usb" not in d["fastboot"]:
        raise InvalidRequest(f"{where}: fastboot.usb (e.g. usb:2-1.2 from `fastboot devices -l`) is required")
    if "alias" not in d["console"]:
        raise InvalidRequest(f"{where}: console.alias (serial-mux alias) is required")
    return DutSpec(d["id"], d["host"], d.get("board", ""), d.get("soc", ""), d.get("description", ""),
                   d["console"], d["fastboot"], d["control"], list(d.get("sdks") or []), d)


def _load(p: Path) -> DutSpec:
    import yaml

    return spec_from_dict(yaml.safe_load(p.read_text(encoding="utf-8")) or {}, str(p))


class DutCatalog:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.duts: dict[str, DutSpec] = {}
        for p in sorted(self.directory.glob("*.yaml")):
            spec = _load(p)
            if spec.id in self.duts:
                raise InvalidRequest(f"duplicate DUT id {spec.id!r} ({p})")
            self.duts[spec.id] = spec

    def get(self, dut_id: str) -> DutSpec:
        if dut_id in self.duts:
            return self.duts[dut_id]
        close = difflib.get_close_matches(dut_id, list(self.duts), n=3, cutoff=0.4)
        raise NotFound(
            f"unknown DUT {dut_id!r}",
            hint=(f"did you mean: {', '.join(close)}? " if close else "")
            + f"known DUTs: {', '.join(sorted(self.duts)) or '(none)'}; list them with `bsp dut list`",
        )

    def on_host(self, host_id: str) -> list[DutSpec]:
        return [d for d in self.duts.values() if d.host == host_id]


class DutRegistry:
    """The DUTs one host drives: static entries from the repo catalog for this
    host, plus entries registered at runtime (``bsp probe`` / ``bsp dut add``),
    persisted in the host state dir. Runtime entries win on id clash."""

    def __init__(self, host_id: str, catalog_dir: Path | None, state_file: Path):
        self.host_id = host_id
        self.state_file = Path(state_file)
        self.static = {d.id: d for d in DutCatalog(catalog_dir).on_host(host_id)} if catalog_dir and Path(catalog_dir).is_dir() else {}
        self.dynamic: dict[str, DutSpec] = {}
        if self.state_file.exists():
            import json

            for d in json.loads(self.state_file.read_text()).get("duts", []):
                spec = spec_from_dict(d, str(self.state_file))
                self.dynamic[spec.id] = spec

    def all(self) -> dict[str, DutSpec]:
        return {**self.static, **self.dynamic}

    def origin(self, dut_id: str) -> str:
        return "probed" if dut_id in self.dynamic else "catalog"

    def add(self, raw: dict[str, Any], replace: bool = False) -> DutSpec:
        from ..core.errors import Conflict

        raw = {**raw, "host": self.host_id}
        spec = spec_from_dict(raw)
        if spec.id in self.all() and not replace:
            raise Conflict(f"DUT {spec.id} already exists on {self.host_id}", hint="pick another name or pass replace=true")
        for other in self.all().values():
            if other.id != spec.id and (other.usb == spec.usb or other.console_alias == spec.console_alias):
                raise Conflict(f"{other.id} already uses usb {other.usb} / console {other.console_alias}",
                               hint=f"remove it first (`bsp dut remove {other.id}`) or replace it")
        self.dynamic[spec.id] = spec
        self._save()
        return spec

    def update(self, dut_id: str, raw: dict[str, Any]) -> DutSpec:
        """Replace a runtime DUT's spec (same or new id); validates like add()."""
        from ..core.errors import Conflict

        if dut_id not in self.dynamic:
            if dut_id in self.static:
                raise InvalidRequest(f"{dut_id} comes from the site catalog (<site>/duts); edit it there")
            raise NotFound(f"unknown DUT {dut_id!r}", hint=f"known: {', '.join(sorted(self.all()))}")
        spec = spec_from_dict({**raw, "host": self.host_id})
        if spec.id != dut_id and spec.id in self.all():
            raise Conflict(f"DUT {spec.id} already exists on {self.host_id}")
        for other in self.all().values():
            if other.id not in (dut_id, spec.id) and (other.usb == spec.usb or other.console_alias == spec.console_alias):
                raise Conflict(f"{other.id} already uses usb {other.usb} / console {other.console_alias}")
        del self.dynamic[dut_id]
        self.dynamic[spec.id] = spec
        self._save()
        return spec

    def remove(self, dut_id: str) -> DutSpec:
        if dut_id not in self.dynamic:
            if dut_id in self.static:
                raise InvalidRequest(f"{dut_id} comes from the site catalog (<site>/duts); edit it there")
            raise NotFound(f"unknown DUT {dut_id!r}", hint=f"known: {', '.join(sorted(self.all()))}")
        spec = self.dynamic.pop(dut_id)
        self._save()
        return spec

    def _save(self) -> None:
        import json

        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"duts": [d.raw for d in self.dynamic.values()]}, indent=2))
        tmp.replace(self.state_file)
