"""op-core: backend-agnostic Python toolkit for 1Password.

Flat re-exports of the public surface. Import everything consumers need
directly from the package root::

    from op_core import OnePassword, CLIBackend, InMemoryBackend, FieldValue

Submodule imports (``from op_core.client import OnePassword``) keep
working but are no longer the recommended form.
"""

from __future__ import annotations

from op_core.auth import Auth, DesktopAuth, ServiceAccountAuth, detect_auth
from op_core.backends.base import AsyncBackend, Backend
from op_core.backends.caching import AsyncCachingBackend, CachingBackend
from op_core.backends.cli import AsyncCLIBackend, CLIBackend
from op_core.backends.detect import detect_async_backend, detect_backend
from op_core.backends.memory import AsyncInMemoryBackend, InMemoryBackend
from op_core.backends.sdk import AsyncSDKBackend, SDKBackend
from op_core.client import AsyncOnePassword, OnePassword
from op_core.exceptions import (
    OpAuthError,
    OpError,
    OpNotFoundError,
    OpOfflineError,
    OpTimeoutError,
)
from op_core.field import (
    TEMPLATE_CLOSE,
    TEMPLATE_OPEN,
    FieldValue,
    classify_type,
    complete_field_refs,
    is_sensitive,
    normalize_original,
)
from op_core.items import Item, ItemField, ItemRef, ItemSection, ItemSummary, VaultSummary
from op_core.opref import OpRef
from op_core.strings import expand_braces

__version__ = "0.1.0"

__all__ = (  # noqa: RUF022 — semantic grouping intentional
    # Version
    "__version__",
    # Facade
    "OnePassword",
    "AsyncOnePassword",
    # Backend protocols (for third-party backends)
    "Backend",
    "AsyncBackend",
    # Backends
    "CLIBackend",
    "AsyncCLIBackend",
    "InMemoryBackend",
    "AsyncInMemoryBackend",
    "CachingBackend",
    "AsyncCachingBackend",
    "SDKBackend",
    "AsyncSDKBackend",
    "detect_backend",
    "detect_async_backend",
    # Auth
    "Auth",
    "ServiceAccountAuth",
    "DesktopAuth",
    "detect_auth",
    # Item models
    "Item",
    "ItemField",
    "ItemSection",
    "ItemSummary",
    "ItemRef",
    "VaultSummary",
    # Field / reference models
    "FieldValue",
    "OpRef",
    # Field helpers
    "normalize_original",
    "classify_type",
    "is_sensitive",
    "complete_field_refs",
    # Template delimiters (op-core does not ship a template engine; these are
    # exported so consumers can recognize op-core's reserved markers without
    # hardcoding their own copies).
    "TEMPLATE_OPEN",
    "TEMPLATE_CLOSE",
    # String helpers
    "expand_braces",
    # Exceptions
    "OpError",
    "OpAuthError",
    "OpNotFoundError",
    "OpTimeoutError",
    "OpOfflineError",
)
