"""Shared test helpers for the cache suites.

Not a test module (no ``test_`` prefix), so pytest does not collect it. Holds
the call-counting backend doubles and the scrambled-file decoder reused across
the file-cache, stack, and CLI suites.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from op_core.backends import file_caching
from op_core.exceptions import OpNotFoundError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from op_core.items import Item, ItemRef, ItemSummary, VaultSummary


class StubBackend:
    """A ``Backend`` that counts calls so tests can prove the cache prevented passthrough."""

    def __init__(self, *, refs: dict[str, str] | None = None, items: list[Item] | None = None) -> None:
        self._refs = refs or {}
        self._items = items or []
        self.read_count = 0
        self.list_items_count = 0
        self.list_vaults_count = 0
        self.get_item_count = 0

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
        return []

    def list_vaults(self) -> list[VaultSummary]:
        self.list_vaults_count += 1
        return []

    def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        self.get_item_count += 1
        raise OpNotFoundError("no item")


class AsyncStubBackend:
    """Async mirror of :class:`StubBackend` (delegates to a wrapped sync stub)."""

    def __init__(self, *, refs: dict[str, str] | None = None) -> None:
        self._sync = StubBackend(refs=refs)

    @property
    def read_count(self) -> int:
        return self._sync.read_count

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

    async def list_vaults(self) -> list[VaultSummary]:
        return self._sync.list_vaults()

    async def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        return self._sync.get_item(item, vault=vault)


def read_sets(path: Path) -> dict[str, dict]:
    """Decode the scrambled cache file and return its raw ``sets`` mapping."""
    payload = file_caching._decode_payload(path.read_bytes())
    assert payload["version"] == 1
    return payload["sets"]
