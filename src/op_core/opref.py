"""1Password ``op://`` URI parser with support for quoted and URL-encoded item names.

Reference format:
  Field refs (3 segments):  ``op://Vault/Item/field``, ``op://./Item/field``, ``op://././field``
  Item refs (2 segments):   ``op://Vault/Item``, ``op://./Item``
  Sensitive:                ``ops://...`` — same as ``op://`` but marks the field as sensitive.

The ``.`` self-marker means "current" at any position, making the reference
relative to ambient context:
  - Vault position: vault-relative ref, e.g. ``op://./Item/field``
  - Item position: item-relative ref — only valid when the vault is also ``.``,
    producing a full self-reference such as ``op://././field``.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, replace

OP_PREFIX = 'op://'
OPS_PREFIX = 'ops://'
SELF_MARKER = '.'


def _encode_part(value: str) -> str:
    """Encode slashes in a vault/item name for the op CLI (/ → %2F)."""
    return value.replace('/', '%2F')


def _split_uri_path(path: str) -> list[str]:
    """Split an op:// path into parts, respecting double-quoted segments.

    Handles:
    - Regular: Vault/Item/field → ['Vault', 'Item', 'field']
    - Quoted: Vault/"Item / Name"/field → ['Vault', 'Item / Name', 'field']
    - URL-encoded: Vault/Item %2F Name/field → ['Vault', 'Item / Name', 'field']

    Splitting happens on unquoted, literal '/' characters. After splitting,
    each part is URL-decoded (%2F → /) and surrounding double quotes are stripped.
    """
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False

    for ch in path:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == '/' and not in_quotes:
            parts.append(urllib.parse.unquote(''.join(current)))
            current = []
        else:
            current.append(ch)

    if current:
        parts.append(urllib.parse.unquote(''.join(current)))

    return parts


@dataclass(frozen=True)
class OpRef:
    """Parsed 1Password op:// reference.

    Provides structured access to vault, item, and field path components,
    with methods to emit the URI in different formats (for CLI, for storage).
    """

    vault: str
    item: str
    field_path: str | None
    sensitive: bool

    @classmethod
    def parse(cls, uri: str) -> OpRef:
        """Parse an op:// URI into components.

        Supports:
        - op://Vault/Item/field          (fully explicit field ref)
        - op://./Item/field              (same-vault cross-item field ref)
        - op://././field                 (self-ref field ref)
        - op://Vault/Item                (item-level ref, e.g. key refs)
        - op://./Item                    (same-vault item ref)
        - op://Vault/"Item / Slash"/f    (quoted item names)
        - op://Vault/Item %2F Name/f    (URL-encoded)
        - ops://...                      (sensitive marker)

        Rejected:
        - op://Vault/./field  (. in item requires . in vault)
        """
        sensitive = uri.startswith(OPS_PREFIX)

        if sensitive:
            path = uri[len(OPS_PREFIX):]
        elif uri.startswith(OP_PREFIX):
            path = uri[len(OP_PREFIX):]
        else:
            path = uri

        parts = _split_uri_path(path)

        if not parts or not parts[0]:
            raise ValueError(f'Invalid reference: {uri}')

        if len(parts) < 2 or not parts[1]:
            raise ValueError(f'Invalid reference (need vault/item): {uri}')

        vault = parts[0]
        item = parts[1]
        field_path = '/'.join(parts[2:]) if len(parts) > 2 and parts[2] else None

        # Validate: self-marker in item position requires self-marker in vault position
        if item == SELF_MARKER and vault != SELF_MARKER:
            raise ValueError(
                f'Invalid reference: "{SELF_MARKER}" in item position requires '
                f'"{SELF_MARKER}" in vault position '
                f'(use {OP_PREFIX}{SELF_MARKER}/{SELF_MARKER}/{SELF_MARKER}/field '
                f'for self-refs): {uri}'
            )

        return cls(vault=vault, item=item, field_path=field_path, sensitive=sensitive)

    @property
    def is_vault_relative(self) -> bool:
        """Whether the vault is the self-marker (``op://./...``), i.e. resolved against ambient context."""
        return self.vault == SELF_MARKER

    @property
    def is_item_relative(self) -> bool:
        """Whether the item is the self-marker (``op://..././...``), i.e. resolved against ambient context."""
        return self.item == SELF_MARKER

    @property
    def is_self_ref(self) -> bool:
        """Whether this is a full self-reference (``op://././...``) — both vault and item relative."""
        return self.is_vault_relative and self.is_item_relative

    @property
    def is_complete(self) -> bool:
        """Whether this reference includes a field path (not just vault/item)."""
        return self.field_path is not None

    def with_field(self, field_path: str) -> OpRef:
        """Return a new :class:`OpRef` with the given field path."""
        return replace(self, field_path=field_path)

    def as_absolute(self, vault_id: str | None = None, item_id: str | None = None) -> OpRef:
        """Return an absolute copy of this reference.

        - Already-absolute refs pass through unchanged (returns ``self``).
        - Vault-relative refs (``op://./Item/field``) resolve against ``vault_id``.
        - Self-refs (``op://././field``) resolve against both ``vault_id`` and ``item_id``.

        Raises:
            ValueError: when the reference is vault-relative and ``vault_id``
                is ``None``, or item-relative and ``item_id`` is ``None``.
        """
        if not self.is_vault_relative:
            return self
        if vault_id is None:
            raise ValueError(f'vault_id required to resolve {self.for_storage()}')
        if self.is_item_relative:
            if item_id is None:
                raise ValueError(f'item_id required to resolve {self.for_storage()}')
            return replace(self, vault=vault_id, item=item_id)
        return replace(self, vault=vault_id)

    def for_op(self) -> str:
        """Emit URI for the op CLI: always op:// prefix, %2F-encoded names."""
        return self._to_uri(OP_PREFIX)

    def for_storage(self) -> str:
        """Emit URI for storage: preserves ops:// sensitivity marker, %2F-encoded."""
        return self._to_uri(OPS_PREFIX if self.sensitive else OP_PREFIX)

    def _to_uri(self, prefix: str) -> str:
        vault = _encode_part(self.vault)
        item = _encode_part(self.item)

        if self.field_path is not None:
            return f'{prefix}{vault}/{item}/{self.field_path}'
        return f'{prefix}{vault}/{item}'
