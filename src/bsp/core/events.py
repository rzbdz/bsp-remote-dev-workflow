"""Thread-safe event bus (observer) feeding the status monitor (SSE, web, `bsp watch`)."""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Event:
    seq: int
    ts: float
    topic: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    def __init__(self, history: int = 500):
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._history: deque[Event] = deque(maxlen=history)
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        self._listeners: list[tuple[str, Any]] = []

    def on(self, topic: str, fn) -> None:
        """Call `fn(event)` in the publishing thread for every event whose topic is `topic` (or starts with `topic.`)."""
        self._listeners.append((topic, fn))

    def publish(self, topic: str, **data: Any) -> Event:
        with self._lock:
            ev = Event(next(self._seq), time.time(), topic, data)
            self._history.append(ev)
            subs = list(self._subscribers)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(_offer, q, ev)
            except RuntimeError:  # loop closed
                with self._lock:
                    self._subscribers.discard((loop, q))
        for prefix, fn in list(self._listeners):
            if topic == prefix or topic.startswith(prefix + "."):
                try:
                    fn(ev)
                except Exception:  # noqa: BLE001 - a listener must not break publishing
                    pass
        return ev

    def recent(self, limit: int = 100, since: int = 0) -> list[Event]:
        with self._lock:
            evs = [e for e in self._history if e.seq > since]
        return evs[-limit:] if limit else evs

    def subscribe(self, since: int | None = None) -> "Subscription":
        """Must be called from a running event loop. ``since`` replays history after that seq."""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            for e in self._history:
                if since is not None and e.seq > since:
                    _offer(q, e)
            self._subscribers.add((loop, q))
        return Subscription(self, loop, q)

    def _unsubscribe(self, key) -> None:
        with self._lock:
            self._subscribers.discard(key)


class Subscription:
    def __init__(self, bus: EventBus, loop: asyncio.AbstractEventLoop, q: asyncio.Queue):
        self._bus, self._key, self._q = bus, (loop, q), q

    async def get(self, timeout: float | None = None) -> Event | None:
        """Next event, or None on timeout (safe to call again afterwards)."""
        try:
            return await asyncio.wait_for(self._q.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self._bus._unsubscribe(self._key)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _offer(q: asyncio.Queue, ev: Event) -> None:
    if q.full():  # slow consumer: drop oldest, keep newest
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(ev)
