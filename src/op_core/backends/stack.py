"""The resolver stack and its layers.

A :class:`ResolverStack` (and its async twin :class:`AsyncResolverStack`)
composes an ordered list of cache *layers* over exactly one *source*
backend. A layer's role is the type that is placed, not a number to decode:
:class:`MemoryLayer` is an in-process read-write cache; the file layers (in
:mod:`op_core.backends.file_caching`) are a read-only observer and a read-write
persister. The source is the real backend, consulted last and only when the
read is allowed online.

This module holds the resolvers, the :class:`MemoryLayer`, and the structural
:class:`CacheLayer` / :class:`WritableCacheLayer` protocols the resolver walks
(design section 4). Caching is deliberately kept out of the ``Backend``
protocol (see :mod:`op_core.backends.base`); a layer is therefore not a
``Backend`` and a ``Backend`` is not a layer.

The sync and async resolvers share the same walk, back-fill, and validation
logic through the module-level helpers below — only the lock type
(:class:`threading.Lock` vs :class:`asyncio.Lock`), the ``await`` on the source,
and the source protocol differ. Keeping the logic in one place is deliberate:
it is the guard against the two stacks drifting apart.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from op_core.backends.base import AsyncBackend, Backend
from op_core.backends.caching import _NOT_FOUND, CacheEntry, _Store
from op_core.exceptions import OpNotFoundError, OpOfflineError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from op_core.items import Item, ItemRef, ItemSummary, VaultSummary


def _monotonic() -> float:
    """Indirection over :func:`time.monotonic` so tests can pin the clock."""
    return time.monotonic()


@runtime_checkable
class CacheLayer(Protocol):
    """A cache layer the resolver can read from.

    ``lookup`` returns a live :class:`~op_core.backends.caching.CacheEntry`
    (positive or a stored miss) or ``None`` when the layer has nothing live for
    the reference. The layer owns its own expiry; the resolver owns locking.
    """

    def lookup(self, reference: str) -> CacheEntry | None: ...


@runtime_checkable
class WritableCacheLayer(CacheLayer, Protocol):
    """A cache layer the resolver can also warm via back-fill.

    ``store`` takes the resolved value string or the module-private miss
    sentinel (typed ``object``). ``clear`` / ``clear_misses`` retract entries.
    """

    def store(self, reference: str, value: object) -> None: ...

    def clear(self) -> None: ...

    def clear_misses(self) -> None: ...


class MemoryLayer:
    """In-process read-write cache layer (design section 4).

    An LRU :class:`~op_core.backends.caching._Store` of monotonic-stamped
    entries. The layer owns TTL expiry in :meth:`lookup`; it carries **no lock**
    — the resolver owns locking, so a layer instance must not be shared between
    stacks. Memory-only (the same exposure as any local variable), so a default
    TTL is harmless here; the no-default rule is about disk (design section 6).

    :meth:`lookup` returns the stored :class:`CacheEntry` unchanged — positive,
    or a negative-cache record whose value is the private ``_NOT_FOUND``
    sentinel. The resolver, not the layer, turns a stored miss into a raised
    :class:`~op_core.exceptions.OpNotFoundError` or the caller's default.
    """

    def __init__(self, ttl: float = 300.0, *, max_entries: int = 1024) -> None:
        self._ttl = ttl
        self._store = _Store(max_entries)

    def lookup(self, reference: str) -> CacheEntry | None:
        entry = self._store.get(reference)
        if entry is None:
            return None
        # One-sided bound is deliberate: time.monotonic never goes backward
        # within a single process, and MemoryLayer entries are always stamped
        # by _monotonic() in store(). The two-sided bound (FIX-2) that
        # _FileCache uses guards against cross-process clock skew on
        # wall-clock-stamped disk entries -- that concern does not apply here.
        if (_monotonic() - entry.cached_at) > self._ttl:
            self._store.delete(reference)
            return None
        return entry

    def store(self, reference: str, value: object) -> None:
        self._store.put(reference, CacheEntry(key=reference, value=value, cached_at=_monotonic(), metadata={}))

    def clear(self) -> None:
        self._store.clear()

    def clear_misses(self) -> None:
        self._store.clear_misses()


# -- shared resolver logic (no locking, no I/O — used by both stacks) ---------


def _validate_layers(layers: Sequence[CacheLayer]) -> list[CacheLayer]:
    """Return ``layers`` as a list, raising ``TypeError`` for a non-layer element."""
    validated = list(layers)
    for layer in validated:
        # isinstance against a @runtime_checkable Protocol is a name-presence
        # check -- it verifies that `lookup` exists on the object, not that its
        # signature or return type matches. This catches the common mistake of
        # passing a Backend (which has no `lookup`) as a layer, but it would not
        # catch an object with a structurally-wrong `lookup` implementation.
        if not isinstance(layer, CacheLayer):
            raise TypeError(
                f"layers must satisfy CacheLayer; a source backend goes in source=, not the layer list: {layer!r}"
            )
    return validated


def _walk(layers: Sequence[CacheLayer], reference: str) -> tuple[int, CacheEntry] | None:
    """Return ``(index, live entry)`` of the first layer that hits, else ``None`` (design 3.2 rule 1)."""
    for index, layer in enumerate(layers):
        entry = layer.lookup(reference)
        if entry is not None:
            return index, entry
    return None


def _backfill(layers: Sequence[CacheLayer], reference: str, payload: object, boundary: int) -> None:
    """Warm writable layers strictly above ``boundary``, deepest-first (design 3.3).

    For a layer hit ``boundary`` is the hit index; for a source result it is
    ``len(layers)``, so every writable layer is warmed. Warming runs from the
    layer immediately above the boundary up to the top of the stack — the more
    durable (deeper) warm lands first if the process dies mid-warm. Read-only
    layers have no ``store`` and are skipped.
    """
    for index in range(boundary - 1, -1, -1):
        layer = layers[index]
        if isinstance(layer, WritableCacheLayer):
            layer.store(reference, payload)


def _unpack(value: object, reference: str, default_value: str | None) -> str:
    """Resolve a payload to a value, the caller's default, or the terminal miss (design 3.3).

    A negative-cache payload (``_NOT_FOUND``) returns ``default_value`` when the
    caller gave one, else raises :class:`OpNotFoundError`. The default is
    returned to the caller only — never stored.
    """
    if value is _NOT_FOUND:
        if default_value is not None:
            return default_value
        raise OpNotFoundError(reference) from None
    return cast("str", value)


def _fan_out_clear(layers: Sequence[CacheLayer], *, misses_only: bool) -> None:
    """Call ``clear_misses`` (or ``clear``) on every writable layer."""
    for layer in layers:
        if isinstance(layer, WritableCacheLayer):
            if misses_only:
                layer.clear_misses()
            else:
                layer.clear()


class ResolverStack:
    """Ordered cache layers over one source backend (design section 3).

    Structurally satisfies the :class:`~op_core.backends.base.Backend` protocol,
    so ``OnePassword(backend=stack)`` works with no facade change (design 5.2).
    :meth:`read` runs the layered walk; :meth:`get_item` / :meth:`list_items` /
    :meth:`list_vaults` route straight to the source — caching is not part of the
    ``Backend`` protocol (design 3.4).

    Locking is **resolver-owned**: a single :class:`threading.Lock` guards the
    layer lookups and the back-fill writes, and is released across the source
    call. Layers carry no locks, so a layer instance must not be shared between
    stacks. Two threads that miss simultaneously both reach the source — the
    accepted benign race (design 3.1 / 11): the cost is one redundant resolve,
    never corruption.
    """

    def __init__(self, layers: Sequence[CacheLayer], source: Backend) -> None:
        self._layers = _validate_layers(layers)
        # Runtime validation of a public-API argument: callers can pass a mistyped
        # source despite the annotation (the type checker reads the guard as dead).
        if not isinstance(source, Backend):
            raise TypeError(f"source must satisfy the Backend protocol (a cache layer is not a backend): {source!r}")
        self._source = source
        self._lock = threading.Lock()

    def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
        with self._lock:
            found = _walk(self._layers, reference)
            if found is not None:
                index, entry = found
                _backfill(self._layers, reference, entry.value, index)
                return _unpack(entry.value, reference, default_value)
            if not online:
                raise OpOfflineError(f"reference not cached: {reference}")
        # Lock released across the source call (design 3.1); the source is called
        # bare — neither default_value nor online is forwarded; the stack owns both.
        try:
            value = self._source.read(reference)
        except OpNotFoundError:
            with self._lock:
                _backfill(self._layers, reference, _NOT_FOUND, len(self._layers))
            return _unpack(_NOT_FOUND, reference, default_value)
        with self._lock:
            _backfill(self._layers, reference, value, len(self._layers))
        return value

    def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        return self._source.get_item(item, vault=vault)

    def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        return self._source.list_items(vault=vault, tags=tags, categories=categories)

    def list_vaults(self) -> list[VaultSummary]:
        return self._source.list_vaults()

    def clear(self) -> None:
        with self._lock:
            _fan_out_clear(self._layers, misses_only=False)

    def clear_misses(self) -> None:
        with self._lock:
            _fan_out_clear(self._layers, misses_only=True)


class AsyncResolverStack:
    """Async mirror of :class:`ResolverStack` (design section 3 / 5.1).

    Runs the identical walk, back-fill, and ``default_value`` / ``online``
    semantics through the shared module helpers, with an :class:`asyncio.Lock`
    and ``await`` only on the source. Layers are synchronous in both stacks; the
    file I/O a writer layer does while the lock is held is an accepted tradeoff —
    the cache file is small and RAM-backed, and the callers are short-lived.
    """

    def __init__(self, layers: Sequence[CacheLayer], source: AsyncBackend) -> None:
        self._layers = _validate_layers(layers)
        # Runtime validation of a public-API argument (see ResolverStack).
        if not isinstance(source, AsyncBackend):
            raise TypeError(
                f"source must satisfy the AsyncBackend protocol (a cache layer is not a backend): {source!r}"
            )
        self._source = source
        self._lock = asyncio.Lock()

    async def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
        async with self._lock:
            found = _walk(self._layers, reference)
            if found is not None:
                index, entry = found
                _backfill(self._layers, reference, entry.value, index)
                return _unpack(entry.value, reference, default_value)
            if not online:
                raise OpOfflineError(f"reference not cached: {reference}")
        # Lock released across the source await (design 3.1); source called bare.
        try:
            value = await self._source.read(reference)
        except OpNotFoundError:
            async with self._lock:
                _backfill(self._layers, reference, _NOT_FOUND, len(self._layers))
            return _unpack(_NOT_FOUND, reference, default_value)
        async with self._lock:
            _backfill(self._layers, reference, value, len(self._layers))
        return value

    async def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        return await self._source.get_item(item, vault=vault)

    async def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        return await self._source.list_items(vault=vault, tags=tags, categories=categories)

    async def list_vaults(self) -> list[VaultSummary]:
        return await self._source.list_vaults()

    async def clear(self) -> None:
        async with self._lock:
            _fan_out_clear(self._layers, misses_only=False)

    async def clear_misses(self) -> None:
        async with self._lock:
            _fan_out_clear(self._layers, misses_only=True)
