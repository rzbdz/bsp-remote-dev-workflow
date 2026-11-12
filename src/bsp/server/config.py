"""Host configuration (<site>/hosts/<host>.yaml)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.env import resolve_refs
from ..core.errors import InvalidRequest


@dataclass
class HostConfig:
    path: Path
    host_id: str
    description: str = ""
    bind: str = "127.0.0.1"
    port: int = 8765
    state_dir: Path = Path("/var/lib/bsp")
    auth_token: str | None = None
    modules: dict[str, dict[str, Any]] = field(default_factory=dict)
    announce: dict[str, Any] | None = None   # registry this host announces itself to
    env: dict[str, str] = field(default_factory=dict)   # values hooks receive (top-level `env:`)

    @property
    def config_dir(self) -> Path:
        return self.path.parent

    @classmethod
    def load(cls, path: str | Path) -> "HostConfig":
        import yaml

        p = Path(path).resolve()
        with open(p, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        # Any string written as `env:NAME` takes the value bsp-hostd runs with (top level, server,
        # modules, ...); an unset variable refuses to start with a message naming it.
        raw = resolve_refs(raw, f"host config {p}")
        host = raw.get("host") or {}
        srv = raw.get("server") or {}
        env = raw.get("env") or {}
        if not isinstance(env, dict):
            raise InvalidRequest(f"{p}: `env:` must be a mapping of NAME: value",
                                 hint="e.g. env: {BSP_SERIAL_LOG_DIR: /var/log/serial}")
        state_dir = Path(os.environ.get("BSP_STATE_DIR") or srv.get("state_dir") or "/var/lib/bsp")
        if not state_dir.is_absolute():
            state_dir = (p.parent / state_dir).resolve()
        return cls(
            path=p,
            host_id=host.get("id", p.stem),
            description=host.get("description", ""),
            bind=os.environ.get("BSP_BIND") or srv.get("bind", "127.0.0.1"),
            port=int(os.environ.get("BSP_PORT") or srv.get("port", 8765)),
            state_dir=state_dir,
            auth_token=os.environ.get("BSP_TOKEN") or srv.get("auth_token") or None,
            modules={k: (v or {}) for k, v in (raw.get("modules") or {}).items()},
            announce=_announce(srv.get("announce")),
            env={str(k): ("" if v is None else str(v)) for k, v in env.items()},
        )


def _announce(cfg: Any) -> dict[str, Any] | None:
    """server.announce: {url, interval_s, ssh_user, addresses, token}; $BSP_REGISTRY_URL overrides url."""
    url = os.environ.get("BSP_REGISTRY_URL")
    if isinstance(cfg, str):
        cfg = {"url": cfg}
    cfg = dict(cfg or {})
    if url:
        cfg["url"] = url
    return cfg if cfg.get("url") else None
