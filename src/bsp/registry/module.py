"""registry module: a directory of board hosts that announce themselves.

Board hosts running bsp-hostd with `server.announce` POST their id, port, addresses and ssh user
here every minute or so. The address the announcement came from is the one this machine sees,
so it is tried first. Clients with `registry` in the site's remotes.json (or $BSP_REGISTRY) list
the fresh entries as remotes, so new hosts and changed addresses need no client configuration.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from ..core.cmdspec import CommandSpec, Param
from ..core.commands import Command
import logging

from ..core.errors import InvalidRequest, NotFound
from ..sdk.catalog import BOOT_METHODS
from ..core.module import MODULES, Module, ModuleContext

log = logging.getLogger(__name__)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_SSH_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _address(text: Any) -> str | None:
    try:
        ip = ipaddress.ip_address(str(text).strip().strip("[]"))
    except ValueError:
        return None
    return None if ip.is_loopback or ip.is_unspecified or ip.is_link_local else str(ip)


def _url(addr: str, port: int) -> str:
    return f"http://[{addr}]:{port}" if ":" in addr else f"http://{addr}:{port}"


@MODULES.register("registry")
class RegistryModule(Module):
    name = "registry"
    title = "Host registry"

    def __init__(self, ctx: ModuleContext, config: dict[str, Any]):
        super().__init__(ctx, config)
        self.path: Path = ctx.module_state_dir("registry") / "hosts.json"
        self.stale_s = float(config.get("stale_s", 300))
        self.trust_proxy = bool(config.get("trust_proxy", False))
        self._lock = threading.Lock()
        try:
            self._hosts: dict[str, dict[str, Any]] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self._hosts = {}
        from ..core.site import layered
        from .catalog import CatalogStore

        # The SDK catalog, seeded by the site this registry runs with (<site>/hosts/<registry>.yaml -> <site>).
        site = (ctx.config_dir / config.get("site", "..")).resolve()
        self.catalog = CatalogStore(ctx.module_state_dir("registry") / "catalog.sqlite", layered("sdk", site))
        # git-registry: the site directory is a clone of the catalog repository, kept in step with the catalog
        self.git = None
        if config.get("git"):
            from .gitsync import GitMirror

            self.git = GitMirror(self.catalog, site, config["git"] if isinstance(config["git"], dict) else {}, ctx.bus)
            self.catalog.listeners.append(self.git.record)
            # documents taken in from git reach the hosts like any other change
            ctx.bus.on("catalog.git", lambda ev: ev.data.get("applied") and self._changed({"ref": "git", "rev": self.catalog.rev}))

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._hosts, indent=2))
        tmp.replace(self.path)

    # ----------------------------------------------------------- announce --
    def announce(self, body: dict[str, Any], seen: str | None) -> dict[str, Any]:
        host = str(body.get("host") or "")
        if not _ID.match(host):
            raise InvalidRequest(f"invalid host id {host!r}", hint="letters, digits, _ . - (max 63)")
        try:
            port = int(body.get("port", 8765))
        except (TypeError, ValueError):
            port = 0
        if not 0 < port < 65536:
            raise InvalidRequest("port must be 1-65535")
        user = body.get("ssh_user")
        if user is not None and not _SSH_USER.match(str(user)):
            raise InvalidRequest(f"invalid ssh user {user!r}")
        seen = _address(seen)
        addrs = [a for a in (_address(x) for x in (body.get("addresses") or [])[:16]) if a]
        addrs = list(dict.fromkeys(([seen] if seen else []) + addrs))
        if not addrs:
            raise InvalidRequest("no usable address in the announcement",
                                 hint="announce from the host's network address, or list addresses")
        now = time.time()
        with self._lock:
            old = self._hosts.get(host) or {}
            entry = {"host": host, "port": port, "addresses": addrs, "seen_address": seen, "ssh_user": user,
                     "description": str(body.get("description") or "")[:200],
                     "version": str(body.get("version") or "")[:40],
                     "duts": [str(d) for d in (body.get("duts") or [])][:200],
                     "slots": [str(d) for d in (body.get("slots") or [])][:200],
                     "catalog_rev": int(body.get("catalog_rev") or 0),
                     "catalog_shadowed": [x for x in (body.get("catalog_shadowed") or []) if isinstance(x, dict)][:100],
                     "ports": {k: [str(x) for x in v][:500] for k, v in (body.get("ports") or {}).items()
                               if k in ("tty", "usb") and isinstance(v, list)},
                     "first_seen": old.get("first_seen", now), "seen": now}
            changed = {k: v for k, v in entry.items() if k not in ("seen", "first_seen") and old.get(k) != v}
            self._hosts[host] = entry
            self._save()
        if changed:
            self.ctx.bus.publish("registry.announced", host=host, addresses=addrs, new=not old)
        # A host's first announcement with ports (a new host, or one that just got the port watcher) is no news.
        if "ports" in changed and old and old.get("ports"):
            before, now = old.get("ports") or {}, entry["ports"]
            added = {k: sorted(set(now.get(k, [])) - set(before.get(k, []))) for k in ("tty", "usb")}
            removed = {k: sorted(set(before.get(k, [])) - set(now.get(k, []))) for k in ("tty", "usb")}
            if any(added.values()) or any(removed.values()):
                self.ctx.bus.publish("registry.ports", host=host, added=added, removed=removed)
        return {"host": host, "registered": True, "seen_address": seen, "addresses": addrs,
                "refresh_s": max(10, int(self.stale_s / 4)), "catalog_rev": self.catalog.rev}

    # -------------------------------------------------------------- reads --
    def view(self, e: dict[str, Any]) -> dict[str, Any]:
        age = time.time() - e["seen"]
        first, *rest = e["addresses"]
        remote = {"url": _url(first, e["port"]), "fallback_urls": [_url(a, e["port"]) for a in rest],
                  "ssh": f"{e['ssh_user']}@{first}" if e.get("ssh_user") else None, "port": e["port"],
                  "description": e.get("description") or f"registered {e['host']}"}
        return {**e, "age_s": round(age), "stale": age > self.stale_s, "remote": remote}

    def list(self, all: bool = False) -> list[dict[str, Any]]:   # noqa: A002 - CLI flag name
        with self._lock:
            items = [self.view(e) for _, e in sorted(self._hosts.items())]
        return items if all else [e for e in items if not e["stale"]]

    def forget(self, host: str) -> dict[str, Any]:
        with self._lock:
            if self._hosts.pop(host, None) is None:
                raise NotFound(f"host {host!r} is not registered", hint="see `bsp registry list --all`")
            self._save()
        self.ctx.bus.publish("registry.forgotten", host=host)
        return {"host": host, "forgotten": True}

    def status(self) -> dict[str, Any]:
        items = self.list(all=True)
        return {"hosts": len(items), "fresh": sum(not e["stale"] for e in items)}

    # ------------------------------------------------------------- catalog --
    def _changed(self, result: dict[str, Any]) -> dict[str, Any]:
        """After a write: announce it, and nudge the hosts to fetch the new catalog now (they also notice at
        their next announcement)."""
        if result.get("checked") or "rev" not in result:
            return result
        self.ctx.bus.publish("catalog.changed", ref=result.get("ref"), rev=self.catalog.rev)

        def nudge() -> None:
            import urllib.request

            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for e in self.list():
                for url in [e["remote"]["url"], *e["remote"]["fallback_urls"]]:
                    try:
                        req = urllib.request.Request(url + "/api/v1/tools/catalog_sync", data=b"{}", method="POST",
                                                     headers={"Content-Type": "application/json"})
                        opener.open(req, timeout=5).read()
                        break
                    except Exception:  # noqa: BLE001 - the host catches up at its next announcement
                        continue
        threading.Thread(target=nudge, name="catalog nudge", daemon=True).start()
        return result

    def startup(self) -> None:
        if self.git:
            try:
                self.git.start()
            except Exception as e:  # noqa: BLE001 - the catalog still works; status says why git does not
                self.git.state["last_error"] = str(e)
                log.warning("git-registry not started: %s", e)

    def shutdown(self) -> None:
        if self.git:
            self.git.stop()

    def catalog_status(self) -> dict[str, Any]:
        rev = self.catalog.rev
        return {"rev": rev, "git": self.git.status() if self.git else None, "hosts": [{"host": e["host"], "catalog_rev": e.get("catalog_rev", 0),
                                       "current": e.get("catalog_rev", 0) == rev, "stale": e["stale"], "age_s": e["age_s"],
                                       "shadowed": e.get("catalog_shadowed") or [],
                                       "duts": e["duts"], "ports": e.get("ports") or {}} for e in self.list(all=True)]}

    def refresh(self) -> dict[str, Any]:
        """Have every host throw its copy of the catalog away and fetch it again now; report what each has."""
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        hosts = []
        for e in self.list():
            row = {"host": e["host"], "rev": None, "error": None, "shadowed": []}
            for url in [e["remote"]["url"], *e["remote"]["fallback_urls"]]:
                try:
                    req = urllib.request.Request(url + "/api/v1/tools/catalog_sync", data=json.dumps({"reset": True}).encode(),
                                                 method="POST", headers={"Content-Type": "application/json"})
                    res = json.loads(opener.open(req, timeout=30).read())
                    if not res.get("ok"):
                        raise ValueError((res.get("error") or {}).get("message"))
                    row.update(rev=res["result"].get("rev"), error=None, shadowed=res["result"].get("shadowed") or [])
                    break
                except Exception as ex:  # noqa: BLE001 - reported per host
                    row["error"] = str(ex)
            hosts.append(row)
        return {"rev": self.catalog.rev, "hosts": hosts}

    def _git_pull(self) -> dict[str, Any]:
        if not self.git:
            raise InvalidRequest("this registry keeps no git repository", hint="set modules.registry.git in its host config")
        return self.git.pull_now()

    def open_paths(self) -> list[str]:
        # Anyone may read and change the catalog: every change is checked, attributed and can be rolled back.
        return ["/api/v1/catalog/", "/api/v1/tools/catalog_"]

    def _catalog_commands(self) -> list[Command]:
        c = self.catalog
        REF = Param("ref", help="sdk id, or <kind>/<id> with kind family | server | workspace | sdk")
        AUTHOR = Param("author", help="who makes the change", required=False, positional=False, default="")
        MSG = Param("message", help="why", required=False, positional=False, default="")
        BASE = Param("base", "integer", "the revision your change is based on (from get)", required=False, positional=False)
        FORCE = Param("force", "boolean", "write even if it changed since `base`", required=False, default=False)
        CHECK = Param("check", "boolean", "only validate, write nothing", required=False, default=False)
        TEXT = Param("text", help="the YAML document", positional=False)

        def show(r: dict[str, Any]) -> str:
            if r.get("checked"):
                return f"{r.get('ref', '')}: valid (nothing written)" if "imported" not in r else f"would import {len(r['imported'])}: {', '.join(r['imported'])}"
            if "imported" in r:
                return f"imported {len(r['imported'])} document(s) into catalog revision {r['rev']}"
            return f"{r['ref']}: {'deleted' if r['deleted'] else 'written'} as catalog revision {r['rev']} (was {r['previous']})"

        return [
            Command(CommandSpec("catalog.list", "SDK catalog documents: where each comes from, its revision, who changed it last",
                                [Param("kind", help="only this kind: family | server | workspace | sdk", required=False)],
                                mutating=False), c.list,
                    lambda rows: "\n".join(f"{r['ref']:<28} {r['origin']:<8} rev {r['rev']:<5} {r['author'] or ''}  {r['message'] or ''}" for r in rows)),
            Command(CommandSpec("catalog.get", "One catalog document as YAML, with its revision (edit it and put it back)",
                                [REF], mutating=False), c.get, lambda r: r["text"]),
            Command(CommandSpec("catalog.put", "Write a catalog document (validated with the whole catalog first)",
                                [REF, TEXT, BASE, FORCE, CHECK, AUTHOR, MSG]),
                    lambda **kw: self._changed(c.put(**kw)), show),
            Command(CommandSpec("catalog.recipe", "Replace one boot method of an SDK: load (RAM boot) or download (flash)",
                                [Param("sdk", help="sdk id"), Param("method", help="load | download", choices=list(BOOT_METHODS)),
                                 TEXT, BASE, FORCE, CHECK, AUTHOR, MSG]),
                    lambda **kw: self._changed(c.recipe(**kw)), show),
            Command(CommandSpec("catalog.delete", "Delete a catalog document (hides the site's copy too)",
                                [REF, BASE, FORCE, CHECK, AUTHOR, MSG]),
                    lambda **kw: self._changed(c.delete(**kw)), show),
            Command(CommandSpec("catalog.history", "Every revision of a catalog document", [REF], mutating=False), c.history,
                    lambda rows: "\n".join(f"rev {r['rev']:<5} {'DELETED ' if r['deleted'] else ''}{r['author']}  {r['message']}" for r in rows) or "(no revisions in the registry)"),
            Command(CommandSpec("catalog.rollback", "Write an earlier revision of a document again (as a new revision)",
                                [REF, Param("rev", "integer", "the revision to go back to"), AUTHOR, MSG]),
                    lambda **kw: self._changed(c.rollback(**kw)), show),
            Command(CommandSpec("catalog.import", "Take the site's SDK documents the registry does not have yet into it",
                                [AUTHOR, CHECK]),
                    lambda author, check: self._changed(c.import_seed(author or "import", check)), show),
            Command(CommandSpec("catalog.snapshot", "The registry's catalog documents and deletions (what hosts layer on their site)",
                                [], mutating=False), c.snapshot, lambda r: f"catalog revision {r['rev']}: {len(r['docs'])} document(s)"),
            Command(CommandSpec("catalog.status", "The catalog revision, which revision each host has, and site edits it overrides",
                                [], mutating=False),
                    self.catalog_status,
                    lambda r: f"catalog revision {r['rev']}" + (
                        f"  (git {r['git']['remote']} {r['git']['branch']} at {r['git']['head']}, pushed {r['git']['pushed']}"
                        + (f", {r['git']['pending']} pending" if r['git']['pending'] else "")
                        + (f"; ERROR {r['git']['last_error']}" if r['git']['last_error'] else "") + ")"
                        + "".join(f"\n  NOT APPLIED from git: {x['ref']} ({x['author']}, {x['commit']}): {x['error']}" for x in r['git']['rejected'][-5:])
                        if r.get("git") else "") + "\n" + "\n".join(
                        f"  {h['host']:<16} rev {h['catalog_rev']:<5} {'ok' if h['current'] else 'BEHIND'}{' (stale)' if h['stale'] else ''}"
                        + "".join(f"\n    NOT IN EFFECT: {x['file']} differs from {x['ref']} (publish with put)" for x in h["shadowed"])
                        for h in r["hosts"])),
            Command(CommandSpec("catalog.git_pull", "Take commits pushed to the catalog's git repository in now (validated)", []),
                    self._git_pull,
                    lambda r: "\n".join([f"{r.get('commits', 0)} new commit(s)"]
                                        + [f"  applied {a['ref']} as revision {a['rev']} ({a['author']}, {a['commit']})" for a in r["applied"]]
                                        + [f"  NOT APPLIED {x['ref']} ({x['author']}, {x['commit']}): {x['error']}" for x in r["rejected"]])),
            Command(CommandSpec("catalog.refresh", "Every host throws its catalog copy away and fetches it again now", []),
                    self.refresh,
                    lambda r: f"catalog revision {r['rev']}\n" + "\n".join(
                        f"  {h['host']:<16} " + (f"rev {h['rev']}" if h["rev"] is not None else f"FAILED: {h['error']}")
                        + "".join(f"\n    NOT IN EFFECT: {x['file']} differs from {x['ref']} (publish with put)" for x in h["shadowed"])
                        for h in r["hosts"])),
        ]

    def commands(self) -> list[Command]:
        return self._catalog_commands() + [
            Command(CommandSpec("registry.list", "Board hosts that announced themselves here, with how to reach them",
                                [Param("all", "boolean", "include hosts not heard from recently", required=False,
                                       default=False)], mutating=False),
                    self.list,
                    lambda items: "\n".join(
                        f"{e['host']:<16} {e['remote']['url']:<28} {'STALE ' if e['stale'] else ''}"
                        f"seen {e['age_s']}s ago  duts: {', '.join(e['duts']) or '-'}" for e in items)
                    or "(no hosts registered)"),
            Command(CommandSpec("registry.forget", "Remove a host from the registry (it comes back if it announces again)",
                                [Param("host", help="host id")]),
                    self.forget, lambda r: f"forgot {r['host']}"),
        ]

    def router(self):
        from .routes import registry_router

        return registry_router(self)
