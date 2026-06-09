"""Environment composition, ``op://`` resolution, and cache-bucket hashing.

This is the shared core both ``op-env exec`` and ``op-env export`` run before
they diverge on output. The steps:

1. **Compose** — start from the process environment and layer ``.env`` files on
   top with a precedence rule (process env wins by default; ``--override``
   flips it so later files win).
2. **Bucket** — derive a reproducible cache-file name from the *set* of
   ``op://`` references that survive into the composed environment, so repeated
   invocations resolving the same secrets share one cache file (and one
   authentication) while unrelated invocations stay isolated.
3. **Resolve** — replace each ``op://`` value with its resolved secret via the
   op-core facade; plain values pass through untouched.

The cache backend itself (and whether caching is on at all) is wired by the CLI
layer — this module stays backend-agnostic so it can be tested against an
``InMemoryBackend``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence

from op_core.cli.errors import MissingKeysError, ResolutionError
from op_core.client import OnePassword
from op_core.field import CHAIN_SEPARATOR, FieldValue

_REFERENCE_PREFIXES = ("op://", "ops://")
# Namespaces the hash input so a future bucket-format change cannot collide
# with cache files written by an older version.
_BUCKET_NAMESPACE = "op-core-env-cache-v1"
_BUCKET_LENGTH = 16


def is_op_reference(value: str) -> bool:
    """Return ``True`` if ``value`` contains an ``op://``/``ops://`` reference.

    Handles ``||`` fallback chains: a value is a reference if *any* segment is
    one (e.g. ``op://V/I/a||literal``). Plain literals and other ``://`` URLs
    (``https://...``) are not references.
    """
    return any(seg.strip().startswith(_REFERENCE_PREFIXES) for seg in value.split(CHAIN_SEPARATOR))


def compose_env(
    parent: Mapping[str, str],
    file_envs: Sequence[Mapping[str, str]],
    *,
    override: bool,
) -> dict[str, str]:
    """Layer ``.env`` files over the ``parent`` environment.

    ``parent`` is the inherited environment when ``--inherit-env`` is given, or
    empty otherwise. ``.env`` files **always override** ``parent`` — a loaded
    file is an explicit instruction, and this is what lets ``PATH=${PATH}/new``
    take effect over an inherited ``PATH``.

    ``override`` controls precedence *between files*: ``False`` (default) keeps
    the *first* file to set a key (so an ascend walk's nearest directory, listed
    first, wins); ``True`` lets later files override earlier ones, for a
    defaults-then-tool layering.
    """
    merged: dict[str, str] = {}
    for file_env in file_envs:
        for key, value in file_env.items():
            if override or key not in merged:
                merged[key] = value
    return {**parent, **merged}


def reference_values(env: Mapping[str, str]) -> list[str]:
    """Return the sorted, de-duplicated ``op://`` reference values in ``env``."""
    return sorted({value for value in env.values() if is_op_reference(value)})


def cache_bucket(env: Mapping[str, str]) -> str:
    """Return a reproducible cache-bucket id for ``env``'s reference set.

    The id is a function of the *set* of ``op://`` reference strings only — not
    their order, the variable names they are bound to, or any non-reference
    values. Two invocations that resolve the same secrets therefore share a
    cache file regardless of how the environment was assembled.
    """
    material = "\n".join([_BUCKET_NAMESPACE, *reference_values(env)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:_BUCKET_LENGTH]


def resolve_env(env: Mapping[str, str], op: OnePassword) -> dict[str, str]:
    """Resolve every ``op://`` value in ``env`` via ``op``; pass plain values through.

    Raises :class:`ResolutionError` (naming the non-secret reference, never a
    value) if a reference resolves to nothing.
    """
    resolved: dict[str, str] = {}
    for key, value in env.items():
        if not is_op_reference(value):
            resolved[key] = value
            continue
        result = op.resolve(FieldValue.from_raw(value, key))
        if result is None:
            raise ResolutionError(f"could not resolve {key} ({value})")
        resolved[key] = result
    return resolved


def check_required(env: Mapping[str, str], required: Iterable[str]) -> None:
    """Raise :class:`MissingKeysError` if any ``required`` key is absent or empty."""
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise MissingKeysError(f"required key(s) unresolved or empty: {', '.join(sorted(missing))}")
