"""1Password authentication strategies.

Op-core supports two auth models:

* :class:`ServiceAccountAuth` — a service-account token sourced from an
  environment variable. Works with both the CLI and the official SDK.
* :class:`DesktopAuth` — ambient 1Password desktop app session. CLI-only.

Backends consume these via a ``match`` statement and apply the credential
to their underlying transport (subprocess env for the CLI, SDK client
constructor for the SDK).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Self

from op_core.exceptions import OpAuthError

SERVICE_ACCOUNT_ENV_VAR = "OP_SERVICE_ACCOUNT_TOKEN"


@dataclass(frozen=True)
class ServiceAccountAuth:
    token: str  # the raw OP_SERVICE_ACCOUNT_TOKEN value

    @classmethod
    def from_env(cls, *, var: str = SERVICE_ACCOUNT_ENV_VAR) -> Self:
        value = os.environ.get(var, "")
        if not value:
            raise OpAuthError(f"environment variable {var!r} is not set or empty")
        return cls(token=value)


@dataclass(frozen=True)
class DesktopAuth:
    pass


Auth = ServiceAccountAuth | DesktopAuth


def detect_auth() -> Auth:
    if os.environ.get(SERVICE_ACCOUNT_ENV_VAR):
        return ServiceAccountAuth(token=os.environ[SERVICE_ACCOUNT_ENV_VAR])
    return DesktopAuth()
