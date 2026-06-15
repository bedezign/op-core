"""Tests for the ``online=`` kwarg across all backends.

Verifies the safety-rail contract: when a caller sets ``online=False``, a
backend must satisfy the request from local state alone. Raw backends
(CLI/SDK) can never satisfy offline reads. A ``ResolverStack`` serves live
cached entries from its layers or raises. ``InMemoryBackend`` honors the flag
locally and propagates it to any ``fallback`` backend.
"""

from __future__ import annotations

import pytest

from op_core.auth import DesktopAuth, ServiceAccountAuth
from op_core.backends.cli import AsyncCLIBackend, CLIBackend
from op_core.backends.memory import InMemoryBackend
from op_core.backends.sdk import AsyncSDKBackend, SDKBackend
from op_core.backends.stack import AsyncResolverStack, MemoryLayer, ResolverStack
from op_core.exceptions import OpNotFoundError, OpOfflineError

# ---------- CLI / SDK raw backends ----------


class TestCLIOffline:
    def test_read_offline_raises_before_subprocess(self):
        backend = CLIBackend(auth=DesktopAuth(), binary="/usr/bin/true")
        with pytest.raises(OpOfflineError, match="CLIBackend"):
            backend.read("op://v/i/f", online=False)

    async def test_async_read_offline_raises_before_subprocess(self):
        backend = AsyncCLIBackend(auth=DesktopAuth(), binary="/usr/bin/true")
        with pytest.raises(OpOfflineError, match="AsyncCLIBackend"):
            await backend.read("op://v/i/f", online=False)


class TestSDKOffline:
    def test_sync_offline_raises(self):
        backend = SDKBackend(ServiceAccountAuth(token="stub"))
        with pytest.raises(OpOfflineError, match="SDKBackend"):
            backend.read("op://v/i/f", online=False)

    async def test_async_offline_raises(self):
        backend = AsyncSDKBackend(ServiceAccountAuth(token="stub"))
        with pytest.raises(OpOfflineError, match="AsyncSDKBackend"):
            await backend.read("op://v/i/f", online=False)


# ---------- ResolverStack (one MemoryLayer over a source) ----------


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

    def list_vaults(self):
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

    async def list_vaults(self):
        return []

    async def get_item(self, item, *, vault=None):
        raise OpNotFoundError(item)


class TestResolverStackOffline:
    def test_offline_hit_returns_cached(self):
        source = _Stub(refs={"op://v/i/f": "val"})
        stack = ResolverStack([MemoryLayer()], source)
        # Populate (online): resolves through source and back-fills the memory layer.
        assert stack.read("op://v/i/f") == "val"
        # Subsequent offline read is served from the layer without touching the source.
        assert stack.read("op://v/i/f", online=False) == "val"
        assert source.read_calls == 1

    def test_offline_miss_raises_offline_error(self):
        source = _Stub()
        stack = ResolverStack([MemoryLayer()], source)
        with pytest.raises(OpOfflineError):
            stack.read("op://v/i/missing", online=False)
        assert source.read_calls == 0

    def test_offline_never_delegates_to_source(self):
        source = _Stub(refs={"op://v/i/f": "val"})
        stack = ResolverStack([MemoryLayer()], source)
        # Cache empty; source has it; offline must NOT delegate.
        with pytest.raises(OpOfflineError):
            stack.read("op://v/i/f", online=False)
        assert source.read_calls == 0

    def test_offline_cached_not_found_raises_not_found(self):
        # A stored miss is authoritative even offline: OpNotFoundError, not OpOfflineError.
        source = _Stub()
        stack = ResolverStack([MemoryLayer()], source)
        with pytest.raises(OpNotFoundError):
            stack.read("op://v/i/missing")  # populate the negative entry
        with pytest.raises(OpNotFoundError):
            stack.read("op://v/i/missing", online=False)
        assert source.read_calls == 1


class TestAsyncResolverStackOffline:
    async def test_offline_hit(self):
        source = _AsyncStub(refs={"op://v/i/f": "val"})
        stack = AsyncResolverStack([MemoryLayer()], source)
        assert await stack.read("op://v/i/f") == "val"
        assert await stack.read("op://v/i/f", online=False) == "val"
        assert source.read_calls == 1

    async def test_offline_miss_raises(self):
        source = _AsyncStub()
        stack = AsyncResolverStack([MemoryLayer()], source)
        with pytest.raises(OpOfflineError):
            await stack.read("op://v/i/missing", online=False)
        assert source.read_calls == 0


# ---------- Composition: InMemory + CLI fallback (the wrap-phase pattern) ----------


class TestWrapPhasePattern:
    def test_hostdata_pattern_uses_local_first(self):
        """Known values come from hostdata; unknown references fail offline."""
        # Only a CLI fallback (which always raises offline)
        cli = CLIBackend(auth=DesktopAuth(), binary="/usr/bin/true")
        backend = InMemoryBackend(
            refs={"op://v/i/known": "pre_resolved"},
            fallback=cli,
        )
        # Known ref: local hit, no subprocess
        assert backend.read("op://v/i/known", online=False) == "pre_resolved"
        # Unknown ref: falls through to CLI which raises offline
        with pytest.raises(OpOfflineError):
            backend.read("op://v/i/unknown", online=False)

    def test_online_true_allows_fallback_fetch(self):
        """With online=True, fallback can fetch unknowns. Using _Stub as stand-in."""
        stub = _Stub(refs={"op://v/i/unknown": "from_fallback"})
        backend = InMemoryBackend(
            refs={"op://v/i/known": "local"},
            fallback=stub,  # type: ignore[arg-type]
        )
        assert backend.read("op://v/i/known") == "local"
        assert backend.read("op://v/i/unknown") == "from_fallback"
