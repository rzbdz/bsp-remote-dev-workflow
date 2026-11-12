"""Announce this host to a registry (`server.announce` in the host config).

A thread POSTs the host id, port, addresses and DUT ids to the registry's
/api/v1/registry/announce every `interval_s`. The registry records the address
the request came from, so a host behind NAT or on DHCP is found where clients can
reach it. Failures are logged and retried; they never affect the host itself.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from typing import Any, Callable

from ..core.net import local_addresses as _local_addresses

log = logging.getLogger(__name__)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def local_addresses() -> list[str]:
    """Global addresses of this machine (best effort, no extra dependencies)."""
    return _local_addresses()


class Announcer:
    def __init__(self, cfg: dict[str, Any], payload: Callable[[], dict[str, Any]]):
        self.url = str(cfg["url"]).rstrip("/") + "/api/v1/registry/announce"
        self.interval = max(10.0, float(cfg.get("interval_s", 60)))
        self.token = cfg.get("token") or os.environ.get("BSP_REGISTRY_TOKEN")
        self.extra = {"ssh_user": cfg.get("ssh_user"), "addresses": list(cfg.get("addresses") or [])}
        self.payload = payload
        self.on_reply: Callable[[dict[str, Any]], None] = lambda result: None
        self.last: dict[str, Any] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="announce", daemon=True)

    def body(self) -> dict[str, Any]:
        b = {**self.payload(), "ssh_user": self.extra["ssh_user"]}
        b["addresses"] = self.extra["addresses"] or local_addresses()
        return b

    def once(self) -> dict[str, Any]:
        req = urllib.request.Request(self.url, data=json.dumps(self.body()).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with _OPENER.open(req, timeout=15) as r:
            return json.loads(r.read())

    def _run(self) -> None:
        failing = False
        while not self._stop.is_set():
            try:
                res = self.once()
                self.last = {"ok": bool(res.get("ok")), "result": res.get("result"), "error": res.get("error")}
                if res.get("ok"):
                    self.on_reply(res.get("result") or {})
                if failing:
                    log.info("announce: registry %s reachable again", self.url)
                failing = False
            except Exception as e:  # noqa: BLE001 - keep trying; the registry may be down for a while
                self.last = {"ok": False, "error": str(e)}
                if not failing:
                    log.warning("announce to %s failed: %s (retrying every %gs)", self.url, e, self.interval)
                failing = True
            self._wake.wait(self.interval)
            self._wake.clear()

    def poke(self) -> None:
        """Announce now rather than at the next interval (a board's cable came or went)."""
        self._wake.set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
