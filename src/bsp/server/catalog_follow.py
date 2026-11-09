"""Keep this host's copy of the registry's SDK catalog, SoC profiles and hooks current.

SDK documents go to <state>/catalog; the shared files (SoC profiles, hooks and the images they
stage, see registry/files.py) to <state>/catalog-files, a layer above the host's own site.

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
    def __init__(self, registry_url: str, layer: Path, bus, site_roots: list[Path] | None = None,
                 token: str | None = None):
        self.base = registry_url.rstrip("/")
        self.url = self.base + "/api/v1/catalog/snapshot"
        self.layer, self.bus = Path(layer), bus
        self.files_layer = self.layer.with_name(self.layer.name + "-files")
        self.token = token
        self.site_roots = [Path(r) for r in (site_roots or [])]
        self.rev = layer_rev(self.layer)
        from ..registry.files import layer_digest

        self.files_digest = layer_digest(self.files_layer)
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
            files = self.sync_files(reset)
            self.shadowed = self.find_shadowed()
            if self.shadowed:
                log.warning("catalog: site edits overridden by the registry, not in effect: %s",
                            ", ".join(f"{x['ref']} ({x['file']})" for x in self.shadowed))
            self.last = {"ok": True, "rev": self.rev, "changed": changed, "shadowed": self.shadowed,
                         "files_digest": self.files_digest, "files": files}
            return self.last

    def _get(self, path: str) -> bytes:
        req = urllib.request.Request(self.base + path)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with _OPENER.open(req, timeout=60) as r:
            return r.read()

    def sync_files(self, reset: bool = False) -> dict[str, Any]:
        """Fetch the registry's SoC profiles and hooks when they changed (a registry without them: nothing)."""
        from ..registry.files import materialize as write_files

        try:
            manifest = json.loads(self._get("/api/v1/registry/files"))["result"]
        except Exception as e:  # noqa: BLE001 - keep the files we have
            log.warning("catalog files: cannot fetch from %s: %s (keeping %s)", self.base, e, self.files_digest[:12] or "none")
            return {"ok": False, "error": str(e)}
        if manifest["digest"] == self.files_digest and not reset:
            return {"ok": True, "changed": False}
        res = write_files(manifest, self.files_layer, lambda sha: self._get(f"/api/v1/registry/blobs/{sha}"),
                          lambda line: log.info("%s", line))
        old, self.files_digest = self.files_digest, manifest["digest"]
        self.bus.publish("catalog.updated", rev=self.rev, files=manifest["digest"][:12], previous_files=old[:12])
        if res["missing"]:
            log.warning("catalog files: images the registry does not have: %s", res["missing"])
        return {"ok": True, "changed": True, **res}

    def check(self, remote_rev: Any, files_digest: Any = None) -> None:
        """The registry said its revision; fetch in the background when ours differs."""
        try:
            if int(remote_rev) != self.rev or (files_digest and files_digest != self.files_digest):
                threading.Thread(target=self.sync, name="catalog sync", daemon=True).start()
        except (TypeError, ValueError):
            pass
