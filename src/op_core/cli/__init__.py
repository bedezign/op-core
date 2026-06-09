"""Command-line entry points for op-core.

The :mod:`op_core.cli` package powers the ``op-env`` console command, which
composes an environment from the current process environment plus zero or more
``.env`` files, resolves any ``op://`` references via op-core, and then either
execs a child process with the composed environment or emits it for
``eval`` / headers consumption.

The package depends only on the standard library so op-core's base install
stays zero-dependency.
"""

from __future__ import annotations
