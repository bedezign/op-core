"""Formatters for ``op-env export`` output.

Two output shapes, both consumed by *non-interactive* tooling:

* :func:`format_env` — shell-safe ``KEY='value'`` lines for
  ``set -a; eval "$(op-env export ...)"; set +a``.
* :func:`format_json` — a ``{"KEY": "value"}`` object for an HTTP-headers
  helper or any JSON consumer.

Both print resolved secret values by design. They are only for piping into
``eval`` or a headers helper — never an interactive terminal or a log.
"""

from __future__ import annotations

import json
from collections.abc import Mapping


def format_env(env: Mapping[str, str]) -> str:
    """Render ``env`` as newline-separated, shell-safe ``KEY='value'`` lines.

    Each value is wrapped in single quotes with embedded single quotes escaped
    using the POSIX ``'\\''`` idiom, so the output is inert under ``eval``:
    no parameter expansion, command substitution, or word splitting occurs.
    Keys are emitted in sorted order for deterministic output.
    """
    return "\n".join(f"{key}={_shell_quote(env[key])}" for key in sorted(env))


def format_json(env: Mapping[str, str]) -> str:
    """Render ``env`` as a compact JSON object with sorted keys."""
    return json.dumps(dict(env), ensure_ascii=False, sort_keys=True)


def _shell_quote(value: str) -> str:
    """Single-quote ``value`` for safe use in a POSIX shell."""
    return "'" + value.replace("'", "'\\''") + "'"
