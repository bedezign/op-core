"""Tests for the ``online=`` kwarg across all backends.

Verifies the safety-rail contract: when a caller sets ``online=False``, a
backend must satisfy the request from local state alone. Raw backends
(CLI/SDK) can never satisfy offline reads. CachingBackend returns live
cached entries or raises. InMemoryBackend honors the flag locally and
propagates it to any ``fallback`` backend.
"""

from __future__ import annotations

import pytest

from op_core.auth import DesktopAuth, ServiceAccountAuth
from op_core.backends.caching import AsyncCachingBackend, CachingBackend
from op_core.backends.cli import AsyncCLIBackend, CLIBackend
from op_core.backends.memory import InMemoryBackend
from op_core.backends.sdk import AsyncSDKBackend, SDKBackend
from op_core.exceptions import OpNotFoundError, OpOfflineError

# ---------- CLI / SDK raw backends ----------


class TestCLIOffline:
    def test_read_offline_raises_before_subprocess(self):
        backend = CLIBackend(auth=DesktopAuth(), binary='/usr/bin/true')
        with pytest.raises(OpOfflineError, match='CLIBackend'):
            backend.read('op://v/i/f', online=False)

    async def test_async_read_offline_raises_before_subprocess(self):
        backend = AsyncCLIBackend(auth=DesktopAuth(), binary='/usr/bin/true')
        with pytest.raises(OpOfflineError, match='AsyncCLIBackend'):
            await backend.read('op://v/i/f', online=False)


class TestSDKOffline:
    def test_sync_offline_raises(self):
        backend = SDKBackend(ServiceAccountAuth(token='stub'))
        with pytest.raises(OpOfflineError, match='SDKBackend'):
            backend.read('op://v/i/f', online=False)

    async def test_async_offline_raises(self):
        backend = AsyncSDKBackend(ServiceAccountAuth(token='stub'))
        with pytest.raises(OpOfflineError, match='AsyncSDKBackend'):
            await backend.read('op://v/i/f', online=False)


# ---------- CachingBackend ----------


class _Stub:
    def __init__(self, refs: dict[str, str] | None = None) -> None:
        self._refs = refs or {}
        self.read_calls = 0

    def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
        self.read_calls += 1
        if reference in self._refs:
            return self._refs[reference]
        raise OpNotFoundError(reference)

    def list_items(self, **kwargs):
        return []

    def get_item(self, item, *, vault=None):
        raise OpNotFoundError(item)


class _AsyncStub:
    def __init__(self, refs=None):
        self._refs = refs or {}
        self.read_calls = 0

    async def read(self, reference, *, default_value=None, online=True):
        self.read_calls += 1
        if reference in self._refs:
            return self._refs[reference]
        raise OpNotFoundError(reference)

    async def list_items(self, **kwargs):
        return []

    async def get_item(self, item, *, vault=None):
        raise OpNotFoundError(item)


class TestCachingOffline:
    def test_offline_hit_returns_cached(self):
        inner = _Stub(refs={'op://v/i/f': 'val'})
        cache = CachingBackend(inner)
        # Populate the cache
        assert cache.read('op://v/i/f') == 'val'
        # Subsequent offline read should hit cache without touching inner
        assert cache.read('op://v/i/f', online=False) == 'val'
        assert inner.read_calls == 1  # only the initial populate

    def test_offline_miss_raises_offline_error(self):
        inner = _Stub()
        cache = CachingBackend(inner)
        with pytest.raises(OpOfflineError):
            cache.read('op://v/i/missing', online=False)
        # Inner was never touched
        assert inner.read_calls == 0

    def test_offline_never_delegates_to_inner(self):
        inner = _Stub(refs={'op://v/i/f': 'val'})
        cache = CachingBackend(inner)
        # Cache is empty; inner has it; offline must NOT delegate
        with pytest.raises(OpOfflineError):
            cache.read('op://v/i/f', online=False)
        assert inner.read_calls == 0

    def test_offline_cached_not_found_raises_not_found(self):
        """Confirmed-absent (cached _NOT_FOUND) raises OpNotFoundError,
        not OpOfflineError — the cache is authoritative for this key.
        """
        inner = _Stub()  # no refs → raises OpNotFoundError on any read
        cache = CachingBackend(inner)
        # Populate negative entry
        with pytest.raises(OpNotFoundError):
            cache.read('op://v/i/missing')
        # Subsequent offline read hits the _NOT_FOUND sentinel
        with pytest.raises(OpNotFoundError):
            cache.read('op://v/i/missing', online=False)
        assert inner.read_calls == 1  # only the first populate


class TestAsyncCachingOffline:
    async def test_offline_hit(self):
        inner = _AsyncStub(refs={'op://v/i/f': 'val'})
        cache = AsyncCachingBackend(inner)
        assert await cache.read('op://v/i/f') == 'val'
        assert await cache.read('op://v/i/f', online=False) == 'val'
        assert inner.read_calls == 1

    async def test_offline_miss_raises(self):
        inner = _AsyncStub()
        cache = AsyncCachingBackend(inner)
        with pytest.raises(OpOfflineError):
            await cache.read('op://v/i/missing', online=False)
        assert inner.read_calls == 0


# ---------- Composition: InMemory + CLI fallback (the wrap-phase pattern) ----------


class TestWrapPhasePattern:
    def test_hostdata_pattern_uses_local_first(self):
        """Known values come from hostdata; unknown references fail offline."""
        # Only a CLI fallback (which always raises offline)
        cli = CLIBackend(auth=DesktopAuth(), binary='/usr/bin/true')
        backend = InMemoryBackend(
            refs={'op://v/i/known': 'pre_resolved'},
            fallback=cli,
        )
        # Known ref: local hit, no subprocess
        assert backend.read('op://v/i/known', online=False) == 'pre_resolved'
        # Unknown ref: falls through to CLI which raises offline
        with pytest.raises(OpOfflineError):
            backend.read('op://v/i/unknown', online=False)

    def test_online_true_allows_fallback_fetch(self):
        """With online=True, fallback can fetch unknowns. Using _Stub as stand-in."""
        stub = _Stub(refs={'op://v/i/unknown': 'from_fallback'})
        backend = InMemoryBackend(
            refs={'op://v/i/known': 'local'},
            fallback=stub,  # type: ignore[arg-type]
        )
        assert backend.read('op://v/i/known') == 'local'
        assert backend.read('op://v/i/unknown') == 'from_fallback'
