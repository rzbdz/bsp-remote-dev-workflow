"""Generic named registry (plugin point for backends, channel modes, layouts, modules)."""

from __future__ import annotations

from typing import Generic, Iterator, TypeVar

from .errors import NotFound

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str):
        self.kind = kind
        self._items: dict[str, T] = {}

    def register(self, name: str, obj: T | None = None):
        """Register directly (``reg.register("x", obj)``) or as a decorator (``@reg.register("x")``)."""
        if obj is not None:
            self._items[name] = obj
            return obj

        def deco(o: T) -> T:
            self._items[name] = o
            return o

        return deco

    def get(self, name: str) -> T:
        try:
            return self._items[name]
        except KeyError:
            raise NotFound(
                f"unknown {self.kind} '{name}'",
                details={"available": self.names()},
            ) from None

    def __contains__(self, name: str) -> bool:
        return name in self._items

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def names(self) -> list[str]:
        return sorted(self._items)

    def items(self):
        return self._items.items()
