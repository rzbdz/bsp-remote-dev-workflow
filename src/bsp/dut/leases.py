"""Leases: who may drive a shared resource right now.

A lease is (holder, token, expiry). Mutating operations must present the token;
read-only ones never need it. Expired leases are free. State is a JSON file so
leases survive a service restart.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from ..core.errors import Conflict, Forbidden, InvalidRequest


class LeaseManager:
    MAX_TTL = 24 * 3600

    def __init__(self, path: Path, kind: str = "DUT"):
        self.path = Path(path)
        self.kind = kind
        self._lock = threading.Lock()
        self._leases: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            try:
                self._leases = json.loads(self.path.read_text())
            except ValueError:
                self._leases = {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._leases, indent=2))
        tmp.replace(self.path)

    def _live(self, rid: str) -> dict[str, Any] | None:
        lease = self._leases.get(rid)
        if lease and lease["expires"] > time.time():
            return lease
        return None

    def move(self, old: str, new: str) -> None:
        """A renamed resource keeps its lease (holder, token and expiry)."""
        with self._lock:
            if old in self._leases:
                self._leases[new] = self._leases.pop(old)
                self._save()

    def view(self, rid: str) -> dict[str, Any] | None:
        lease = self._live(rid)
        if not lease:
            return None
        return {k: v for k, v in lease.items() if k != "token"} | {
            "expires_in_s": int(lease["expires"] - time.time())}

    def acquire(self, rid: str, holder: str, ttl_s: int, purpose: str = "", token: str | None = None) -> dict[str, Any]:
        if not holder:
            raise InvalidRequest("a lease needs a holder name", hint="pass --holder <you/agent-id>")
        ttl_s = max(60, min(int(ttl_s), self.MAX_TTL))
        with self._lock:
            cur = self._live(rid)
            if cur and token != cur["token"]:
                raise Conflict(
                    f"{self.kind} {rid} is leased by {cur['holder']} for another {int(cur['expires'] - time.time())}s"
                    + (f" ({cur['purpose']})" if cur.get("purpose") else ""),
                    hint=f"wait, ask {cur['holder']} to release it, or pick another {self.kind} (`bsp dut list`)",
                    details=self.view(rid),
                )
            now = time.time()
            lease = {"resource": rid, "holder": holder, "purpose": purpose,
                     "token": cur["token"] if cur else secrets.token_hex(8),
                     "acquired": cur["acquired"] if cur else now, "expires": now + ttl_s}
            self._leases[rid] = lease
            self._save()
            return dict(lease) | {"expires_in_s": ttl_s}

    def renew(self, rid: str, token: str | None, ttl_s: int) -> dict[str, Any]:
        """Extend a live lease held with `token`, keeping its holder, purpose and token."""
        with self._lock:
            cur = self._live(rid)
            if not cur:
                raise Forbidden(f"{self.kind} {rid} is not leased (any more)",
                                hint=f"leases end when they expire; acquire it again: `bsp dut acquire {rid}`")
            if token != cur["token"]:
                raise Forbidden(f"{self.kind} {rid} is leased by {cur['holder']}; token does not match",
                                hint="only the holder can renew it", details=self.view(rid))
            ttl_s = max(60, min(int(ttl_s), self.MAX_TTL))
            cur["expires"] = time.time() + ttl_s
            self._save()
            return dict(cur) | {"expires_in_s": ttl_s}

    def check(self, rid: str, token: str | None) -> dict[str, Any]:
        """Require a live lease held with `token` for a mutating operation."""
        cur = self._live(rid)
        if not cur:
            raise Forbidden(
                f"{self.kind} {rid} is not leased; mutating operations need a lease",
                hint=f"`bsp dut acquire {rid} --holder <name>` first (the CLI remembers the token)",
            )
        if token != cur["token"]:
            raise Forbidden(
                f"{self.kind} {rid} is leased by {cur['holder']}; wrong or missing lease token",
                hint="pass --lease <token> / set BSP_LEASE, or acquire it once it is released",
                details=self.view(rid),
            )
        return cur

    def release(self, rid: str, token: str | None, force: bool = False) -> dict[str, Any]:
        with self._lock:
            cur = self._live(rid)
            if not cur:
                self._leases.pop(rid, None)
                self._save()
                return {"resource": rid, "released": False, "message": "was not leased"}
            if token != cur["token"] and not force:
                raise Forbidden(f"{self.kind} {rid} is leased by {cur['holder']}; token does not match",
                                hint="only the holder can release it (or an operator with --force)")
            del self._leases[rid]
            self._save()
            return {"resource": rid, "released": True, "holder": cur["holder"]}
