"""Exceptions raised by op-core.

All op-core errors inherit from :class:`OpError`, so callers can catch the
base class to handle any 1Password failure or target specific subclasses
for finer-grained handling.
"""

from __future__ import annotations


class OpError(Exception):
    """Base class for all op-core errors."""


class OpAuthError(OpError):
    """1Password authentication or authorization failed.

    Raised for signed-out sessions, expired or invalid service account tokens,
    and operations that lack access to the requested vault or item.
    """


class OpNotFoundError(OpError):
    """The requested 1Password vault, item, or field does not exist."""


class OpTimeoutError(OpError):
    """A 1Password operation exceeded its allotted time."""


class OpOfflineError(OpError):
    """A value could not be produced without going online.

    Raised when a backend was asked to honor ``online=False`` but could not
    satisfy the request from local state alone. Distinct from
    :class:`OpNotFoundError`: the value may still exist upstream — the caller
    simply forbade the network round-trip needed to check.
    """
