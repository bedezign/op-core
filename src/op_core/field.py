"""Field value model: classification, sensitivity, and reference resolution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import cast

from op_core.opref import OPS_PREFIX, OpRef

CHAIN_SEPARATOR = '||'
TEMPLATE_OPEN = '{{'
TEMPLATE_CLOSE = '}}'

SENSITIVE_FIELD_NAMES = frozenset({'password', 'passwd', 'pass', 'secret', 'token', 'otp'})

# Field names whose item-level refs (op://Vault/Item) auto-complete with a field path.
_AUTO_COMPLETE_FIELDS: dict[str, str] = {
    'password': 'password',
}

Reader = Callable[[str], str | None]
"""Callable that resolves a concrete ``op://`` reference to its value."""

AsyncReader = Callable[[str], Awaitable[str | None]]
"""Async callable that resolves a concrete ``op://`` reference to its value."""


def _is_reference(value: str) -> bool:
    """Return True if a value segment is a reference (contains ``://``)."""
    return '://' in value


def _has_template(value: str) -> bool:
    """Return True if a value contains ``{{...}}`` template syntax."""
    return TEMPLATE_OPEN in value and TEMPLATE_CLOSE in value


def classify_type(raw: str) -> str:
    """Classify a raw field value as ``'reference'``, ``'template'``, or ``'literal'``."""
    if _is_reference(raw):
        return 'reference'
    if _has_template(raw):
        return 'template'
    return 'literal'


def is_sensitive(raw: str, field_name: str) -> bool:
    """Determine if a field is sensitive.

    A field is sensitive when any of these are true:
    - The field name contains a known sensitive word (e.g. ``sudo_password``, ``api_token``).
    - Any segment in a ``||`` chain uses the ``ops://`` prefix.
    """
    name = field_name.lower()
    return any(s in name for s in SENSITIVE_FIELD_NAMES) or OPS_PREFIX in raw


def complete_field_refs(raw: str, field_name: str) -> str:
    """Ensure all reference segments in a raw value have a field path.

    Item-level refs (``op://Vault/Item``) cannot be resolved via ``op read``.
    For known fields (e.g. ``password``) the field path is auto-appended;
    for anything else a :class:`ValueError` is raised. Non-reference segments
    and already-complete references pass through unchanged.
    """
    segments = raw.split(CHAIN_SEPARATOR)
    result: list[str] = []
    for segment in segments:
        stripped = segment.strip()
        if not stripped or not _is_reference(stripped):
            result.append(segment)
            continue

        ref = OpRef.parse(stripped)
        if ref.is_complete:
            result.append(segment)
            continue

        auto_field = _AUTO_COMPLETE_FIELDS.get(field_name)
        if auto_field is None:
            raise ValueError(f'incomplete reference (missing field path): {stripped}')

        completed = ref.with_field(auto_field)
        result.append(segment.replace(stripped, completed.for_storage()))

    return CHAIN_SEPARATOR.join(result)


def normalize_original(raw: str, vault_id: str, item_id: str) -> str:
    """Expand self-references in a raw value for storage.

    Expands ``op://././field`` → ``op://vault_id/item_id/field`` and
    ``op://./Item/field`` → ``op://vault_id/Item/field`` in each segment
    of a ``||`` chain. Preserves the ``ops://`` prefix (sensitivity marker) —
    only the path is expanded.
    """
    segments = raw.split(CHAIN_SEPARATOR)
    normalized: list[str] = []
    for segment in segments:
        stripped = segment.strip()
        if not stripped or not _is_reference(stripped):
            normalized.append(segment)
            continue

        normalized.append(OpRef.parse(stripped).as_absolute(vault_id, item_id).for_storage())

    return CHAIN_SEPARATOR.join(normalized)


def resolve_chain(
    raw: str,
    reader: Reader,
    vault_id: str | None = None,
    item_id: str | None = None,
) -> str | None:
    """Resolve a ``||`` fallback chain against a reader callable.

    Splits on ``||`` and tries each segment left-to-right:
    - Reference segment → parse as :class:`OpRef`, normalize, resolve via ``reader``.
    - Literal segment → use as-is if non-empty.

    Returns the first non-empty result, or ``None`` if every segment fails.
    """
    for segment in raw.split(CHAIN_SEPARATOR):
        segment = segment.strip()
        if not segment:
            continue

        if _is_reference(segment):
            resolved = reader(OpRef.parse(segment).as_absolute(vault_id, item_id).for_op())
            if resolved:
                return resolved
        else:
            return segment

    return None


async def async_resolve_chain(
    raw: str,
    reader: AsyncReader,
    vault_id: str | None = None,
    item_id: str | None = None,
) -> str | None:
    """Async-aware :func:`resolve_chain` — walks the chain awaiting each reader call."""
    for segment in raw.split(CHAIN_SEPARATOR):
        segment = segment.strip()
        if not segment:
            continue

        if _is_reference(segment):
            resolved = await reader(OpRef.parse(segment).as_absolute(vault_id, item_id).for_op())
            if resolved:
                return resolved
        else:
            return segment

    return None


@dataclass(frozen=True)
class FieldValue:
    """Immutable 1Password field value with classification and optional resolved state.

    ``FieldValue`` is a pure data model. Resolution is performed by the
    :class:`OnePassword` facade (or any caller), not by the dataclass itself,
    so the model has no knowledge of backends or caches.
    """

    original: str
    resolved: str | None
    sensitive: bool
    field_type: str  # 'literal', 'reference', 'template'

    @classmethod
    def from_raw(cls, raw: str, field_name: str) -> FieldValue:
        """Create a :class:`FieldValue` from a raw 1Password field value."""
        return cls(
            original=raw,
            resolved=None,
            field_type=classify_type(raw),
            sensitive=is_sensitive(raw, field_name),
        )

    def with_resolved(self, resolved: str | None) -> FieldValue:
        """Return a copy with ``resolved`` replaced."""
        return cast(FieldValue, replace(self, resolved=resolved))

    def to_dict(self) -> dict[str, str | bool | None]:
        """Serialize to a JSON-friendly dict.

        Stores only ``original``, ``resolved``, and ``sensitive``. ``field_type``
        is derived from ``original`` via :func:`classify_type` on :meth:`from_dict`
        so the on-disk format stays minimal and forward-compatible.
        """
        return {
            'original': self.original,
            'resolved': self.resolved,
            'sensitive': self.sensitive,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, str | bool | None]) -> FieldValue:
        """Reconstruct a :class:`FieldValue` from a :meth:`to_dict` payload."""
        if 'original' not in data:
            raise ValueError("'original' is required")
        original = data['original']
        resolved = data.get('resolved')
        sensitive = data.get('sensitive', False)
        if not isinstance(original, str):
            raise ValueError("'original' must be a string")
        if resolved is not None and not isinstance(resolved, str):
            raise ValueError("'resolved' must be a string or None")
        if not isinstance(sensitive, bool):
            raise ValueError("'sensitive' must be a bool")
        return cls(
            original=original,
            resolved=resolved,
            sensitive=sensitive,
            field_type=classify_type(original),
        )
