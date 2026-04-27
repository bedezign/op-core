"""Backend and AsyncBackend protocols.

These describe the read-only surface every op-core backend must expose.
CRUD (`create_item`, `edit_item`, `delete_item`) is intentionally deferred
until the `ItemSpec` / `ItemPatch` shapes land.

Caching is *not* part of the protocol — it is layered on via the
`CachingBackend` decorator.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from op_core.items import Item, ItemRef, ItemSummary, VaultSummary


@runtime_checkable
class Backend(Protocol):
    def read(
        self,
        reference: str,
        *,
        default_value: str | None = None,
        online: bool = True,
    ) -> str: ...

    def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]: ...

    def list_vaults(self) -> list[VaultSummary]: ...

    def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item: ...


@runtime_checkable
class AsyncBackend(Protocol):
    async def read(
        self,
        reference: str,
        *,
        default_value: str | None = None,
        online: bool = True,
    ) -> str: ...

    async def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]: ...

    async def list_vaults(self) -> list[VaultSummary]: ...

    async def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item: ...
