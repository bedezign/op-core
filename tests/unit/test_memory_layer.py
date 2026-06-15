"""Tests for :class:`op_core.backends.stack.MemoryLayer` (design section 4).

``MemoryLayer`` is the in-process read-write layer: an LRU ``_Store`` of
monotonic-stamped entries with TTL expiry owned by the layer (the resolver owns
locking, so the layer carries none). ``lookup`` returns the ``CacheEntry`` —
positive, a stored miss (``_NOT_FOUND``), or ``None`` when absent or expired;
the resolver, not the layer, interprets a miss.

Ported read-caching cases from ``test_caching_backend.py`` (the retired
``CachingBackend``'s ``read`` memoization, minus the ``is_expired`` hook and
``get_item`` memoization, which do not carry over — design section 5.5).
"""

from __future__ import annotations

import pytest

from op_core.backends import stack
from op_core.backends.caching import _NOT_FOUND
from op_core.backends.stack import MemoryLayer, WritableCacheLayer


class TestMemoryLayerBasics:
    def test_store_then_lookup_returns_value(self) -> None:
        layer = MemoryLayer(ttl=300.0)
        layer.store("op://v/a", "secret")
        entry = layer.lookup("op://v/a")
        assert entry is not None
        assert entry.value == "secret"

    def test_lookup_absent_returns_none(self) -> None:
        assert MemoryLayer(ttl=300.0).lookup("op://v/missing") is None

    def test_stored_miss_round_trips(self) -> None:
        # The layer stores and returns the miss sentinel verbatim; it does not
        # raise — interpreting the miss is the resolver's job.
        layer = MemoryLayer(ttl=300.0)
        layer.store("op://v/missing", _NOT_FOUND)
        entry = layer.lookup("op://v/missing")
        assert entry is not None
        assert entry.value is _NOT_FOUND

    def test_store_overwrites_previous_value(self) -> None:
        layer = MemoryLayer(ttl=300.0)
        layer.store("op://v/a", "old")
        layer.store("op://v/a", "new")
        entry = layer.lookup("op://v/a")
        assert entry is not None
        assert entry.value == "new"

    def test_satisfies_writable_cache_layer(self) -> None:
        assert isinstance(MemoryLayer(ttl=300.0), WritableCacheLayer)


class TestMemoryLayerTTL:
    def test_expired_entry_not_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(stack, "_monotonic", lambda: 1000.0)
        layer = MemoryLayer(ttl=300.0)
        layer.store("op://v/a", "secret")
        monkeypatch.setattr(stack, "_monotonic", lambda: 1000.0 + 301)
        assert layer.lookup("op://v/a") is None

    def test_within_ttl_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(stack, "_monotonic", lambda: 1000.0)
        layer = MemoryLayer(ttl=300.0)
        layer.store("op://v/a", "secret")
        monkeypatch.setattr(stack, "_monotonic", lambda: 1000.0 + 299)
        entry = layer.lookup("op://v/a")
        assert entry is not None
        assert entry.value == "secret"

    def test_boundary_exactly_ttl_still_fresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Boundary is "> ttl", so an age of exactly ttl is still fresh.
        monkeypatch.setattr(stack, "_monotonic", lambda: 1000.0)
        layer = MemoryLayer(ttl=300.0)
        layer.store("op://v/a", "secret")
        monkeypatch.setattr(stack, "_monotonic", lambda: 1300.0)
        assert layer.lookup("op://v/a") is not None

    def test_expired_lookup_evicts_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An expired lookup drops the entry, so the store does not accumulate dead keys.
        monkeypatch.setattr(stack, "_monotonic", lambda: 1000.0)
        layer = MemoryLayer(ttl=300.0)
        layer.store("op://v/a", "secret")
        monkeypatch.setattr(stack, "_monotonic", lambda: 2000.0)
        layer.lookup("op://v/a")
        assert len(layer._store) == 0

    def test_negative_ttl_never_serves(self) -> None:
        # A non-positive TTL means an entry is stale the instant it is stored.
        layer = MemoryLayer(ttl=-1.0)
        layer.store("op://v/a", "secret")
        assert layer.lookup("op://v/a") is None


class TestMemoryLayerLRU:
    def test_max_entries_evicts_oldest(self) -> None:
        layer = MemoryLayer(ttl=300.0, max_entries=2)
        layer.store("a", "1")
        layer.store("b", "2")
        layer.store("c", "3")  # evicts "a"
        assert layer.lookup("a") is None
        assert layer.lookup("b") is not None
        assert layer.lookup("c") is not None

    def test_lookup_marks_recent(self) -> None:
        layer = MemoryLayer(ttl=300.0, max_entries=2)
        layer.store("a", "1")
        layer.store("b", "2")
        layer.lookup("a")  # touch "a" so it becomes most-recently-used
        layer.store("c", "3")  # evicts "b" (now oldest)
        assert layer.lookup("b") is None
        assert layer.lookup("a") is not None
        assert layer.lookup("c") is not None

    def test_max_entries_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            MemoryLayer(ttl=300.0, max_entries=0)


class TestMemoryLayerClear:
    def test_clear_empties_everything(self) -> None:
        layer = MemoryLayer(ttl=300.0)
        layer.store("a", "1")
        layer.store("b", _NOT_FOUND)
        layer.clear()
        assert layer.lookup("a") is None
        assert layer.lookup("b") is None

    def test_clear_misses_drops_misses_keeps_values(self) -> None:
        layer = MemoryLayer(ttl=300.0)
        layer.store("hit", "real")
        layer.store("miss", _NOT_FOUND)
        layer.clear_misses()
        hit = layer.lookup("hit")
        assert hit is not None
        assert hit.value == "real"
        assert layer.lookup("miss") is None
