"""Canonical 1Password item dataclasses.

These are backend-agnostic, immutable representations of 1Password items.
Each :class:`Backend` implementation normalizes its native response into
:class:`Item` so consumers see one shape regardless of source (CLI, SDK,
in-memory fake).

``Item``, ``ItemField``, and ``ItemSection`` carry no behavior — they are
pure data. Field types and item categories are kept as raw strings to
remain forward-compatible with new 1Password values.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ItemSection:
    """A named grouping of fields within an item."""

    id: str
    label: str


@dataclass(frozen=True)
class ItemField:
    """A single field on a 1Password item."""

    id: str
    label: str
    value: str | None
    type: str  # raw 1Password field type: 'STRING', 'CONCEALED', 'OTP', etc.
    section_id: str | None  # None for top-level (un-sectioned) fields


@dataclass(frozen=True)
class ItemURL:
    """A URL entry on a 1Password item.

    URLs live alongside ``fields`` and ``sections`` in the item payload but
    are NOT addressable via ``op read`` — the CLI rejects
    ``op://vault/item/<url-label>`` with a "not a field" error. They are
    exposed here for read-only inspection, e.g. so a validator can
    distinguish a URL label from a missing field name on the same item.

    Defaults for ``label`` and ``primary`` mirror 1Password's UI convention:
    an URL with no user-set label is shown as ``"website"``, and an URL
    without an explicit primary marker is not the primary. Both backend
    parsers populate these defaults when the source payload omits or
    empties the corresponding field, so every parsed ``ItemURL`` carries a
    non-empty label and a boolean ``primary``.

    The ``primary`` flag is only populated meaningfully by the CLI backend.
    The official 1Password SDK's ``Website`` type has no equivalent, so
    URLs sourced via :class:`~op_core.backends.sdk.SDKBackend` always carry
    ``primary=False``.
    """

    href: str
    label: str = "website"
    primary: bool = False


@dataclass(frozen=True)
class Item:
    """A 1Password item in its canonical, backend-agnostic form."""

    id: str
    title: str
    vault_id: str
    vault_name: str
    category: str  # raw 1Password item category: 'LOGIN', 'SSH_KEY', etc.
    tags: tuple[str, ...]
    sections: tuple[ItemSection, ...]
    fields: tuple[ItemField, ...]
    urls: tuple[ItemURL, ...] = ()

    def field(self, label: str) -> ItemField | None:
        """Return the first field whose label matches ``label`` exactly, or ``None``.

        Matching is case-sensitive. Labels in 1Password are user-controlled
        strings; callers that want fuzzy matching should iterate ``fields``
        directly.
        """
        for f in self.fields:
            if f.label == label:
                return f
        return None

    def url(self, label: str) -> ItemURL | None:
        """Return the first URL whose label matches ``label`` exactly, or ``None``.

        Matching is case-sensitive. Labels in 1Password are user-controlled
        strings; callers that want fuzzy matching should iterate ``urls``
        directly.
        """
        for u in self.urls:
            if u.label == label:
                return u
        return None

    def primary_url(self) -> ItemURL | None:
        """Return the first URL marked ``primary=True``, or ``None``.

        Returns ``None`` when no URL is marked primary — does not guess by
        falling back to the first entry. SDK-sourced items always return
        ``None`` because the SDK does not expose the primary flag.
        """
        for u in self.urls:
            if u.primary:
                return u
        return None

    def fields_in(self, section: str | ItemSection) -> tuple[ItemField, ...]:
        """Return all fields belonging to ``section``.

        Accepts an :class:`ItemSection` instance (matched by its ``id``) or a
        string. Strings are resolved by checking section ids first, then
        section labels — so if an id happens to equal another section's label,
        the id wins. Pass an :class:`ItemSection` explicitly to disambiguate.
        Returns an empty tuple when the section is not found or has no fields.
        """
        if isinstance(section, ItemSection):
            section_id = section.id
        else:
            section_id = self._resolve_section_id(section)
            if section_id is None:
                return ()
        return tuple(f for f in self.fields if f.section_id == section_id)

    def top_level_fields(self) -> tuple[ItemField, ...]:
        """Return all fields that are not part of any section."""
        return tuple(f for f in self.fields if f.section_id is None)

    def _resolve_section_id(self, id_or_label: str) -> str | None:
        """Find a section by id first, then by label. Returns its id or ``None``."""
        for s in self.sections:
            if s.id == id_or_label:
                return s.id
        for s in self.sections:
            if s.label == id_or_label:
                return s.id
        return None


@dataclass(frozen=True)
class ItemSummary:
    """Lightweight view of an item, carrying only the metadata returned by list operations.

    Backends that enumerate items (e.g. ``op item list``) do not receive field data;
    :class:`ItemSummary` represents that partial view explicitly so callers cannot
    mistake it for a fully-fetched :class:`Item`. To obtain the full item, pass the
    summary to ``backend.get_item``.
    """

    id: str
    title: str
    vault_id: str
    vault_name: str
    category: str  # raw 1Password item category
    tags: tuple[str, ...]


@dataclass(frozen=True)
class VaultSummary:
    """Lightweight view of a vault, returned by ``list_vaults``.

    Carries only id and name — enough to scope a subsequent ``list_items``
    call to a single vault, which is dramatically faster on accounts with
    thousands of items spread across many vaults.
    """

    id: str
    name: str


ItemRef = str | ItemSummary | Item
"""Anything that identifies an item: a bare id, a summary, or a full item."""
