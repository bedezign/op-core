"""Backend auto-detection.

Picks a :class:`Backend` / :class:`AsyncBackend` implementation based on
the runtime environment:

* ``OP_SERVICE_ACCOUNT_TOKEN`` present? Prefer service-account auth.
* Explicit ``binary`` argument given? Always route to the CLI backend —
  providing a binary path is itself the signal "use the CLI".
* SDK extra (``onepassword``) installed? Prefer it for the
  token-based path because it avoids fork/exec overhead.
* Otherwise fall back to the ``op`` CLI found on ``$PATH``.

Order of checks is designed to minimize disk I/O: the common
"token set + SDK installed + no explicit binary" path never touches
:mod:`shutil.which`.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from typing import Literal

from op_core.auth import SERVICE_ACCOUNT_ENV_VAR, Auth, DesktopAuth, ServiceAccountAuth
from op_core.backends.base import AsyncBackend, Backend
from op_core.exceptions import OpError

_SDK_MODULE_NAME = "onepassword"

BackendKind = Literal["sdk", "cli"]


def _resolve_binary(binary: str | None) -> str | None:
    """Resolve and validate the ``op`` binary path via :func:`shutil.which`.

    Returns the absolute path to an executable on success, or ``None``
    when ``binary`` is ``None`` and ``op`` is not on ``$PATH``.

    An explicit ``binary`` that fails to resolve is a hard error: the
    caller has stated intent ("use this binary") and silently falling
    back to something else would violate that intent.
    """
    if binary is not None:
        resolved = shutil.which(binary)
        if resolved is None:
            raise OpError(f"op binary not found or not executable: {binary}")
        return resolved
    return shutil.which("op")


def _sdk_available() -> bool:
    """Return ``True`` if the optional SDK package can be imported.

    Uses :func:`importlib.util.find_spec` so the package is *not*
    actually imported — this stays cheap and side-effect-free.
    """
    return importlib.util.find_spec(_SDK_MODULE_NAME) is not None


def _decide(binary: str | None) -> tuple[BackendKind, Auth, str | None]:
    """Resolve the matrix row for ``binary`` into ``(kind, auth, path)``.

    ``path`` is only meaningful when ``kind == 'cli'``.
    """
    token_set = bool(os.environ.get(SERVICE_ACCOUNT_ENV_VAR))

    if token_set:
        auth: Auth = ServiceAccountAuth.from_env()
        # Explicit binary always routes to CLI, regardless of SDK presence.
        if binary is not None:
            return "cli", auth, _resolve_binary(binary)
        # Prefer SDK when installed — avoids any disk I/O entirely.
        if _sdk_available():
            return "sdk", auth, None
        # Fall back to CLI on PATH.
        resolved = _resolve_binary(None)
        if resolved is None:
            raise OpError(
                "op-core needs either the [sdk] extra or the op CLI",
            )
        return "cli", auth, resolved

    # No token → desktop auth, which is CLI-only.
    desktop: Auth = DesktopAuth()
    if binary is not None:
        return "cli", desktop, _resolve_binary(binary)
    resolved = _resolve_binary(None)
    if resolved is None:
        raise OpError("op-core needs the op CLI for desktop auth")
    return "cli", desktop, resolved


def detect_backend(*, binary: str | None = None) -> Backend:
    """Return the best available synchronous :class:`Backend`."""
    kind, auth, binary_path = _decide(binary)
    if kind == "sdk":
        # _decide guarantees ServiceAccountAuth when kind == "sdk"; assert for pyright.
        assert isinstance(auth, ServiceAccountAuth)
        from op_core.backends.sdk import SDKBackend

        return SDKBackend(auth)
    from op_core.backends.cli import CLIBackend

    return CLIBackend(auth, binary=binary_path or "op")


def detect_async_backend(*, binary: str | None = None) -> AsyncBackend:
    """Return the best available :class:`AsyncBackend`."""
    kind, auth, binary_path = _decide(binary)
    if kind == "sdk":
        assert isinstance(auth, ServiceAccountAuth)
        from op_core.backends.sdk import AsyncSDKBackend

        return AsyncSDKBackend(auth)
    from op_core.backends.cli import AsyncCLIBackend

    return AsyncCLIBackend(auth, binary=binary_path or "op")
