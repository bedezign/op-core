"""Tests for :mod:`op_core.backends.caching`."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

import pytest

from op_core.backends.caching import (
    _NOT_FOUND,
    AsyncCachingBackend,
    CacheEntry,
    CachingBackend,
    _Store,
    ttl_is_expired,
)
from op_core.exceptions import OpNotFoundError
from op_core.items import Item, ItemRef, ItemSummary, VaultSummary

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(item_id: str, *, vault_id: str = "vault-1", title: str = "Item") -> Item:
    return Item(
        id=item_id,
        title=title,
        vault_id=vault_id,
        vault_name=vault_id,
        category="LOGIN",
        tags=(),
        sections=(),
        fields=(),
    )


class StubBackend:
    """Counts calls so tests can verify caching prevented passthrough."""

    def __init__(
        self,
        *,
        refs: dict[str, str] | None = None,
        items: list[Item] | None = None,
    ) -> None:
        self._refs = refs or {}
        self._items = items or []
        self.read_count = 0
        self.get_item_count = 0
        self.list_items_count = 0
        self.list_vaults_count = 0

    def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
        self.read_count += 1
        if reference in self._refs:
            return self._refs[reference]
        if default_value is not None:
            return default_value
        raise OpNotFoundError(f"missing: {reference}")

    def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        self.list_items_count += 1
        return [
            ItemSummary(
                id=i.id,
                title=i.title,
                vault_id=i.vault_id,
                vault_name=i.vault_name,
                category=i.category,
                tags=i.tags,
            )
            for i in self._items
        ]

    def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        self.get_item_count += 1
        item_id = item if isinstance(item, str) else item.id
        effective_vault = vault
        if effective_vault is None and not isinstance(item, str):
            effective_vault = item.vault_id
        for candidate in self._items:
            if candidate.id != item_id:
                continue
            if effective_vault is not None and candidate.vault_id != effective_vault:
                continue
            return candidate
        raise OpNotFoundError(f"item not found: {item_id}")

    def list_vaults(self) -> list[VaultSummary]:
        self.list_vaults_count += 1
        seen: dict[str, VaultSummary] = {}
        for it in self._items:
            if it.vault_id not in seen:
                seen[it.vault_id] = VaultSummary(id=it.vault_id, name=it.vault_name)
        return list(seen.values())


class AsyncStubBackend:
    def __init__(
        self,
        *,
        refs: dict[str, str] | None = None,
        items: list[Item] | None = None,
    ) -> None:
        self._sync = StubBackend(refs=refs, items=items)

    @property
    def read_count(self) -> int:
        return self._sync.read_count

    @property
    def get_item_count(self) -> int:
        return self._sync.get_item_count

    @property
    def list_vaults_count(self) -> int:
        return self._sync.list_vaults_count

    async def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
        return self._sync.read(reference, default_value=default_value)

    async def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        return self._sync.list_items(vault=vault, tags=tags, categories=categories)

    async def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        return self._sync.get_item(item, vault=vault)

    async def list_vaults(self) -> list[VaultSummary]:
        return self._sync.list_vaults()


def _entry(key: str, value: Any = "v") -> CacheEntry:
    return CacheEntry(key=key, value=value, cached_at=time.monotonic(), metadata={})


# ---------------------------------------------------------------------------
# _Store
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# ttl_is_expired
# ---------------------------------------------------------------------------


class TestTTLIsExpired:
    def test_fresh_entry_not_expired(self) -> None:
        backend = StubBackend()
        entry = _entry("k")
        assert ttl_is_expired(10.0)(backend, entry) is False

    def test_old_entry_expired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = StubBackend()
        entry = CacheEntry(key="k", value="v", cached_at=0.0, metadata={})
        monkeypatch.setattr(time, "monotonic", lambda: 100.0)
        assert ttl_is_expired(5.0)(backend, entry) is True

    def test_at_boundary_not_expired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = StubBackend()
        entry = CacheEntry(key="k", value="v", cached_at=0.0, metadata={})
        monkeypatch.setattr(time, "monotonic", lambda: 5.0)
        # boundary is "> ttl", so exactly ttl is still fresh
        assert ttl_is_expired(5.0)(backend, entry) is False


# ---------------------------------------------------------------------------
# CachingBackend.read
# ---------------------------------------------------------------------------


class TestCachingBackendRead:
    def test_second_call_served_from_cache(self) -> None:
        inner = StubBackend(refs={"op://v/i/f": "secret"})
        cache = CachingBackend(inner)
        assert cache.read("op://v/i/f") == "secret"
        assert cache.read("op://v/i/f") == "secret"
        assert inner.read_count == 1

    def test_different_references_cached_independently(self) -> None:
        inner = StubBackend(refs={"op://v/a": "1", "op://v/b": "2"})
        cache = CachingBackend(inner)
        assert cache.read("op://v/a") == "1"
        assert cache.read("op://v/b") == "2"
        assert cache.read("op://v/a") == "1"
        assert inner.read_count == 2

    def test_expired_entry_refetches(self) -> None:
        inner = StubBackend(refs={"op://v/a": "1"})
        expired = {"flag": False}

        def is_expired(_b: Any, _e: CacheEntry) -> bool:
            return expired["flag"]

        cache = CachingBackend(inner, is_expired=is_expired)
        cache.read("op://v/a")
        expired["flag"] = True
        cache.read("op://v/a")
        assert inner.read_count == 2

    def test_missing_raises_not_found(self) -> None:
        inner = StubBackend()
        cache = CachingBackend(inner)
        with pytest.raises(OpNotFoundError):
            cache.read("op://v/missing")

    def test_missing_second_call_does_not_hit_inner(self) -> None:
        inner = StubBackend()
        cache = CachingBackend(inner)
        with pytest.raises(OpNotFoundError):
            cache.read("op://v/missing")
        with pytest.raises(OpNotFoundError):
            cache.read("op://v/missing")
        assert inner.read_count == 1

    def test_missing_second_call_with_default_returns_default(self) -> None:
        inner = StubBackend()
        cache = CachingBackend(inner)
        with pytest.raises(OpNotFoundError):
            cache.read("op://v/missing")
        assert cache.read("op://v/missing", default_value="fallback") == "fallback"
        assert inner.read_count == 1

    def test_first_call_with_default_caches_miss(self) -> None:
        inner = StubBackend()
        cache = CachingBackend(inner)
        assert cache.read("op://v/missing", default_value="x") == "x"
        # second call without default now raises — because cached state
        # is "not found", not "resolves to x"
        with pytest.raises(OpNotFoundError):
            cache.read("op://v/missing")
        assert inner.read_count == 1

    def test_clear_misses_lets_reference_refetch(self) -> None:
        inner = StubBackend()
        cache = CachingBackend(inner)
        with pytest.raises(OpNotFoundError):
            cache.read("op://v/missing")
        cache.clear_misses()
        with pytest.raises(OpNotFoundError):
            cache.read("op://v/missing")
        assert inner.read_count == 2


# ---------------------------------------------------------------------------
# CachingBackend.get_item
# ---------------------------------------------------------------------------


class TestCachingBackendGetItem:
    def test_caches_by_vault_and_id(self) -> None:
        item = _make_item("abc", vault_id="v1")
        inner = StubBackend(items=[item])
        cache = CachingBackend(inner)
        cache.get_item("abc", vault="v1")
        cache.get_item("abc", vault="v1")
        assert inner.get_item_count == 1

    def test_same_id_different_vaults_independent(self) -> None:
        a = _make_item("shared", vault_id="v1")
        b = _make_item("shared", vault_id="v2")
        inner = StubBackend(items=[a, b])
        cache = CachingBackend(inner)
        assert cache.get_item("shared", vault="v1").vault_id == "v1"
        assert cache.get_item("shared", vault="v2").vault_id == "v2"
        assert inner.get_item_count == 2
        # verify cache hits on repeat
        cache.get_item("shared", vault="v1")
        cache.get_item("shared", vault="v2")
        assert inner.get_item_count == 2

    def test_missing_raises_and_caches(self) -> None:
        inner = StubBackend()
        cache = CachingBackend(inner)
        with pytest.raises(OpNotFoundError):
            cache.get_item("ghost", vault="v1")
        with pytest.raises(OpNotFoundError):
            cache.get_item("ghost", vault="v1")
        assert inner.get_item_count == 1

    def test_item_summary_uses_item_vault_id(self) -> None:
        item = _make_item("abc", vault_id="v1")
        summary = ItemSummary(
            id="abc",
            title="Item",
            vault_id="v1",
            vault_name="v1",
            category="LOGIN",
            tags=(),
        )
        inner = StubBackend(items=[item])
        cache = CachingBackend(inner)
        cache.get_item(summary)
        cache.get_item("abc", vault="v1")
        assert inner.get_item_count == 1

    def test_full_item_ref_shares_key_with_string_ref(self) -> None:
        item = _make_item("abc", vault_id="v1")
        inner = StubBackend(items=[item])
        cache = CachingBackend(inner)
        cache.get_item(item)
        cache.get_item("abc", vault="v1")
        assert inner.get_item_count == 1


# ---------------------------------------------------------------------------
# CachingBackend.list_items
# ---------------------------------------------------------------------------


class TestCachingBackendPassthrough:
    def test_list_items_always_passes_through(self) -> None:
        item = _make_item("abc")
        inner = StubBackend(items=[item])
        cache = CachingBackend(inner)
        cache.list_items()
        cache.list_items()
        cache.list_items()
        assert inner.list_items_count == 3

    def test_list_items_does_not_store_anything(self) -> None:
        inner = StubBackend()
        cache = CachingBackend(inner)
        cache.list_items()
        assert len(cache._store) == 0

    def test_list_vaults_always_passes_through(self) -> None:
        item = _make_item("abc")
        inner = StubBackend(items=[item])
        cache = CachingBackend(inner)
        cache.list_vaults()
        cache.list_vaults()
        cache.list_vaults()
        assert inner.list_vaults_count == 3

    def test_list_vaults_does_not_store_anything(self) -> None:
        inner = StubBackend(items=[_make_item("abc")])
        cache = CachingBackend(inner)
        cache.list_vaults()
        assert len(cache._store) == 0


# ---------------------------------------------------------------------------
# CachingBackend.clear / clear_misses
# ---------------------------------------------------------------------------


class TestCachingBackendClear:
    def test_clear_empties_everything(self) -> None:
        inner = StubBackend(refs={"op://v/a": "1"})
        cache = CachingBackend(inner)
        cache.read("op://v/a")
        cache.clear()
        cache.read("op://v/a")
        assert inner.read_count == 2

    def test_clear_misses_keeps_hits(self) -> None:
        inner = StubBackend(refs={"op://v/a": "1"})
        cache = CachingBackend(inner)
        cache.read("op://v/a")
        with pytest.raises(OpNotFoundError):
            cache.read("op://v/missing")
        cache.clear_misses()
        cache.read("op://v/a")
        assert inner.read_count == 2  # 1 hit + 1 miss, no extras
        with pytest.raises(OpNotFoundError):
            cache.read("op://v/missing")
        assert inner.read_count == 3


# ---------------------------------------------------------------------------
# AsyncCachingBackend
# ---------------------------------------------------------------------------


class TestAsyncCachingBackend:
    async def test_read_caches_across_awaits(self) -> None:
        inner = AsyncStubBackend(refs={"op://v/a": "1"})
        cache = AsyncCachingBackend(inner)
        assert await cache.read("op://v/a") == "1"
        assert await cache.read("op://v/a") == "1"
        assert inner.read_count == 1

    async def test_get_item_caches(self) -> None:
        item = _make_item("abc")
        inner = AsyncStubBackend(items=[item])
        cache = AsyncCachingBackend(inner)
        await cache.get_item("abc", vault="vault-1")
        await cache.get_item("abc", vault="vault-1")
        assert inner.get_item_count == 1

    async def test_missing_read_cached_as_miss(self) -> None:
        inner = AsyncStubBackend()
        cache = AsyncCachingBackend(inner)
        with pytest.raises(OpNotFoundError):
            await cache.read("op://v/missing")
        with pytest.raises(OpNotFoundError):
            await cache.read("op://v/missing")
        assert inner.read_count == 1

    async def test_clear(self) -> None:
        inner = AsyncStubBackend(refs={"op://v/a": "1"})
        cache = AsyncCachingBackend(inner)
        await cache.read("op://v/a")
        await cache.clear()
        await cache.read("op://v/a")
        assert inner.read_count == 2

    async def test_clear_misses(self) -> None:
        inner = AsyncStubBackend(refs={"op://v/a": "1"})
        cache = AsyncCachingBackend(inner)
        await cache.read("op://v/a")
        with pytest.raises(OpNotFoundError):
            await cache.read("op://v/missing")
        await cache.clear_misses()
        await cache.read("op://v/a")
        assert inner.read_count == 2
        with pytest.raises(OpNotFoundError):
            await cache.read("op://v/missing")
        assert inner.read_count == 3

    async def test_list_items_passthrough(self) -> None:
        inner = AsyncStubBackend(items=[_make_item("abc")])
        cache = AsyncCachingBackend(inner)
        await cache.list_items()
        await cache.list_items()
        assert inner._sync.list_items_count == 2

    async def test_list_vaults_passthrough(self) -> None:
        inner = AsyncStubBackend(items=[_make_item("abc")])
        cache = AsyncCachingBackend(inner)
        await cache.list_vaults()
        await cache.list_vaults()
        assert inner.list_vaults_count == 2

    async def test_concurrent_reads_share_result(self) -> None:
        # Two coroutines reading the same key serially (after each other)
        # should still only trigger one inner call on the second.
        inner = AsyncStubBackend(refs={"op://v/a": "1"})
        cache = AsyncCachingBackend(inner)
        a, b = await asyncio.gather(
            cache.read("op://v/a"),
            cache.read("op://v/a"),
        )
        assert a == b == "1"
        # Concurrent reads may race — both might hit inner before either writes.
        # We only assert a subsequent read is served from cache.
        await cache.read("op://v/a")
        count_after = inner.read_count
        await cache.read("op://v/a")
        assert inner.read_count == count_after
