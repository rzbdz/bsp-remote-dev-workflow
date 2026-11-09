"""Keep this host's copy of the registry's SDK catalog current.

The registry answers every announcement with its catalog revision and nudges hosts right after
a change. When the revision differs from the copy kept in <state>/catalog, the host fetches the
snapshot, writes it there in one step and publishes `catalog.updated`, and the modules reload
their catalogs. The copy outlives restarts and outages: without the registry the host keeps the
last catalog it had. Jobs already running keep the recipe they started with.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from pathlib import Path
from typing import Any

from ..registry.catalog import layer_rev, materialize

log = logging.getLogger(__name__)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CatalogFollower:
    def __init__(self, registry_url: str, layer: Path, bus, site_roots: list[Path] | None = None):
        self.url = registry_url.rstrip("/") + "/api/v1/catalog/snapshot"
        self.layer, self.bus = Path(layer), bus
        self.site_roots = [Path(r) for r in (site_roots or [])]
        self.rev = layer_rev(self.layer)
        self.last: dict[str, Any] = {}
        self.shadowed: list[dict[str, Any]] = self.find_shadowed()
        self._lock = threading.Lock()

    def find_shadowed(self) -> list[dict[str, Any]]:
        """This host's site files that differ from the registry's document with the same id: edits made here that
        the registry's catalog overrides, so they have no effect until someone puts them into the registry."""
        import yaml

        from ..registry.catalog import ref_of
        from ..sdk.catalog import CATALOG_KINDS

        out = []
        if not self.layer.is_dir():
            return out
        for root in self.site_roots:
            for kind in CATALOG_KINDS:
                for p in sorted((root / kind).glob("*.yaml")):
                    try:
                        site = yaml.safe_load(p.read_text()) or {}
                        ident = site.get("id") or p.stem
                        reg = self.layer / kind / f"{ident}.yaml"
                        if reg.exists() and yaml.safe_load(reg.read_text()) != site:
                            out.append({"ref": ref_of(kind, ident), "file": str(p), "modified": int(p.stat().st_mtime)})
                    except Exception:  # noqa: BLE001 - a broken site file is reported elsewhere
                        continue
        return out

    def sync(self, reset: bool = False) -> dict[str, Any]:
        """Fetch the registry's catalog when its revision differs; with `reset`, throw the local copy away and fetch
        it again whatever the revision."""
        with self._lock:
            if reset:
                import shutil

                shutil.rmtree(self.layer, ignore_errors=True)
                self.rev = 0
            try:
                with _OPENER.open(urllib.request.Request(self.url, headers={"Accept": "application/json"}), timeout=15) as r:
                    snap = json.loads(r.read())["result"]
            except Exception as e:  # noqa: BLE001 - keep the copy we have
                self.last = {"ok": False, "error": str(e), "rev": self.rev}
                log.warning("catalog: cannot fetch %s: %s (keeping revision %s)", self.url, e, self.rev)
                return self.last
            changed = snap["rev"] != self.rev
            if changed:
                materialize(snap, self.layer)
                old, self.rev = self.rev, snap["rev"]
                self.bus.publish("catalog.updated", rev=self.rev, previous=old, documents=len(snap["docs"]))
            self.shadowed = self.find_shadowed()
            if self.shadowed:
                log.warning("catalog: site edits overridden by the registry, not in effect: %s",
                            ", ".join(f"{x['ref']} ({x['file']})" for x in self.shadowed))
            self.last = {"ok": True, "rev": self.rev, "changed": changed, "shadowed": self.shadowed}
            return self.last

    def check(self, remote_rev: Any) -> None:
        """The registry said its revision; fetch in the background when ours differs."""
        try:
            if int(remote_rev) != self.rev:
                threading.Thread(target=self.sync, name="catalog sync", daemon=True).start()
        except (TypeError, ValueError):
            pass
