"""Error types for the ``op-env`` command.

A single :class:`OpEnvError` base lets the CLI entry point catch every
expected, user-facing failure (bad ``.env`` file, unresolved reference,
missing required key) and turn it into a clean ``stderr`` line plus a non-zero
exit code — rather than dumping a traceback. Unexpected errors still propagate.
"""

from __future__ import annotations


class OpEnvError(Exception):
    """Base class for expected, user-facing ``op-env`` failures."""


class ResolutionError(OpEnvError):
    """An ``op://`` reference could not be resolved to a value."""


class MissingKeysError(OpEnvError):
    """One or more ``--require`` keys are absent or empty after resolution."""
