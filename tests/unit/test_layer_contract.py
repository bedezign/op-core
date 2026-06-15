"""Layer-contract protocol tests (design section 4).

``CacheLayer`` / ``WritableCacheLayer`` are the structural protocols the
resolver walks. These tests pin the *discriminating* shape: a read-only layer
exposes ``lookup`` only; a writable layer adds ``store`` / ``clear`` /
``clear_misses``; a ``Backend`` is not a layer and a layer is not a ``Backend``
(caching is not part of the ``Backend`` protocol — see ``backends/base.py``).

Conformance assertions for the concrete layers (``MemoryLayer``,
``FileReaderLayer``, ``FileWriterLayer``) are added in their own phases as those
classes come online.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from op_core.backends.base import Backend
from op_core.backends.memory import InMemoryBackend
from op_core.backends.stack import CacheLayer, WritableCacheLayer

if TYPE_CHECKING:
    from op_core.backends.caching import CacheEntry


class _ReadOnlyStub:
    """A lookup-only layer: satisfies ``CacheLayer``, not ``WritableCacheLayer``."""

    def lookup(self, reference: str) -> CacheEntry | None:
        return None


class _WritableStub:
    """A full writable layer: satisfies both protocols."""

    def lookup(self, reference: str) -> CacheEntry | None:
        return None

    def store(self, reference: str, value: object) -> None:
        pass

    def clear(self) -> None:
        pass

    def clear_misses(self) -> None:
        pass


class TestCacheLayerProtocol:
    def test_readonly_stub_is_cache_layer(self) -> None:
        assert isinstance(_ReadOnlyStub(), CacheLayer)

    def test_readonly_stub_is_not_writable(self) -> None:
        # lookup alone must not satisfy the writable protocol.
        assert not isinstance(_ReadOnlyStub(), WritableCacheLayer)

    def test_writable_stub_is_cache_layer(self) -> None:
        assert isinstance(_WritableStub(), CacheLayer)

    def test_writable_stub_is_writable(self) -> None:
        assert isinstance(_WritableStub(), WritableCacheLayer)


class TestLayerBackendDisjoint:
    def test_backend_is_not_a_cache_layer(self) -> None:
        # A real Backend has no lookup() — caching is not part of the protocol.
        assert not isinstance(InMemoryBackend(), CacheLayer)

    def test_layer_is_not_a_backend(self) -> None:
        # A layer has no read()/get_item()/list_*() — it is not a Backend.
        assert not isinstance(_WritableStub(), Backend)
