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
addressable via :meth:`read`. A top-level field (no section) is addressable
under ``op://<vault_id>/<item_id>/<label>`` **and**
``op://<vault_id>/<item_id>/<id>``. A field that belongs to a section is
addressable under ``op://<vault_id>/<item_id>/<section>/<label-or-id>``,
where ``<section>`` is either the section's label or its id and
``<label-or-id>`` is either the field's label or its id, **plus** the bare
``op://<vault_id>/<item_id>/<id>`` escape hatch, which always works
regardless of section membership. A bare label therefore unambiguously
names a top-level field. So ``InMemoryBackend(items=fetched_items,
fallback=CLIBackend())`` serves the fetched literal fields from memory and
only falls through on genuine misses. Explicit ``refs`` win over the
auto-built item index on collision.

A section or field label containing ``/`` cannot be used as a path
component — the key using that form is skipped, and the field remains
addressable through its other forms (in practice the bare ``id`` and/or the
section-id-qualified forms).

Every key built from a field's *label* (bare or section-qualified) and every
key built from a field's *id* share one namespace. Two distinct fields
claiming the same key of the *same* kind — both label-derived, or both
id-derived — is genuine ambiguity, and building the index raises
:class:`ValueError` naming the item and both field ids. A label-derived key
and an id-derived key from *different* fields landing on the same string is
not an error: the label-derived key wins, and the id-keyed field stays
reachable through its other forms (in practice its bare id). This matters in
practice because a 1Password item's built-in field ids are fixed and can
collide with a user-added field's label — e.g. a SERVER item's built-in URL
field (label ``URL``, id ``url``) alongside a user field labelled ``url``.

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
from op_core.items import Item, ItemField, ItemRef, ItemSection, ItemSummary, VaultSummary

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


def _vaults_from_items(items: Iterable[Item]) -> list[VaultSummary]:
    """Derive an ordered, deduped vault list from seeded items.

    First-seen wins on collisions: if two items disagree on ``vault_name``
    for the same ``vault_id``, the first item's name is kept and the
    second is silently ignored. Real items from the same vault always
    carry the same name; the rule guards the dedup contract rather than
    a real-world scenario.
    """
    seen: dict[str, VaultSummary] = {}
    for item in items:
        if item.vault_id not in seen:
            seen[item.vault_id] = VaultSummary(id=item.vault_id, name=item.vault_name)
    return list(seen.values())


def _field_index_keys(
    base: str, field: ItemField, sections_by_id: Mapping[str, ItemSection]
) -> tuple[set[str], set[str]]:
    """Return the ``(id_keys, label_keys)`` op:// keys ``field`` should be indexed under.

    See :func:`_build_item_index` for the addressing scheme and the
    label-wins collision rule this split enables. A key's kind is decided by
    which field form it uses, regardless of whether the section component
    (when present) is the section's label or its id: the bare id and every
    section-qualified key using ``field.id`` are id-derived; every key using
    ``field.label`` is label-derived. When a field's label equals its id, it
    contributes no separate label-derived keys — its keys are id-derived
    only. A path component (a label or an id) containing ``/`` is excluded
    from the key forms it would otherwise appear in.
    """
    id_keys: set[str] = set()
    label_keys: set[str] = set()
    has_own_label = field.label != field.id
    if "/" not in field.id:
        id_keys.add(f"{base}/{field.id}")
    if field.section_id is None:
        if has_own_label and "/" not in field.label:
            label_keys.add(f"{base}/{field.label}")
        return id_keys, label_keys
    section = sections_by_id.get(field.section_id)
    section_forms = {section.label, section.id} if section is not None else {field.section_id}
    for section_form in section_forms:
        if "/" in section_form:
            continue
        if "/" not in field.id:
            id_keys.add(f"{base}/{section_form}/{field.id}")
        if has_own_label and "/" not in field.label:
            label_keys.add(f"{base}/{section_form}/{field.label}")
    return id_keys, label_keys


def _build_item_index(items: Iterable[Item]) -> dict[str, str]:
    """Return a ``{op://vault/item/...: value}`` lookup for non-``None`` literal fields.

    A top-level field (``section_id is None``) is indexed under its bare
    label (when the label differs from the id) and under its bare id. A
    field that belongs to a section is indexed under every combination of
    section form (its label, its id) crossed with field form (its label,
    its id) — e.g. ``op://vault/item/{section_label}/{field_label}`` and
    ``op://vault/item/{section_id}/{field_id}`` — plus its bare id, which is
    always indexed regardless of section membership. A ``section_id`` with
    no matching entry in ``item.sections`` is used verbatim as the sole
    section form. Sectioned fields are never indexed under a bare label, so
    a bare label unambiguously resolves to a top-level field.

    Fields whose value starts with ``op://`` or ``ops://`` (an op-core
    reference, including a ``||`` chain that starts with a reference segment)
    are NOT indexed — they require backend resolution and must fall through to
    the configured ``fallback`` (or raise :class:`OpNotFoundError` if none is
    set). Indexing them as literals would return the reference *string*
    instead of the value it points at. Other ``://`` values (e.g.
    ``https://example.com``) are indexed as literals.

    A key built from one field's label and a key built from a *different*
    field's id share one namespace. Where the two coincide, the label-derived
    key wins and the id-keyed field remains reachable through its other
    forms (see :func:`_field_index_keys`). Raises :class:`ValueError` only
    for a *same-kind* collision — two distinct fields whose label-derived
    keys coincide, or two distinct fields whose id-derived keys coincide.
    """
    index: dict[str, str] = {}
    for item in items:
        base = f"op://{item.vault_id}/{item.id}"
        sections_by_id = {section.id: section for section in item.sections}
        id_owner_by_key: dict[str, str] = {}
        label_owner_by_key: dict[str, str] = {}
        id_values: dict[str, str] = {}
        label_values: dict[str, str] = {}
        for field in item.fields:
            if field.value is None:
                continue
            if field.value.startswith(_REFERENCE_PREFIXES):
                continue
            id_keys, label_keys = _field_index_keys(base, field, sections_by_id)
            for key in id_keys:
                owner = id_owner_by_key.get(key)
                if owner is not None and owner != field.id:
                    raise ValueError(
                        f"duplicate item-index key {key!r} on item {item.vault_id}/{item.id}: "
                        f"fields {owner!r} and {field.id!r} both resolve to it"
                    )
                id_owner_by_key[key] = field.id
                id_values[key] = field.value
            for key in label_keys:
                owner = label_owner_by_key.get(key)
                if owner is not None and owner != field.id:
                    raise ValueError(
                        f"duplicate item-index key {key!r} on item {item.vault_id}/{item.id}: "
                        f"fields {owner!r} and {field.id!r} both resolve to it"
                    )
                label_owner_by_key[key] = field.id
                label_values[key] = field.value
        index.update(id_values)
        index.update(label_values)
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

    def list_vaults(self) -> list[VaultSummary]:
        return _vaults_from_items(self._items)


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

    async def list_items(  # NOSONAR python:S7503 — async required for AsyncBackend protocol conformance
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

    async def get_item(
        self, item: ItemRef, *, vault: str | None = None
    ) -> Item:  # NOSONAR python:S7503 — async required for AsyncBackend protocol conformance
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

    async def list_vaults(
        self,
    ) -> list[VaultSummary]:  # NOSONAR python:S7503 — async required for AsyncBackend protocol conformance
        return _vaults_from_items(self._items)
