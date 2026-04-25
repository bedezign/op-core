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


ItemRef = str | ItemSummary | Item
"""Anything that identifies an item: a bare id, a summary, or a full item."""
