"""The SDK catalog kept by the registry: the source of truth for families, build servers, workspaces
and SDK instances (with their boot and flash recipes), handed to every host and client.

Storage is a small document store in SQLite: each document keeps the YAML text it was written
with (comments included) and every write is a new revision in an append-only history, so any
change can be looked up and rolled back. The site's sdk/ directory is the seed underneath: a
document the registry does not have comes from the site, and deleting one in the registry hides
the site's copy too.

Writes are safe for many writers without accounts:
  * every write names the revision it was based on; a document changed since is a conflict,
    so two agents never silently overwrite each other (`force` overrides);
  * the whole catalog is loaded with the change applied and every ready boot method is planned
    before anything is stored; a change that breaks any SDK is refused with the reason;
  * every write records who made it and why.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ..core.errors import Conflict, InvalidRequest, NotFound
from ..sdk.catalog import BOOT_METHODS, CATALOG_KINDS, SdkCatalog

# what people call each kind; refs are `<kind>/<id>`, and a bare id is an SDK instance
KIND_NAMES = {"family": "families", "server": "servers", "workspace": "workspaces", "sdk": "instances"}
SHORT = {v: k for k, v in KIND_NAMES.items()}


def parse_ref(ref: str) -> tuple[str, str]:
    """`sdk-a` or `sdk/sdk-a` -> ("instances", "sdk-a"); `family/x` -> ("families", "x")."""
    kind, _, ident = (ref or "").partition("/")
    if not ident:
        kind, ident = "sdk", kind
    if kind in CATALOG_KINDS:
        kind = SHORT[kind]
    if kind not in KIND_NAMES:
        raise InvalidRequest(f"unknown catalog kind {kind!r}", hint=f"kinds: {', '.join(KIND_NAMES)}; a bare id is an sdk")
    import re

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", ident):
        raise InvalidRequest(f"invalid id {ident!r}", hint="letters, digits, _ . - (max 63)")
    return KIND_NAMES[kind], ident


def ref_of(kind: str, ident: str) -> str:
    return ident if kind == "instances" else f"{SHORT[kind]}/{ident}"


def _parse(text: str, ident: str) -> dict[str, Any]:
    import yaml

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise InvalidRequest(f"not valid YAML: {e}") from None
    if not isinstance(doc, dict):
        raise InvalidRequest("a catalog document is a YAML mapping")
    doc.setdefault("id", ident)
    if doc["id"] != ident:
        raise InvalidRequest(f"the document's id is {doc['id']!r}, not {ident!r}",
                             hint="rename by writing the new id and deleting the old one")
    return doc


class CatalogStore:
    def __init__(self, path: Path, seed_roots: list[Path]):
        self.seed_roots = [Path(r) for r in seed_roots]
        # called as fn(kind, id, text or None, author, message, rev) after each stored write (e.g. the git mirror)
        self.listeners: list[Any] = []
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS history (
              rev INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, id TEXT NOT NULL,
              text TEXT, body TEXT, deleted INTEGER NOT NULL DEFAULT 0, base INTEGER,
              author TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS history_doc ON history(kind, id, rev);
            CREATE TABLE IF NOT EXISTS docs (
              kind TEXT NOT NULL, id TEXT NOT NULL, rev INTEGER NOT NULL, PRIMARY KEY(kind, id));
        """)

    # -------------------------------------------------------------- reads --
    @property
    def rev(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(rev), 0) FROM history").fetchone()[0]

    def _current(self, kind: str, ident: str) -> dict[str, Any] | None:
        row = self.db.execute("""SELECT h.rev, h.text, h.deleted, h.author, h.message, h.at FROM docs d
                                 JOIN history h ON h.rev = d.rev WHERE d.kind=? AND d.id=?""", (kind, ident)).fetchone()
        return dict(zip(("rev", "text", "deleted", "author", "message", "at"), row)) if row else None

    def _seed(self) -> dict[tuple[str, str], Path]:
        out = {}
        for root in self.seed_roots:
            for kind in CATALOG_KINDS:
                for p in sorted((root / kind).glob("*.yaml")):
                    import yaml

                    try:
                        ident = (yaml.safe_load(p.read_text()) or {}).get("id") or p.stem
                    except Exception:  # noqa: BLE001 - a broken seed file is the site's problem
                        continue
                    out[(kind, ident)] = p
        return out

    def get(self, ref: str) -> dict[str, Any]:
        kind, ident = parse_ref(ref)
        cur = self._current(kind, ident)
        if cur and not cur["deleted"]:
            return {"ref": ref_of(kind, ident), "kind": SHORT[kind], "id": ident, "origin": "registry", "rev": cur["rev"],
                    "author": cur["author"], "at": cur["at"], "message": cur["message"], "text": cur["text"]}
        seed = None if cur else self._seed().get((kind, ident))
        if seed:
            return {"ref": ref_of(kind, ident), "kind": SHORT[kind], "id": ident, "origin": "site", "rev": 0,
                    "author": None, "at": None, "message": "", "text": seed.read_text()}
        raise NotFound(f"no {SHORT[kind]} {ident!r} in the catalog" + (" (deleted)" if cur else ""),
                       hint="`bsp catalog list` shows what there is")

    def list(self, kind: str | None = None) -> list[dict[str, Any]]:
        kinds = [parse_ref(f"{kind}/x")[0]] if kind else list(CATALOG_KINDS)
        rows = {}
        for (k, ident), p in self._seed().items():
            if k in kinds:
                rows[(k, ident)] = {"ref": ref_of(k, ident), "kind": SHORT[k], "id": ident, "origin": "site", "rev": 0,
                                    "author": None, "at": None, "message": ""}
        for k, ident, rev, deleted, author, message, at in self.db.execute(
                """SELECT d.kind, d.id, h.rev, h.deleted, h.author, h.message, h.at FROM docs d JOIN history h ON h.rev = d.rev"""):
            if k not in kinds:
                continue
            if deleted:
                rows.pop((k, ident), None)
                continue
            rows[(k, ident)] = {"ref": ref_of(k, ident), "kind": SHORT[k], "id": ident, "origin": "registry", "rev": rev,
                                "author": author, "at": at, "message": message}
        order = {k: i for i, k in enumerate(CATALOG_KINDS)}
        return sorted(rows.values(), key=lambda r: (order[KIND_NAMES[r["kind"]]], r["id"]))

    def history(self, ref: str) -> list[dict[str, Any]]:
        kind, ident = parse_ref(ref)
        return [dict(zip(("rev", "deleted", "base", "author", "message", "at"), row)) | {"deleted": bool(row[1])}
                for row in self.db.execute("""SELECT rev, deleted, base, author, message, at FROM history
                                              WHERE kind=? AND id=? ORDER BY rev DESC""", (kind, ident))]

    def text_at(self, ref: str, rev: int) -> str | None:
        kind, ident = parse_ref(ref)
        row = self.db.execute("SELECT text, deleted FROM history WHERE kind=? AND id=? AND rev=?", (kind, ident, rev)).fetchone()
        if not row:
            raise NotFound(f"{ref} has no revision {rev}", hint=f"`bsp catalog history {ref}`")
        return None if row[1] else row[0]

    def snapshot(self) -> dict[str, Any]:
        """What hosts and clients layer above their site: the registry's documents and deletions."""
        docs, removed = [], {k: [] for k in CATALOG_KINDS}
        for k, ident, text, deleted in self.db.execute(
                "SELECT d.kind, d.id, h.text, h.deleted FROM docs d JOIN history h ON h.rev = d.rev ORDER BY d.kind, d.id"):
            if deleted:
                removed[k].append(ident)
            else:
                docs.append({"kind": k, "id": ident, "text": text})
        return {"rev": self.rev, "docs": docs, "removed": {k: v for k, v in removed.items() if v}}

    # ---------------------------------------------------------- validation --
    def validate(self, changes: dict[tuple[str, str], str | None]) -> dict[str, Any]:
        """Load the whole catalog with `changes` (text, or None to delete) applied and plan every ready boot
        method. Raises InvalidRequest naming what broke; returns what the catalog would hold."""
        snap = self.snapshot()
        texts = {(d["kind"], d["id"]): d["text"] for d in snap["docs"]}
        removed = {(k, i) for k, ids in snap["removed"].items() for i in ids}
        for key, text in changes.items():
            if text is None:
                texts.pop(key, None)
                removed.add(key)
            else:
                texts[key] = text
                removed.discard(key)
        with tempfile.TemporaryDirectory(prefix="bsp-catalog-") as tmp:
            layer = write_layer(Path(tmp) / "layer", texts, removed)
            try:
                cat = SdkCatalog(self.seed_roots + [layer])
            except (InvalidRequest, NotFound) as e:
                raise InvalidRequest(f"the catalog would not load: {e.message}", hint=e.hint or
                                     "fix the document (references to families, servers and workspaces must exist)",
                                     details=e.details) from None
            problems = []
            for sdk_id, inst in cat.instances.items():
                for method in BOOT_METHODS:
                    if inst.boot[method].status != "ready":
                        continue
                    try:
                        inst.boot_plan(method)
                    except (InvalidRequest, NotFound) as e:
                        problems.append(f"{sdk_id}/{method}: {e.message}")
            if problems:
                raise InvalidRequest("boot plans would break: " + "; ".join(problems))
            return {"sdks": sorted(cat.instances.names()), "families": cat.families.names(),
                    "servers": cat.servers.names()}

    # -------------------------------------------------------------- writes --
    def _write(self, kind: str, ident: str, text: str | None, base: int | None, force: bool, author: str,
               message: str, check: bool) -> dict[str, Any]:
        if not author:
            raise InvalidRequest("a catalog change needs an author", hint="pass --author (default: $BSP_HOLDER or $USER)")
        if text is not None:
            _parse(text, ident)
        with self._lock:
            cur = self._current(kind, ident)
            now_rev = cur["rev"] if cur else 0
            if not force:
                if base is None:
                    if cur and not cur["deleted"]:
                        raise Conflict(f"{ref_of(kind, ident)} exists (revision {now_rev})",
                                       hint=f"get it first and edit that (`bsp catalog get {ref_of(kind, ident)} -o FILE`), "
                                            "or pass --force to replace it", details={"rev": now_rev})
                elif base != now_rev:
                    raise Conflict(f"{ref_of(kind, ident)} changed since revision {base}: now revision {now_rev}"
                                   + (f" by {cur['author']} ({cur['message']})" if cur else ""),
                                   hint=f"get it again, apply your change to that, and put it with --base {now_rev}",
                                   details={"rev": now_rev, "author": cur and cur["author"]})
            checked = self.validate({(kind, ident): text})
            if check:
                return {"ref": ref_of(kind, ident), "checked": True, "would_be": checked, "rev": now_rev}
            body = json.dumps(_parse(text, ident)) if text is not None else None
            self.db.execute("BEGIN IMMEDIATE")
            try:
                rev = self.db.execute("""INSERT INTO history(kind, id, text, body, deleted, base, author, message, at)
                                         VALUES(?,?,?,?,?,?,?,?,?)""",
                                      (kind, ident, text, body, int(text is None), now_rev, author, message or "",
                                       time.time())).lastrowid
                self.db.execute("INSERT INTO docs(kind, id, rev) VALUES(?,?,?) ON CONFLICT(kind, id) DO UPDATE SET rev=excluded.rev",
                                (kind, ident, rev))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        for fn in list(self.listeners):
            try:
                fn(kind, ident, text, author, message, rev)
            except Exception:  # noqa: BLE001 - a listener never undoes a stored write
                pass
        return {"ref": ref_of(kind, ident), "rev": rev, "previous": now_rev, "deleted": text is None, "catalog": checked}

    def put(self, ref: str, text: str, base: int | None = None, force: bool = False, author: str = "",
            message: str = "", check: bool = False) -> dict[str, Any]:
        kind, ident = parse_ref(ref)
        cur = self._current(kind, ident)
        # A document only the site has is edited at revision 0: the first write takes it over.
        if base is None and not cur and (kind, ident) in self._seed():
            base = 0
        return self._write(kind, ident, text, base, force, author, message, check)

    def delete(self, ref: str, base: int | None = None, force: bool = False, author: str = "", message: str = "",
               check: bool = False) -> dict[str, Any]:
        kind, ident = parse_ref(ref)
        self.get(ref)                                   # must exist
        if base is None and not force:
            raise InvalidRequest("deleting needs --base (the revision you looked at) or --force",
                                 hint=f"`bsp catalog get {ref}` shows its revision")
        return self._write(kind, ident, None, base, force, author, message, check)

    def recipe(self, sdk: str, method: str, text: str, base: int | None = None, force: bool = False,
               author: str = "", message: str = "", check: bool = False) -> dict[str, Any]:
        """Replace one boot method of an SDK (`load` or `download`): steps, status, notes, variants."""
        import yaml

        if method not in BOOT_METHODS:
            raise InvalidRequest(f"method must be one of {list(BOOT_METHODS)}")
        try:
            recipe = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise InvalidRequest(f"recipe is not valid YAML: {e}") from None
        if isinstance(recipe, list):                     # just the steps
            recipe = {"status": "ready", "steps": recipe}
        if not isinstance(recipe, dict) or "steps" not in recipe:
            raise InvalidRequest("a recipe is a list of steps, or a mapping with `steps` (and status, notes, variants)")
        recipe.setdefault("status", "ready" if recipe["steps"] else "todo")
        doc = self.get(sdk)
        if doc["kind"] != "sdk":
            raise InvalidRequest(f"{sdk} is not an sdk")
        data = _parse(doc["text"], doc["id"])
        data.setdefault("boot", {})[method] = recipe
        new = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        return self.put(sdk, new, base=doc["rev"] if base is None else base, force=force, author=author,
                        message=message or f"{method} recipe", check=check)

    def rollback(self, ref: str, rev: int, author: str = "", message: str = "") -> dict[str, Any]:
        kind, ident = parse_ref(ref)
        text = self.text_at(ref, rev)
        cur = self._current(kind, ident)
        return self._write(kind, ident, text, cur["rev"] if cur else 0, False, author,
                           message or f"roll back to revision {rev}", False)

    def import_seed(self, author: str, check: bool = False) -> dict[str, Any]:
        """Take every site document the registry does not have yet into the registry (one revision each)."""
        imported = []
        for (kind, ident), p in sorted(self._seed().items(), key=lambda kv: CATALOG_KINDS.index(kv[0][0])):
            if self._current(kind, ident):
                continue
            if not check:
                self._write(kind, ident, p.read_text(), 0, False, author, f"imported from the site ({p.name})", False)
            imported.append(ref_of(kind, ident))
        return {"imported": imported, "rev": self.rev, "checked": check}


def write_layer(dest: Path, texts: dict[tuple[str, str], str], removed: set[tuple[str, str]] | None = None) -> Path:
    """A catalog root holding `texts` ({(kind, id): yaml}) and a removed.json for deletions."""
    dest.mkdir(parents=True, exist_ok=True)
    for kind in CATALOG_KINDS:
        (dest / kind).mkdir(exist_ok=True)
    for (kind, ident), text in texts.items():
        (dest / kind / f"{ident}.yaml").write_text(text)
    gone: dict[str, list[str]] = {}
    for kind, ident in removed or ():
        gone.setdefault(kind, []).append(ident)
    if gone:
        (dest / "removed.json").write_text(json.dumps(gone))
    return dest


def materialize(snapshot: dict[str, Any], dest: Path) -> Path:
    """Write a registry snapshot as a catalog root at `dest`, replacing what was there in one step."""
    import shutil

    tmp = dest.with_name(dest.name + f".new-{time.monotonic_ns()}")
    write_layer(tmp, {(d["kind"], d["id"]): d["text"] for d in snapshot.get("docs", [])},
                {(k, i) for k, ids in (snapshot.get("removed") or {}).items() for i in ids})
    (tmp / "REV").write_text(str(snapshot.get("rev", 0)))
    old = dest.with_name(dest.name + f".old-{time.monotonic_ns()}")
    if dest.exists():
        dest.rename(old)
    tmp.rename(dest)
    shutil.rmtree(old, ignore_errors=True)
    return dest


def layer_rev(dest: Path) -> int:
    try:
        return int((dest / "REV").read_text())
    except (OSError, ValueError):
        return 0
