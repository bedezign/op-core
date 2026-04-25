"""Shared validation helpers for backend filter arguments.

The hard-fail rules live here so every backend rejects malformed filter
inputs identically — empty sequences (use ``None`` for "no filter") and
entries containing commas (which the underlying ``op`` CLI would
silently split into multiple values).
"""

from __future__ import annotations

from collections.abc import Sequence


def validate_filter(name: str, values: Sequence[str] | None) -> None:
    if values is None:
        return
    if len(values) == 0:
        raise ValueError(f"{name} must be None or a non-empty sequence")
    for v in values:
        if "," in v:
            raise ValueError(f"{name} entries cannot contain commas; got {v!r}")
