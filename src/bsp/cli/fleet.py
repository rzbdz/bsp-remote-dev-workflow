"""Client-side state and fleet logic for the `bsp` CLI (stdlib only).

Fleet = the bsp hosts this CLI talks to, discovered with zero configuration:
  * host mode  — a bsp-hostd answers on 127.0.0.1: only that host (it drives its own boards)
  * remote mode — otherwise: every configured remote (built-in list + ~/.config/bsp/remotes.json)
  `-r NAME[,NAME|all]` overrides either.
DUT commands are routed to whichever host owns the DUT.
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .remotes import Remote, RemoteError, RemoteStore
from .transport import Unreachable


def config_dir() -> Path:
    if os.environ.get("BSP_CONFIG"):
        return Path(os.environ["BSP_CONFIG"]).parent
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "bsp"


# ------------------------------------------------------------- leases --
class LeaseBook:
    """Remembers lease tokens this user acquired (dut:<id>, sdk:<id>) so later
    commands need no --lease. Disabled in stateless mode (use BSP_LEASE / --lease)."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.path = config_dir() / "leases.json"
        self.data: dict[str, Any] = {}
        if enabled and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except ValueError:
                self.data = {}

    def get(self, key: str) -> str | None:
        e = self.data.get(key)
        if e and e.get("expires", 0) > time.time():
            return e["token"]
        return None

    def put(self, key: str, token: str, ttl: int, **extra: Any) -> None:
        if not self.enabled:
            return
        self.data[key] = {"token": token, "expires": time.time() + ttl, **extra}
        self._save()

    def drop(self, key: str) -> None:
        if self.enabled and self.data.pop(key, None) is not None:
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


# --------------------------------------------------------------- hosts --
class HostHandle:
    """One reachable bsp-hostd: its remote entry, working transport and host id."""

    def __init__(self, remote: Remote, transport: Any, status: dict[str, Any]):
        self.remote = remote
        self.transport = transport
        self.status = status
        self.id = status.get("host") or remote.name

    def call(self, tool: str, args: dict[str, Any] | None = None, timeout: float = 120) -> dict[str, Any]:
        return self.transport.request("POST", f"/api/v1/tools/{tool}", args or {}, timeout=timeout)

    def get(self, path: str, timeout: float = 30) -> Any:
        return self.transport.request("GET", path, timeout=timeout)


def connect(remote: Remote, quick: float = 2.0) -> HostHandle:
    errors = []
    for t in remote.transports():
        try:
            if t.kind == "http" and not t.reachable(quick):
                errors.append(f"{t.label}: no route")
                continue
            st = t.request("GET", "/api/v1/status", timeout=15)
            if not (st or {}).get("ok"):
                errors.append(f"{t.label}: not a bsp-hostd")
                continue
            return HostHandle(remote, t, st["result"])
        except Unreachable as e:
            errors.append(str(e))
    raise Unreachable(remote.name, "; ".join(errors) or "no transport configured")


class Fleet:
    def __init__(self, store: RemoteStore, selector: str | None, url: str | None, token: str | None):
        self.store = store
        self.selector = selector
        self.url = url
        self.token = token
        self.errors: dict[str, str] = {}
        self._hosts: list[HostHandle] | None = None
        self.mode = "remote"

    def candidates(self) -> list[Remote]:
        if self.url or self.selector or os.environ.get("BSP_URL") or os.environ.get("BSP_REMOTE"):
            return self.store.select(self.url, self.selector, self.token)
        rs = self.store.remotes()
        local = rs.get("local")
        if local:
            try:
                h = connect(local, quick=0.5)
                if "dut" in h.status.get("modules", {}):     # a registry-only hostd drives no boards
                    self.mode = "local"
                    return [local]
            except Unreachable:
                pass
        default = self.store.default_name
        if default and default in rs and default != "local":
            return [rs[default]]
        return [r for n, r in rs.items() if n != "local"]

    def hosts(self) -> list[HostHandle]:
        if self._hosts is None:
            cands = self.candidates()
            out: list[HostHandle] = []

            def one(r: Remote):
                try:
                    return connect(r)
                except Unreachable as e:
                    self.errors[r.name] = str(e.reason)
                    return None

            with ThreadPoolExecutor(max_workers=8) as pool:
                for h in pool.map(one, cands):
                    if h:
                        out.append(h)
            if self.store.registry_error:
                self.errors["registry"] = self.store.registry_error
            self._hosts = out
        return self._hosts

    def require(self) -> list[HostHandle]:
        hs = self.hosts()
        if not hs:
            detail = "; ".join(f"{k}: {v}" for k, v in self.errors.items())
            raise RemoteError(f"no bsp host reachable ({detail})", code="unreachable",
                              hint="check `bsp hosts`; add one with `bsp remote add NAME URL --ssh user@host`")
        return hs

    def fan(self, tool: str, args: dict[str, Any] | None = None, timeout: float = 120) -> list[tuple[HostHandle, dict]]:
        hs = self.require()
        with ThreadPoolExecutor(max_workers=8) as pool:
            res = list(pool.map(lambda h: _safe_call(h, tool, args, timeout), hs))
        return list(zip(hs, res))

    def find_dut(self, dut: str) -> HostHandle:
        """The host of a DUT, by its name or its slot (`<host>/s3`; a bare `s3` when there is one host)."""
        host, _, slot = dut.rpartition("/")
        if host:
            for h in self.require():
                if h.id == host:
                    return h
            raise RemoteError(f"no reachable host {host!r} (from slot {dut})", code="not_found",
                              hint=f"hosts: {', '.join(h.id for h in self.hosts()) or '(none)'}")
        if re.fullmatch(r"s\d+", dut):
            hs = self.require()
            owners = [h for h, r in self.fan("dut_list") if r.get("ok") and any(d["id"] == dut for d in r["result"])]
            if owners:
                return owners[0]
            if len(hs) == 1:
                return hs[0]
            raise RemoteError(f"slot {dut} is ambiguous with several hosts", code="invalid_request",
                              hint=f"name the host: {', '.join(f'{h.id}/{dut}' for h in hs)}")
        known: list[str] = []
        for h, r in self.fan("dut_list"):
            if not r.get("ok"):
                continue
            ids = [d["id"] for d in r["result"]]
            known += [f"{i}@{h.id}" for i in ids]
            if dut in ids:
                return h
        import difflib

        names = [k.split("@")[0] for k in known]
        close = difflib.get_close_matches(dut, names, n=3, cutoff=0.4)
        raise RemoteError(
            f"no DUT {dut!r} on any reachable host", code="not_found",
            hint=(f"did you mean: {', '.join(close)}? " if close else "")
            + f"known: {', '.join(known) or '(none — run `bsp probe`)'}"
            + (f"; unreachable: {', '.join(self.errors)}" if self.errors else ""))


def _safe_call(h: HostHandle, tool: str, args, timeout) -> dict[str, Any]:
    try:
        return h.call(tool, args, timeout)
    except Unreachable as e:
        return {"ok": False, "error": {"code": "unreachable", "message": str(e)}}
