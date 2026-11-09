"""Timeout guard (decorator). Hardware calls that block in the kernel must not
take the whole service down: each call runs on a worker and is abandoned after
``timeout`` seconds, flipping the backend into ``degraded`` state."""

from __future__ import annotations

import concurrent.futures as cf
import threading
import time
from typing import Any, Callable

from ...core.errors import HardwareError, HardwareTimeout
from .base import GpioBackend, LineStatus


class GuardedBackend(GpioBackend):
    def __init__(self, inner: GpioBackend, timeout: float = 3.0, workers: int = 4):
        self.inner = inner
        self.name = inner.name
        self.timeout = timeout
        self._pool = cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"gpio-{inner.name}")
        self._lock = threading.Lock()
        self.hung_calls = 0
        self.last_error: dict[str, Any] | None = None

    def _call(self, what: str, fn: Callable[..., Any], *args: Any) -> Any:
        fut = self._pool.submit(fn, *args)
        try:
            return fut.result(timeout=self.timeout)
        except cf.TimeoutError:
            with self._lock:
                self.hung_calls += 1
                self.last_error = {"ts": time.time(), "op": what, "args": list(args), "error": "timeout"}
            raise HardwareTimeout(
                f"{self.name}: {what}{tuple(args)} did not complete within {self.timeout}s",
                hint="kernel GPIO/pinctrl call is blocked; check `dmesg` and D-state processes, a host reboot may be required",
            ) from None
        except HardwareError as e:
            self.last_error = {"ts": time.time(), "op": what, "args": list(args), "error": e.message}
            raise

    def set_output(self, gpio: int, value: int) -> None:
        self._call("set_output", self.inner.set_output, gpio, value)

    def set_input(self, gpio: int) -> None:
        self._call("set_input", self.inner.set_input, gpio)

    def status(self, gpio: int) -> LineStatus:
        return self._call("status", self.inner.status, gpio)

    def free(self, gpio: int) -> None:
        self._call("free", self.inner.free, gpio)

    def close(self) -> None:
        self.inner.close()
        self._pool.shutdown(wait=False, cancel_futures=True)

    @property
    def degraded(self) -> bool:
        return self.hung_calls > 0

    def info(self) -> dict:
        d = dict(self.inner.info())
        d.update(timeout_s=self.timeout, degraded=self.degraded, hung_calls=self.hung_calls, last_error=self.last_error)
        return d
