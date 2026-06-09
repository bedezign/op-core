"""``.env`` loading for the ``op-env`` command, built on python-dotenv.

op-core's base install is zero-dependency; ``.env`` parsing lives behind the
optional ``[cli]`` extra (``pip install op-core[cli]``) which pulls in
``python-dotenv``. This module is a thin wrapper that:

* lazily imports ``dotenv`` so a base install that never runs ``op-env`` does
  not require it, and surfaces an actionable error when the extra is missing;
* turns python-dotenv's lenient ``None`` result for a malformed line into a
  loud :class:`EnvFileError` (op-core does not silently drop assignments);
* loads each file *raw* (no parse-time interpolation). ``${VAR}`` expansion is
  applied later, by :func:`expand_variables`, against an explicit source so it
  resolves only what op-env decides it may see (never an implicit
  ``os.environ``). ``op://`` references are resolved *after* expansion, so a
  resolved secret value is never fed through interpolation (and never mangled).

All actual ``.env`` grammar (quotes, ``export`` prefix, ``#`` comments,
``=`` in values, multi-line quoted values) is python-dotenv's responsibility.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from op_core.cli.errors import OpEnvError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from collections.abc import Set as AbstractSet

_MISSING_EXTRA_HINT = "op-env requires the optional [cli] extra: pip install 'op-core[cli]'"


class EnvFileError(OpEnvError):
    """Raised when a ``.env`` file cannot be read or contains a malformed line."""


class _DotenvValues(Protocol):
    def __call__(
        self,
        *,
        dotenv_path: str | None = ...,
        stream: io.StringIO | None = ...,
        interpolate: bool = ...,
    ) -> Mapping[str, str | None]: ...


class _Atom(Protocol):
    def resolve(self, env: Mapping[str, str | None]) -> str: ...


class _ParseVariables(Protocol):
    def __call__(self, value: str) -> Iterable[_Atom]: ...


def _dotenv_values() -> _DotenvValues:
    """Return ``dotenv.dotenv_values``, or raise a helpful error if the extra is absent."""
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        raise EnvFileError(_MISSING_EXTRA_HINT) from exc
    return dotenv_values


def _parse_variables() -> _ParseVariables:
    """Return ``dotenv.variables.parse_variables``, or raise if the extra is absent."""
    try:
        from dotenv.variables import parse_variables
    except ImportError as exc:
        raise EnvFileError(_MISSING_EXTRA_HINT) from exc
    return parse_variables


def parse_env(text: str, *, source: str = "<env>") -> dict[str, str]:
    """Parse ``.env`` ``text`` into a ``{KEY: VALUE}`` dict.

    A line python-dotenv cannot turn into an assignment (e.g. a bare ``KEY``
    with no ``=``) is reported as an :class:`EnvFileError` rather than silently
    dropped. ``source`` is only used to locate errors in the message.
    """
    raw = _dotenv_values()(stream=io.StringIO(text), interpolate=False)
    return _normalize(raw, source=source)


def load_env_file(path: str | Path) -> dict[str, str]:
    """Read and parse a ``.env`` file. Raises :class:`EnvFileError` on any failure."""
    p = Path(path)
    if not p.is_file():
        raise EnvFileError(f"env file not found: {p}")
    raw = _dotenv_values()(dotenv_path=str(p), interpolate=False)
    return _normalize(raw, source=str(p))


def _normalize(raw: Mapping[str, str | None], *, source: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            raise EnvFileError(f"{source}: malformed assignment for key {key!r} (no value)")
        result[key] = value
    return result


def expand_variables(
    env: Mapping[str, str],
    *,
    introduced: AbstractSet[str],
    source: Mapping[str, str],
) -> dict[str, str]:
    """Expand ``${VAR}`` / ``${VAR:-default}`` in the ``introduced`` values of ``env``.

    Only keys in ``introduced`` (the ones the ``.env`` files contributed) are
    expanded; every other value passes through verbatim, so an inherited
    environment is never rewritten. Expansion resolves against ``source`` (the
    inherited environment, or empty) plus the values resolved earlier in ``env``
    insertion order — and against *nothing else*. There is no implicit
    ``os.environ`` lookup, so what a ``${VAR}`` can see is exactly what op-env
    decided to expose.

    Each value is resolved *before* it is written back to the working source, so
    ``PATH=${PATH}/new`` reads the inherited ``PATH`` rather than referring to
    its own half-formed value. A reference that points *forward* in ``env`` (or
    forms a cycle) resolves to empty — single forward pass, no recursion.

    Callers must run this *before* resolving ``op://`` references so resolved
    secret values are never themselves subjected to expansion.
    """
    parse = _parse_variables()
    working: dict[str, str | None] = dict(source)
    result: dict[str, str] = {}
    for key, value in env.items():
        if key in introduced:
            value = "".join(atom.resolve(working) for atom in parse(value))
        result[key] = value
        working[key] = value
    return result
