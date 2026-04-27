"""Caching decorator backends.

:class:`CachingBackend` and :class:`AsyncCachingBackend` wrap any
:class:`~op_core.backends.base.Backend` / ``AsyncBackend`` to memoize the
results of ``read()`` and ``get_item()``. Results are kept in an LRU
store with a TTL; misses are cached too (negative caching) with a
private sentinel sharing the same expiry clock.

``list_items()`` always passes through — it is not considered safe to
cache automatically.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from op_core.exceptions import OpNotFoundError, OpOfflineError
from op_core.items import Item, ItemRef, ItemSummary, VaultSummary

if TYPE_CHECKING:
    from op_core.backends.base import AsyncBackend, Backend


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


IsExpired = Callable[["Backend", CacheEntry], bool]
AsyncIsExpired = Callable[["AsyncBackend", CacheEntry], Awaitable[bool]]


def ttl_is_expired(ttl: float) -> IsExpired:
    """Default expiry policy: entries stale after ``ttl`` seconds."""

    def _check(_backend: Backend, entry: CacheEntry) -> bool:
        return (time.monotonic() - entry.cached_at) > ttl

    return _check


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

    def __len__(self) -> int:
        return len(self._entries)


def _item_cache_key(item: ItemRef, vault: str | None) -> str:
    if isinstance(item, str):
        effective_vault = vault
        item_id = item
    else:
        item_id = item.id
        effective_vault = vault if vault is not None else item.vault_id
    return f"{effective_vault or ''}::{item_id}"


def _now() -> float:
    return time.monotonic()


class CachingBackend:
    """Sync caching decorator. Wraps any :class:`Backend`."""

    def __init__(
        self,
        inner: Backend,
        *,
        ttl: float = 300.0,
        max_entries: int = 1024,
        is_expired: IsExpired | None = None,
    ) -> None:
        self._inner = inner
        self._store = _Store(max_entries)
        self._lock = threading.Lock()
        self._is_expired: IsExpired = is_expired or ttl_is_expired(ttl)

    # Backend protocol ---------------------------------------------------

    def read(
        self,
        reference: str,
        *,
        default_value: str | None = None,
        online: bool = True,
    ) -> str:
        # Note: `online` is NOT forwarded to the inner backend. This layer
        # owns the "can we satisfy this without going upstream?" decision
        # for its own cache; the inner backend is only invoked on cache miss
        # with online=True, and only then when the caller has permitted it.
        # Stacked CachingBackend(CachingBackend(...)) would make the inner
        # guard a dead letter, which is acceptable because layered caching
        # is not a supported composition.
        key = reference
        entry = self._lookup(key)
        if entry is not None:
            return self._unpack_read(entry, reference, default_value)

        if not online:
            raise OpOfflineError(f"reference not cached: {reference}")

        try:
            value: Any = self._inner.read(reference)
        except OpNotFoundError:
            self._store_entry(key, _NOT_FOUND)
            if default_value is not None:
                return default_value
            raise OpNotFoundError(reference) from None
        self._store_entry(key, value)
        return value

    def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        return self._inner.list_items(vault=vault, tags=tags, categories=categories)

    def list_vaults(self) -> list[VaultSummary]:
        return self._inner.list_vaults()

    def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        key = _item_cache_key(item, vault)
        entry = self._lookup(key)
        if entry is not None:
            if entry.value is _NOT_FOUND:
                raise OpNotFoundError(key)
            return entry.value

        try:
            fetched = self._inner.get_item(item, vault=vault)
        except OpNotFoundError:
            self._store_entry(key, _NOT_FOUND)
            raise
        self._store_entry(key, fetched)
        return fetched

    # Cache control ------------------------------------------------------

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def clear_misses(self) -> None:
        with self._lock:
            self._store.clear_misses()

    # Internal -----------------------------------------------------------

    def _lookup(self, key: str) -> CacheEntry | None:
        """Return a live cache entry for ``key``, or ``None`` if absent or expired.

        Releases the lock before calling ``is_expired`` because the callback
        may re-enter the inner backend.
        """
        with self._lock:
            entry = self._store.get(key)
        if entry is None:
            return None
        if self._is_expired(self._inner, entry):
            with self._lock:
                self._store.delete(key)
            return None
        return entry

    def _store_entry(self, key: str, value: Any) -> None:
        entry = CacheEntry(key=key, value=value, cached_at=_now(), metadata={})
        with self._lock:
            self._store.put(key, entry)

    @staticmethod
    def _unpack_read(entry: CacheEntry, reference: str, default_value: str | None) -> str:
        if entry.value is _NOT_FOUND:
            if default_value is not None:
                return default_value
            raise OpNotFoundError(reference)
        return entry.value


class AsyncCachingBackend:
    """Async caching decorator. Wraps any :class:`AsyncBackend`."""

    def __init__(
        self,
        inner: AsyncBackend,
        *,
        ttl: float = 300.0,
        max_entries: int = 1024,
        is_expired: IsExpired | AsyncIsExpired | None = None,
    ) -> None:
        self._inner = inner
        self._store = _Store(max_entries)
        self._lock = asyncio.Lock()
        self._is_expired = is_expired or ttl_is_expired(ttl)

    async def read(
        self,
        reference: str,
        *,
        default_value: str | None = None,
        online: bool = True,
    ) -> str:
        key = reference
        entry = await self._lookup(key)
        if entry is not None:
            if entry.value is _NOT_FOUND:
                if default_value is not None:
                    return default_value
                raise OpNotFoundError(reference)
            return entry.value

        if not online:
            raise OpOfflineError(f"reference not cached: {reference}")

        try:
            value: Any = await self._inner.read(reference)
        except OpNotFoundError:
            await self._store_entry(key, _NOT_FOUND)
            if default_value is not None:
                return default_value
            raise OpNotFoundError(reference) from None
        await self._store_entry(key, value)
        return value

    async def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        return await self._inner.list_items(vault=vault, tags=tags, categories=categories)

    async def list_vaults(self) -> list[VaultSummary]:
        return await self._inner.list_vaults()

    async def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        key = _item_cache_key(item, vault)
        entry = await self._lookup(key)
        if entry is not None:
            if entry.value is _NOT_FOUND:
                raise OpNotFoundError(key)
            return entry.value

        try:
            fetched = await self._inner.get_item(item, vault=vault)
        except OpNotFoundError:
            await self._store_entry(key, _NOT_FOUND)
            raise
        await self._store_entry(key, fetched)
        return fetched

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()

    async def clear_misses(self) -> None:
        async with self._lock:
            self._store.clear_misses()

    async def _lookup(self, key: str) -> CacheEntry | None:
        async with self._lock:
            entry = self._store.get(key)
        if entry is None:
            return None
        # _is_expired is `IsExpired | AsyncIsExpired`. The sync variant's signature
        # types the backend as `Backend`, but `ttl_is_expired` ignores it and any
        # caller-supplied sync predicate is responsible for handling the async
        # backend if it actually inspects it. Cast resolves pyright variance.
        result = self._is_expired(cast(Any, self._inner), entry)
        if inspect.isawaitable(result):
            result = await result
        if result:
            async with self._lock:
                self._store.delete(key)
            return None
        return entry

    async def _store_entry(self, key: str, value: Any) -> None:
        entry = CacheEntry(key=key, value=value, cached_at=_now(), metadata={})
        async with self._lock:
            self._store.put(key, entry)
