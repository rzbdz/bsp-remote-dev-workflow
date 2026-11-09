"""`bsp catalog ...`: the SDK catalog kept by the registry (see registry/catalog.py).

Documents are SDK instances (with their load and flash recipes), families, build servers and
workspaces, addressed as `<id>` (an sdk) or `<kind>/<id>`. `get -o FILE` writes the document with
a first line naming its revision; `put -f FILE` reads that line back as the revision the change is
based on, so editing a copy someone else has meanwhile changed is a conflict, not a lost update.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from ..core.errors import BspError, InvalidRequest
from .remotes import Remote, RemoteError, RemoteStore
from .transport import HttpTransport, Unreachable

HEADER = re.compile(r"^# bsp catalog: (\S+) revision (\d+)\b.*\n?")


def registry_url(store: RemoteStore | None = None) -> str | None:
    if os.environ.get("BSP_REGISTRY"):
        return os.environ["BSP_REGISTRY"]
    try:
        return (store or RemoteStore()).registry_url
    except RemoteError:
        return None


def registry_layer() -> Path | None:
    """The registry's catalog as a local catalog root, fetched when it changed; the last copy when the
    registry cannot be reached; None without a registry."""
    from ..registry.catalog import layer_rev, materialize

    url = registry_url()
    if not url:
        return None
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "bsp" / \
        f"catalog-{hashlib.sha1(url.encode()).hexdigest()[:10]}"
    try:
        r = HttpTransport(url).request("GET", "/api/v1/catalog/snapshot", timeout=8)
        if (r or {}).get("ok") and r["result"]["rev"] != layer_rev(cache):
            cache.parent.mkdir(parents=True, exist_ok=True)
            materialize(r["result"], cache)
    except Unreachable:
        pass
    return cache if cache.is_dir() else None


def _author(args) -> str:
    return args.author or os.environ.get("BSP_HOLDER") or os.environ.get("USER") or "anonymous"


def _read(path: str) -> tuple[str, int | None]:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    m = HEADER.match(text)
    return (text[m.end():], int(m.group(2))) if m else (text, None)


def add_parser(sub) -> None:
    c = sub.add_parser("catalog", help="the SDK catalog in the registry: SDKs, flash recipes, build servers (shared, versioned)")
    cs = c.add_subparsers(dest="catalog_cmd", metavar="<action>")
    ls = cs.add_parser("list", help="documents, where each comes from (registry or site), revision, last change")
    ls.add_argument("--kind", choices=["sdk", "family", "server", "workspace"])
    g = cs.add_parser("get", help="one document as YAML (with -o: to a file you can edit and put back)")
    g.add_argument("ref", help="sdk id, or family/<id>, server/<id>, workspace/<id>")
    g.add_argument("-o", "--output")
    for name, hlp in (("put", "write a document (validated with the whole catalog first)"),
                      ("recipe", "replace an SDK's load (RAM boot) or download (flash) recipe")):
        p = cs.add_parser(name, help=hlp)
        if name == "put":
            p.add_argument("ref")
        else:
            p.add_argument("sdk")
            p.add_argument("method", choices=["load", "download"])
        p.add_argument("-f", "--file", required=True, help="the YAML (- for stdin); a recipe is a list of steps or "
                                                            "{status, steps, notes, variants}")
        p.add_argument("--base", type=int, help="the revision you edited (default: from the file's first line)")
        p.add_argument("--force", action="store_true", help="write even if someone changed it meanwhile")
        p.add_argument("--check", action="store_true", help="only validate, write nothing")
        p.add_argument("-m", "--message", default="")
        p.add_argument("--author")
    rm = cs.add_parser("rm", help="delete a document (hides the site's copy too)")
    rm.add_argument("ref")
    rm.add_argument("--base", type=int)
    rm.add_argument("--force", action="store_true")
    rm.add_argument("-m", "--message", default="")
    rm.add_argument("--author")
    h = cs.add_parser("history", help="every revision of a document")
    h.add_argument("ref")
    rb = cs.add_parser("rollback", help="write an earlier revision again (as a new revision)")
    rb.add_argument("ref")
    rb.add_argument("rev", type=int)
    rb.add_argument("-m", "--message", default="")
    rb.add_argument("--author")
    im = cs.add_parser("import", help="take the site's SDK documents the registry does not have into it")
    im.add_argument("--check", action="store_true")
    im.add_argument("--author")
    ex = cs.add_parser("export", help="write the registry's documents as site files (backup), with removed.json")
    ex.add_argument("--dir", required=True)
    cs.add_parser("status", help="the catalog revision, the revision each host has, and site edits it overrides")
    cs.add_parser("refresh", help="clear this machine's cached catalog and have every host fetch it again now")


def run(args, store: RemoteStore, token: str | None) -> tuple[Any, str]:
    from .fleet import connect

    url = registry_url(store)
    if not url:
        raise InvalidRequest("no registry configured", hint='set "registry" in the site\'s remotes.json, or BSP_REGISTRY')
    try:
        h = connect(Remote("registry", url, token, source="registry"))
    except Unreachable as u:
        raise RemoteError(f"registry {url} unreachable: {u.reason}", code="unreachable") from None

    def call(tool: str, **kw: Any) -> Any:
        r = h.call(tool, {k: v for k, v in kw.items() if v is not None}, timeout=120)
        if not r.get("ok"):
            from ..core.errors import Conflict, Forbidden, NotFound

            e = r.get("error") or {}
            cls = {"conflict": Conflict, "not_found": NotFound, "invalid_request": InvalidRequest,
                   "forbidden": Forbidden}.get(e.get("code"), BspError)
            raise cls(e.get("message", "error"), hint=e.get("hint"), details=e.get("details"))
        return r["result"], r.get("text")

    c = args.catalog_cmd
    if c in (None, "list"):
        rows, text = call("catalog_list", kind=getattr(args, "kind", None))
        return rows, text or "(empty catalog)"
    if c == "get":
        doc, _ = call("catalog_get", ref=args.ref)
        body = f"# bsp catalog: {doc['ref']} revision {doc['rev']} (from {doc['origin']}; keep this line, put uses it)\n" + doc["text"]
        if args.output:
            Path(args.output).write_text(body, encoding="utf-8")
            return doc, f"wrote {doc['ref']} revision {doc['rev']} ({doc['origin']}) to {args.output}"
        return doc, body
    if c in ("put", "recipe"):
        text, base = _read(args.file)
        base = args.base if args.base is not None else base
        if c == "put":
            res, out = call("catalog_put", ref=args.ref, text=text, base=base, force=args.force, check=args.check,
                            author=_author(args), message=args.message)
        else:
            res, out = call("catalog_recipe", sdk=args.sdk, method=args.method, text=text, base=base, force=args.force,
                            check=args.check, author=_author(args), message=args.message)
        return res, out
    if c == "rm":
        return call("catalog_delete", ref=args.ref, base=args.base, force=args.force, author=_author(args),
                    message=args.message)
    if c == "history":
        return call("catalog_history", ref=args.ref)
    if c == "rollback":
        return call("catalog_rollback", ref=args.ref, rev=args.rev, author=_author(args), message=args.message)
    if c == "import":
        return call("catalog_import", author=_author(args), check=args.check)
    if c == "export":
        from ..registry.catalog import materialize

        snap, _ = call("catalog_snapshot")
        dest = materialize(snap, Path(args.dir).expanduser().resolve())
        return {"dir": str(dest), "rev": snap["rev"], "documents": len(snap["docs"])}, \
            f"wrote catalog revision {snap['rev']} ({len(snap['docs'])} documents) to {dest}"
    if c == "status":
        return call("catalog_status")
    if c == "refresh":
        import shutil

        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "bsp"
        cleared = [str(p) for p in cache.glob("catalog-*")]
        for p in cleared:
            shutil.rmtree(p, ignore_errors=True)
        res, text = call("catalog_refresh")
        return {**res, "cleared": cleared}, (f"cleared {len(cleared)} cached catalog(s) here\n" if cleared else "") + (text or "")
    raise InvalidRequest(f"unknown catalog action {c}")
