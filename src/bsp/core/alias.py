"""Hierarchical alias table — the pass-through building block every component shares.

A component (gpio-relay, serial, fastboot, ...) only resolves its own aliases.
Downstream layers register meaning on top: e.g. a DUT manager registers
``DUT3/reset -> P11`` on the gpio-relay host, then forwards ``DUT3/reset`` calls
untouched. Aliases are path-like (``DUT3/reset``), case-insensitive, may carry
tags, and are either *config* (host-config baseline, read-only via API) or
*runtime* (added through the API, persisted).
"""

from __future__ import annotations

import difflib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .errors import Conflict, Forbidden, InvalidRequest, NotFound

_SEG = r"[A-Za-z0-9][A-Za-z0-9_.+-]*"
ALIAS_RE = re.compile(rf"^{_SEG}(?:/{_SEG})*$")
MAX_LEN = 128


@dataclass
class Alias:
    name: str
    target: str                     # canonical target key of the owning component
    tags: list[str] = field(default_factory=list)
    note: str = ""
    owner: str | None = None        # who registered it (free text, e.g. "dut-manager")
    origin: str = "runtime"         # runtime | config
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AliasTable:
    def __init__(
        self,
        canonicalize: Callable[[str], str],
        *,
        path: Path | None = None,
        reserved: Callable[[str], bool] = lambda n: False,
        describe: Callable[[str], str] = lambda t: t,
    ):
        """``canonicalize(ref)`` turns any accepted target ref into the component's
        canonical key (raising a helpful error if invalid). ``reserved(name)`` flags
        names that would shadow native refs (``P12``, ``GPIO5``...)."""
        self._canon = canonicalize
        self._reserved = reserved
        self._describe = describe
        self.path = path
        self._lock = threading.RLock()
        self._items: dict[str, Alias] = {}       # lower(name) -> Alias
        if path and path.exists():
            for d in json.loads(path.read_text(encoding="utf-8")).get("aliases", []):
                a = Alias(**d)
                self._items[a.name.lower()] = a

    # ------------------------------------------------------------- helpers --
    def validate_name(self, name: str) -> str:
        name = (name or "").strip().strip("/")
        if not name or len(name) > MAX_LEN or not ALIAS_RE.match(name):
            raise InvalidRequest(
                f"invalid alias name {name!r}",
                hint="path-like segments of letters/digits/_.+- separated by '/', e.g. DUT3/reset (max 128 chars)",
            )
        if self._reserved(name):
            raise InvalidRequest(
                f"alias {name!r} would shadow a native target reference",
                hint="native refs like 33, P33, GPIO127 always resolve directly; choose a descriptive name such as DUT3/reset",
            )
        return name

    def _save(self) -> None:
        if not self.path:
            return
        runtime = [a.to_dict() for a in self._items.values() if a.origin == "runtime"]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"aliases": runtime}, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # ---------------------------------------------------------------- API --
    def load_baseline(self, entries: dict[str, Any]) -> None:
        """Host-config aliases: ``{name: target}`` or ``{name: {target, tags, note}}``."""
        with self._lock:
            for name, spec in (entries or {}).items():
                spec = {"target": spec} if not isinstance(spec, dict) else spec
                name = self.validate_name(str(name))
                self._items[name.lower()] = Alias(
                    name, self._canon(str(spec["target"])), list(spec.get("tags") or []),
                    spec.get("note", ""), "host-config", "config",
                )

    def add(self, name: str, target: str, *, tags: list[str] | None = None, note: str = "",
            owner: str | None = None, replace: bool = False) -> Alias:
        name = self.validate_name(name)
        canon = self._canon(target)
        with self._lock:
            old = self._items.get(name.lower())
            if old and old.origin == "config":
                raise Forbidden(
                    f"alias {old.name!r} is defined in the host config and cannot be changed via API",
                    hint="edit the `aliases:` section of the host config file, or register a different name",
                    details=old.to_dict(),
                )
            if old and not replace:
                if old.target == canon:
                    return old                         # idempotent re-registration
                raise Conflict(
                    f"alias {old.name!r} already points to {self._describe(old.target)}",
                    hint="pass replace=true to re-point it, or delete it first",
                    details=old.to_dict(),
                )
            a = Alias(name, canon, sorted(set(tags or [])), note or "", owner)
            self._items[name.lower()] = a
            self._save()
            return a

    def delete(self, name: str, prefix: bool = False) -> list[Alias]:
        key = (name or "").strip().strip("/").lower()
        with self._lock:
            if prefix:
                victims = [a for k, a in self._items.items() if k == key or k.startswith(key + "/")]
            else:
                victims = [self._items[key]] if key in self._items else []
            if not victims:
                raise NotFound(f"no alias {'under prefix ' if prefix else ''}{name!r}", **self.suggest(name))
            locked = [a.name for a in victims if a.origin == "config"]
            if locked:
                raise Forbidden(
                    f"alias(es) {locked} are defined in the host config and cannot be deleted via API",
                    hint="edit the `aliases:` section of the host config file",
                )
            for a in victims:
                del self._items[a.name.lower()]
            self._save()
            return victims

    def get(self, name: str) -> Alias | None:
        return self._items.get((name or "").strip().strip("/").lower())

    def list(self, prefix: str | None = None, tag: str | None = None, target: str | None = None) -> list[Alias]:
        p = (prefix or "").strip().strip("/").lower()
        out = []
        for k, a in sorted(self._items.items()):
            if p and not (k == p or k.startswith(p + "/")):
                continue
            if tag and tag not in a.tags:
                continue
            if target and a.target != target:
                continue
            out.append(a)
        return out

    def for_target(self, canonical: str) -> list[Alias]:
        return [a for a in self._items.values() if a.target == canonical]

    def suggest(self, name: str) -> dict[str, Any]:
        """``hint``/``details`` kwargs for a helpful not-found error."""
        key = (name or "").strip().strip("/").lower()
        names = [a.name for a in self._items.values()]
        children = [a.name for a in self.list(prefix=key)] if key else []
        close = difflib.get_close_matches(name, names, n=5, cutoff=0.6) if name else []
        hint = None
        if children:
            hint = f"{name!r} is an alias prefix; pick one of: {', '.join(children[:10])}"
        elif close:
            hint = f"did you mean: {', '.join(close)}?"
        return {"hint": hint, "details": {"did_you_mean": close, "children": children, "known_aliases": len(names)}}
