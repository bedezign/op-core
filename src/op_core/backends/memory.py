"""In-memory backend with optional fall-through to another backend.

:class:`InMemoryBackend` and :class:`AsyncInMemoryBackend` satisfy the backend
protocols from a local ``refs`` dict and ``items`` list. They are useful for
two distinct purposes:

* **Tests** — downstream consumers test code that depends on op-core without
  provisioning a real 1Password account.
* **Persistent local caches** — a generate/wrap-style workflow can persist
  pre-resolved reference values to disk, reload them into an
  :class:`InMemoryBackend` at runtime, and set ``fallback`` to a live backend
  (e.g. :class:`CLIBackend`) so any reference the local store doesn't know
  about is fetched on demand.

Passing ``items=`` both powers :meth:`list_items`/:meth:`get_item` and makes
every non-``None``, non-reference :class:`~op_core.items.ItemField` value
addressable via :meth:`read` under ``op://<vault_id>/<item_id>/<label>`` **and**
``op://<vault_id>/<item_id>/<id>``. So
``InMemoryBackend(items=fetched_items, fallback=CLIBackend())`` serves the
fetched literal fields from memory and only falls through on genuine misses.
Explicit ``refs`` win over the auto-built item index on collision.

Values that start with ``op://`` or ``ops://`` (op-core references,
including ``||`` chains that start with a reference segment) are NOT
indexed — they require backend resolution and fall through to the
configured ``fallback`` (or raise :class:`OpNotFoundError` if no fallback
is set). Indexing them as literals would return the reference string
instead of the value it points at. Other ``://`` values such as
``https://example.com`` are indexed as ordinary literals.

The async variant wraps the sync one — there is no I/O to await, so
duplicating logic would be pure ceremony.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING

from op_core.backends._filters import validate_filter
from op_core.exceptions import OpNotFoundError, OpOfflineError
from op_core.items import Item, ItemRef, ItemSummary

# Prefix-based check (not substring '://' in value) so legitimate URL field
# values like "https://example.com" still get indexed as literals.
_REFERENCE_PREFIXES = ("op://", "ops://")

if TYPE_CHECKING:
    from op_core.backends.base import AsyncBackend, Backend


def _to_summary(item: Item) -> ItemSummary:
    return ItemSummary(
        id=item.id,
        title=item.title,
        vault_id=item.vault_id,
        vault_name=item.vault_name,
        category=item.category,
        tags=item.tags,
    )


def _build_item_index(items: Iterable[Item]) -> dict[str, str]:
    """Return a ``{op://vault/item/label: value}`` lookup for non-``None`` literal fields.

    Indexes each field under both its label and its id (unless they are identical).

    Fields whose value starts with ``op://`` or ``ops://`` (an op-core
    reference, including a ``||`` chain that starts with a reference segment)
    are NOT indexed — they require backend resolution and must fall through to
    the configured ``fallback`` (or raise :class:`OpNotFoundError` if none is
    set). Indexing them as literals would return the reference *string*
    instead of the value it points at. Other ``://`` values (e.g.
    ``https://example.com``) are indexed as literals.

    When two literal-valued fields on the same item share a label,
    last-in-iteration-order wins.
    """
    index: dict[str, str] = {}
    for item in items:
        base = f"op://{item.vault_id}/{item.id}"
        for field in item.fields:
            if field.value is None:
                continue
            if field.value.startswith(_REFERENCE_PREFIXES):
                continue
            index[f"{base}/{field.label}"] = field.value
            if field.id != field.label:
                index[f"{base}/{field.id}"] = field.value
    return index


class InMemoryBackend:
    """Backend backed by an in-process dict of refs and list of items.

    On ``read`` miss, delegates to ``fallback`` if set; otherwise raises
    :class:`OpNotFoundError`. The ``online`` kwarg propagates through to the
    fallback so a chain of backends can uniformly honor an offline request.
    """

    def __init__(
        self,
        *,
        refs: Mapping[str, str] | None = None,
        items: Iterable[Item] | None = None,
        fallback: Backend | None = None,
    ) -> None:
        self._refs = dict(refs or {})
        self._items = list(items or ())
        self._item_index = _build_item_index(self._items)
        self._fallback = fallback

    def read(
        self,
        reference: str,
        *,
        default_value: str | None = None,
        online: bool = True,
    ) -> str:
        # default_value semantics: applied only on "confirmed missing"
        # (OpNotFoundError), never on OpOfflineError — an offline condition
        # means we could not check, which is a different failure mode.
        if reference in self._refs:
            return self._refs[reference]
        if reference in self._item_index:
            return self._item_index[reference]
        if self._fallback is not None:
            try:
                return self._fallback.read(reference, online=online)
            except OpNotFoundError:
                if default_value is not None:
                    return default_value
                raise
        if default_value is not None:
            return default_value
        if not online:
            raise OpOfflineError(f"reference not available offline: {reference}")
        raise OpNotFoundError(f"reference not found: {reference}")

    def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        validate_filter("tags", tags)
        validate_filter("categories", categories)
        tag_set = set(tags) if tags is not None else None
        category_set = set(categories) if categories is not None else None
        result: list[ItemSummary] = []
        for item in self._items:
            if vault is not None and vault not in (item.vault_id, item.vault_name):
                continue
            if tag_set is not None and not tag_set.intersection(item.tags):
                continue
            if category_set is not None and item.category not in category_set:
                continue
            result.append(_to_summary(item))
        return result

    def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
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


class AsyncInMemoryBackend:
    """Async mirror of :class:`InMemoryBackend`."""

    def __init__(
        self,
        *,
        refs: Mapping[str, str] | None = None,
        items: Iterable[Item] | None = None,
        fallback: AsyncBackend | None = None,
    ) -> None:
        self._refs = dict(refs or {})
        self._items = list(items or ())
        self._item_index = _build_item_index(self._items)
        self._fallback = fallback

    async def read(
        self,
        reference: str,
        *,
        default_value: str | None = None,
        online: bool = True,
    ) -> str:
        # default_value applies to OpNotFoundError only — see sync variant.
        if reference in self._refs:
            return self._refs[reference]
        if reference in self._item_index:
            return self._item_index[reference]
        if self._fallback is not None:
            try:
                return await self._fallback.read(reference, online=online)
            except OpNotFoundError:
                if default_value is not None:
                    return default_value
                raise
        if default_value is not None:
            return default_value
        if not online:
            raise OpOfflineError(f"reference not available offline: {reference}")
        raise OpNotFoundError(f"reference not found: {reference}")

    async def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        validate_filter("tags", tags)
        validate_filter("categories", categories)
        tag_set = set(tags) if tags is not None else None
        category_set = set(categories) if categories is not None else None
        result: list[ItemSummary] = []
        for item in self._items:
            if vault is not None and vault not in (item.vault_id, item.vault_name):
                continue
            if tag_set is not None and not tag_set.intersection(item.tags):
                continue
            if category_set is not None and item.category not in category_set:
                continue
            result.append(_to_summary(item))
        return result

    async def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
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
