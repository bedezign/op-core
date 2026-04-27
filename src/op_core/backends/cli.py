"""Subprocess-based 1Password CLI backend.

:class:`CLIBackend` wraps the ``op`` command-line tool. Every public method
routes through :func:`_run`, which handles subprocess invocation, auth
env-var injection, timeout handling, and error mapping. The two
``_parse_*`` helpers normalize ``op``'s JSON output into op-core's
canonical :class:`Item` / :class:`ItemSummary` shapes and are module-level
so parser behavior can be unit-tested directly against captured JSON
fixtures.

Error-string matching in :func:`_map_error` is heuristic — ``op`` does
not publish stable error strings. The contract tests against a real
vault will lock in whatever phrases 1Password actually emits.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Sequence
from typing import Any

from op_core.auth import Auth, DesktopAuth, ServiceAccountAuth
from op_core.backends._filters import validate_filter
from op_core.exceptions import (
    OpAuthError,
    OpError,
    OpNotFoundError,
    OpOfflineError,
    OpTimeoutError,
)
from op_core.items import Item, ItemField, ItemRef, ItemSection, ItemSummary, VaultSummary

_DEFAULT_AUTH = DesktopAuth()

_AUTH_PHRASES = (
    "not signed in",
    "not currently signed in",
    "session expired",
    "please sign in",
    "invalid token",
)
_NOT_FOUND_PHRASES = (
    "not found",
    "isn't an item",
    "doesn't exist",
    "no item",
    # `op read op://V/I/missing-field` against a real item with a missing field
    # emits e.g. "item 'V/I' does not have a field 'missing-field'".
    "does not have a field",
)


def _map_error(return_code: int, stderr: str) -> OpError:
    """Classify an ``op`` stderr message into the appropriate exception type."""
    lowered = stderr.lower()
    message = stderr.strip() or f"op exited with code {return_code}"
    if any(p in lowered for p in _AUTH_PHRASES):
        return OpAuthError(message)
    if any(p in lowered for p in _NOT_FOUND_PHRASES):
        return OpNotFoundError(message)
    return OpError(message)


def _normalize_tags(raw: Any) -> tuple[str, ...]:
    """Normalize the ``tags`` field from an op JSON payload.

    ``op`` has emitted tags as bare strings in some versions and as dicts
    of the form ``{"name": "..."}`` in others. Accept both.
    """
    if not raw:
        return ()
    result: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            result.append(entry)
        elif isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str):
                result.append(name)
    return tuple(result)


def _parse_vault_summary(data: dict[str, Any]) -> VaultSummary:
    return VaultSummary(id=data["id"], name=data.get("name", ""))


def _parse_item_summary(data: dict[str, Any]) -> ItemSummary:
    vault = data.get("vault", {})
    return ItemSummary(
        id=data["id"],
        title=data["title"],
        vault_id=vault.get("id", ""),
        vault_name=vault.get("name", ""),
        category=data.get("category", ""),
        tags=_normalize_tags(data.get("tags")),
    )


def _parse_item(data: dict[str, Any]) -> Item:
    vault = data.get("vault", {})
    sections = tuple(ItemSection(id=s["id"], label=s.get("label", "")) for s in data.get("sections", []))
    fields = tuple(
        ItemField(
            id=f["id"],
            label=f.get("label", ""),
            value=f.get("value"),
            type=f.get("type", ""),
            section_id=(f.get("section") or {}).get("id"),
        )
        for f in data.get("fields", [])
    )
    return Item(
        id=data["id"],
        title=data["title"],
        vault_id=vault.get("id", ""),
        vault_name=vault.get("name", ""),
        category=data.get("category", ""),
        tags=_normalize_tags(data.get("tags")),
        sections=sections,
        fields=fields,
    )


class CLIBackend:
    """Backend that shells out to the ``op`` command-line tool."""

    def __init__(
        self,
        auth: Auth = _DEFAULT_AUTH,
        *,
        binary: str = "op",
        timeout: float = 120,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._auth = auth
        self._binary = binary
        self._timeout = timeout

    def _run(self, args: list[str]) -> str:
        env = os.environ.copy()
        match self._auth:
            case ServiceAccountAuth(token=t):
                env["OP_SERVICE_ACCOUNT_TOKEN"] = t
            case DesktopAuth():
                env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
        try:
            result = subprocess.run(
                [self._binary, *args],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=env,
            )
        except FileNotFoundError as exc:
            raise OpError(f"op CLI not found: {self._binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OpTimeoutError(f"op command timed out after {self._timeout}s") from exc
        if result.returncode != 0:
            raise _map_error(result.returncode, result.stderr)
        return result.stdout

    def read(
        self,
        reference: str,
        *,
        default_value: str | None = None,
        online: bool = True,
    ) -> str:
        if not online:
            raise OpOfflineError(f"CLIBackend cannot satisfy {reference} offline")
        try:
            return self._run(["read", reference]).rstrip("\n")
        except OpNotFoundError:
            if default_value is None:
                raise
            return default_value

    def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        validate_filter("tags", tags)
        validate_filter("categories", categories)
        args = ["item", "list", "--format", "json"]
        if vault is not None:
            args += ["--vault", vault]
        if tags is not None:
            args += ["--tags", ",".join(tags)]
        # Categories are filtered client-side: `op item list --categories` only
        # accepts display labels (e.g. "SSH Key"), but op-core's contract is
        # canonical upper-case (e.g. "SSH_KEY") and must match SDKBackend.
        payload = json.loads(self._run(args))
        summaries = [_parse_item_summary(entry) for entry in payload]
        if categories is not None:
            wanted = set(categories)
            summaries = [s for s in summaries if s.category in wanted]
        return summaries

    def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        item_id = item if isinstance(item, str) else item.id
        effective_vault = vault
        if effective_vault is None and not isinstance(item, str):
            effective_vault = item.vault_id
        args = ["item", "get", item_id, "--format", "json"]
        if effective_vault is not None:
            args += ["--vault", effective_vault]
        return _parse_item(json.loads(self._run(args)))

    def list_vaults(self) -> list[VaultSummary]:
        payload = json.loads(self._run(["vault", "list", "--format", "json"]))
        return [_parse_vault_summary(entry) for entry in payload]


class AsyncCLIBackend:
    """Backend that invokes ``op`` via :func:`asyncio.create_subprocess_exec`.

    Mirrors :class:`CLIBackend` method-for-method, sharing all parsers and
    error-mapping helpers. Timeouts kill the child process to prevent
    orphaned ``op`` invocations from lingering past the caller's deadline.
    """

    def __init__(
        self,
        auth: Auth = _DEFAULT_AUTH,
        *,
        binary: str = "op",
        timeout: float = 120,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._auth = auth
        self._binary = binary
        self._timeout = timeout

    async def _arun(self, args: list[str]) -> str:
        env = os.environ.copy()
        match self._auth:
            case ServiceAccountAuth(token=t):
                env["OP_SERVICE_ACCOUNT_TOKEN"] = t
            case DesktopAuth():
                env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            raise OpError(f"op CLI not found: {self._binary}") from exc
        try:
            async with asyncio.timeout(self._timeout):
                stdout_bytes, stderr_bytes = await proc.communicate()
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise OpTimeoutError(f"op command timed out after {self._timeout}s") from exc
        return_code = proc.returncode
        assert return_code is not None  # guaranteed by communicate() returning
        if return_code != 0:
            raise _map_error(return_code, stderr_bytes.decode())
        return stdout_bytes.decode()

    async def read(
        self,
        reference: str,
        *,
        default_value: str | None = None,
        online: bool = True,
    ) -> str:
        if not online:
            raise OpOfflineError(f"AsyncCLIBackend cannot satisfy {reference} offline")
        try:
            return (await self._arun(["read", reference])).rstrip("\n")
        except OpNotFoundError:
            if default_value is None:
                raise
            return default_value

    async def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        validate_filter("tags", tags)
        validate_filter("categories", categories)
        args = ["item", "list", "--format", "json"]
        if vault is not None:
            args += ["--vault", vault]
        if tags is not None:
            args += ["--tags", ",".join(tags)]
        # See CLIBackend.list_items for why categories are filtered client-side.
        payload = json.loads(await self._arun(args))
        summaries = [_parse_item_summary(entry) for entry in payload]
        if categories is not None:
            wanted = set(categories)
            summaries = [s for s in summaries if s.category in wanted]
        return summaries

    async def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        item_id = item if isinstance(item, str) else item.id
        effective_vault = vault
        if effective_vault is None and not isinstance(item, str):
            effective_vault = item.vault_id
        args = ["item", "get", item_id, "--format", "json"]
        if effective_vault is not None:
            args += ["--vault", effective_vault]
        return _parse_item(json.loads(await self._arun(args)))

    async def list_vaults(self) -> list[VaultSummary]:
        payload = json.loads(await self._arun(["vault", "list", "--format", "json"]))
        return [_parse_vault_summary(entry) for entry in payload]
