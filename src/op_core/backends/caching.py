"""In-process cache store and entry types.

The engine behind :class:`~op_core.backends.stack.MemoryLayer`: an LRU
:class:`_Store` of TTL-stamped :class:`CacheEntry` objects, plus the private
``_NOT_FOUND`` negative-cache sentinel. Nothing here locks or reads the clock --
the store is passive; the resolver owns locking and each layer owns its expiry.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


class _NotFound:
    """Sentinel for cached negative lookups."""

    _instance: _NotFound | None = None

    def __new__(cls) -> _NotFound:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<_NOT_FOUND>"


_NOT_FOUND = _NotFound()


@dataclass(frozen=True)
class CacheEntry:
    key: str  # cache key
    value: Any  # resolved value, or the _NOT_FOUND sentinel for negative cache
    cached_at: float  # time.monotonic() at insertion
    metadata: dict[str, Any] = field(default_factory=dict)  # arbitrary per-entry bag


class _Store:
    """LRU-capped cache storage. No locking, no time logic."""

    def __init__(self, max_entries: int) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max = max_entries
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()

    def get(self, key: str) -> CacheEntry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry

    def put(self, key: str, entry: CacheEntry) -> None:
        if key in self._entries:
            self._entries.move_to_end(key)
        self._entries[key] = entry
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def delete(self, key: str) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    def clear_misses(self) -> None:
        dead = [k for k, e in self._entries.items() if e.value is _NOT_FOUND]
        for k in dead:
            del self._entries[k]

    def items(self) -> list[tuple[str, CacheEntry]]:
        """Snapshot of (key, entry) pairs in LRU order. For persistence/inspection."""
        return list(self._entries.items())

    def __len__(self) -> int:
        return len(self._entries)
