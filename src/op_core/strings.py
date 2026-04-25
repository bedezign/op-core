"""String utilities.

Small, dependency-free helpers that are useful across op-core consumers.
"""

from __future__ import annotations

import re

_BRACE_RE = re.compile(r'^(.*?)\{([^}]+)\}(.*)$')
_RANGE_RE = re.compile(r'^(\d+)\.\.(\d+)$')


def expand_braces(pattern: str) -> list[str]:
    """Expand a single brace expression in a string.

    Supports:

    * Comma lists — ``host{1,2,3}`` → ``['host1', 'host2', 'host3']``
    * Numeric ranges — ``worker{1..8}`` → ``['worker1', ..., 'worker8']``

    Only the first brace pair is expanded. Inputs without a valid brace
    expression (including empty braces like ``host{}``) pass through
    unchanged as a single-element list.
    """
    match = _BRACE_RE.match(pattern)
    if not match:
        return [pattern]

    prefix, expr, suffix = match.groups()

    range_match = _RANGE_RE.match(expr)
    if range_match:
        start, end = int(range_match.group(1)), int(range_match.group(2))
        return [f'{prefix}{i}{suffix}' for i in range(start, end + 1)]

    if ',' in expr:
        parts = [p.strip() for p in expr.split(',')]
        return [f'{prefix}{p}{suffix}' for p in parts]

    return [pattern]
