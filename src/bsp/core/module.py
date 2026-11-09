"""Module plugin contract: a module contributes commands, optional routes and status."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .commands import Command
from .errors import NotFound
from .events import EventBus
from .jobs import JobManager
from .registry import Registry


@dataclass
class ModuleContext:
    host_id: str
    bus: EventBus
    state_dir: Path      # writable runtime state (overlays, caches)
    config_dir: Path     # directory of the host config file (resolve relative paths against it)
    jobs: JobManager | None = None
    port: int = 8765     # where this host's API listens (for helpers that call back)
    auth_token: str | None = None   # the token this host's API asks of writers (server.auth_token)
    # in-process service registry: modules publish objects others may use
    # (e.g. gpio_relay publishes its service; the dut module drives it)
    services: dict[str, Any] = field(default_factory=dict)

    def service(self, name: str) -> Any:
        try:
            return self.services[name]
        except KeyError:
            raise NotFound(
                f"service '{name}' is not available on this host",
                hint=f"enable the module that provides it in the host config (loaded: {sorted(self.services)})",
            ) from None

    def module_state_dir(self, name: str) -> Path:
        p = self.state_dir / name
        p.mkdir(parents=True, exist_ok=True)
        return p


class Module(ABC):
    name: str = ""
    title: str = ""

    def __init__(self, ctx: ModuleContext, config: dict[str, Any]):
        self.ctx = ctx
        self.config = config

    @abstractmethod
    def commands(self) -> list[Command]: ...

    def router(self):
        """Optional extra FastAPI router (beyond auto-generated command routes)."""
        return None

    def startup(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {}

    def open_paths(self) -> list[str]:
        """API path prefixes whose writes need no token on a host that has one."""
        return []


MODULES: Registry[type[Module]] = Registry("module")
