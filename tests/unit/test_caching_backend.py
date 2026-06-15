"""Tests for the kept in-process cache internal: the LRU ``_Store``.

The ``CachingBackend`` / ``AsyncCachingBackend`` decorators were removed in 0.6.0
(design 5.5). Their behavioral cases map to successors as follows:

* ``read()`` memoization       -> ``MemoryLayer`` + ``ResolverStack``
  (``test_memory_layer.py``, ``test_resolver_stack.py``, ``test_async_resolver_stack.py``)
* negative caching / misses    -> ``MemoryLayer`` stored-miss + resolver terminal-miss
* ``default_value`` / offline  -> resolver semantics (``test_resolver_stack.py``)
* ``clear`` / ``clear_misses`` -> ``MemoryLayer`` + ``ResolverStack`` fan-out
* ``list_items`` / ``list_vaults`` passthrough -> resolver delegation (``test_resolver_stack.py``)
* ``get_item`` memoization     -> dropped; the stack routes ``get_item`` to the source (design 3.4)
* ``is_expired`` hook / ``ttl_is_expired`` -> dropped; layers expire on TTL only (design 5.5)

What remains here is the engine internal that outlived the decorators and now
serves ``MemoryLayer``: the LRU ``_Store``.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from op_core.backends.caching import _NOT_FOUND, CacheEntry, _Store


def _entry(key: str, value: Any = "v") -> CacheEntry:
    return CacheEntry(key=key, value=value, cached_at=time.monotonic(), metadata={})


class TestStore:
    def test_get_missing_returns_none(self) -> None:
        store = _Store(max_entries=4)
        assert store.get("nope") is None

    def test_put_then_get(self) -> None:
        store = _Store(max_entries=4)
        entry = _entry("k", "val")
        store.put("k", entry)
        assert store.get("k") is entry

    def test_delete(self) -> None:
        store = _Store(max_entries=4)
        store.put("k", _entry("k"))
        store.delete("k")
        assert store.get("k") is None

    def test_clear(self) -> None:
        store = _Store(max_entries=4)
        store.put("a", _entry("a"))
        store.put("b", _entry("b"))
        store.clear()
        assert len(store) == 0

    def test_clear_misses_keeps_positive_entries(self) -> None:
        store = _Store(max_entries=4)
        store.put("hit", _entry("hit", "real"))
        store.put("miss", _entry("miss", _NOT_FOUND))
        store.clear_misses()
        assert store.get("hit") is not None
        assert store.get("miss") is None

    def test_lru_eviction_drops_oldest(self) -> None:
        store = _Store(max_entries=2)
        store.put("a", _entry("a"))
        store.put("b", _entry("b"))
        store.put("c", _entry("c"))
        assert store.get("a") is None
        assert store.get("b") is not None
        assert store.get("c") is not None

    def test_lru_get_marks_recent(self) -> None:
        store = _Store(max_entries=2)
        store.put("a", _entry("a"))
        store.put("b", _entry("b"))
        # touch 'a' so it becomes most recent
        store.get("a")
        store.put("c", _entry("c"))
        assert store.get("b") is None
        assert store.get("a") is not None
        assert store.get("c") is not None

    def test_max_entries_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            _Store(max_entries=0)
