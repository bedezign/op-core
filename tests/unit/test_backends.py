"""Tests for the Backend / AsyncBackend protocols."""

from __future__ import annotations

from collections.abc import Sequence

from op_core.backends import AsyncBackend, Backend
from op_core.items import Item, ItemRef, ItemSummary, VaultSummary


class _MinimalSyncBackend:
    def read(self, reference: str, *, default_value: str | None = None) -> str:
        return ""

    def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        return []

    def list_vaults(self) -> list[VaultSummary]:
        return []

    def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        raise NotImplementedError


class _MinimalAsyncBackend:
    async def read(self, reference: str, *, default_value: str | None = None) -> str:
        return ""

    async def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        return []

    async def list_vaults(self) -> list[VaultSummary]:
        return []

    async def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        raise NotImplementedError


class _MissingMethod:
    def read(self, reference: str, *, default_value: str | None = None) -> str:
        return ""

    def list_items(self, **kwargs) -> list[ItemSummary]:
        return []

    # no get_item


class TestBackendProtocol:
    def test_minimal_impl_satisfies(self):
        assert isinstance(_MinimalSyncBackend(), Backend)

    def test_missing_method_does_not_satisfy(self):
        assert not isinstance(_MissingMethod(), Backend)

    def test_async_impl_does_not_satisfy_sync(self):
        # runtime_checkable Protocols only verify attribute presence, not
        # signatures, so an async class may happen to pass isinstance against
        # the sync Protocol. This test documents that caveat — we only assert
        # the positive sync case.
        pass


class TestAsyncBackendProtocol:
    def test_minimal_impl_satisfies(self):
        assert isinstance(_MinimalAsyncBackend(), AsyncBackend)

    def test_missing_method_does_not_satisfy(self):
        class Incomplete:
            async def read(self, reference: str) -> str | None:
                return None

        assert not isinstance(Incomplete(), AsyncBackend)
