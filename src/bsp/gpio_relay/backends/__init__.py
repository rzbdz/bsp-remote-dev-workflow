"""Backend plugin registry + factory."""

from __future__ import annotations

import importlib
from typing import Any

from ...core.registry import Registry
from .base import GpioBackend, LineStatus

BACKENDS: Registry[type[GpioBackend]] = Registry("gpio backend")

# importing registers them; gpiod imports its library lazily so this never fails
for _m in ("sysfs", "mock", "gpiod_backend"):
    importlib.import_module(f"{__name__}.{_m}")


def create_backend(name: str, options: dict[str, Any] | None = None, timeout: float = 3.0) -> GpioBackend:
    from .guarded import GuardedBackend

    inner = BACKENDS.get(name)(**(options or {}))
    return GuardedBackend(inner, timeout=timeout)


__all__ = ["BACKENDS", "GpioBackend", "LineStatus", "create_backend"]
