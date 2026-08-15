"""Official 1Password Python SDK backend.

:class:`AsyncSDKBackend` is the native shape: it wraps the
``onepassword.Client`` (installed via the ``op-core[sdk]`` extra) and
calls its coroutine methods directly. :class:`SDKBackend` is a thin
sync facade that drives an ``AsyncSDKBackend`` on a persistent daemon
thread running its own event loop — each sync call submits a coroutine
via :func:`asyncio.run_coroutine_threadsafe` and blocks on the result.

Because the official SDK only accepts service-account tokens, the
public constructor is type-hinted as :class:`ServiceAccountAuth`
explicitly; passing :class:`DesktopAuth` is a compile-time (and
runtime) error.

Several behaviors that the CLI backend gets "for free" from ``op`` are
implemented client-side here because the SDK's surface is leaner:

* ``list_items`` with no vault enumerates vaults and lists each.
* Tag/category filtering is applied in Python — the SDK only supports
  a state filter (active/archived).
* Item summaries and full items carry ``vault_name=""`` because the
  SDK's per-item responses omit the vault title. Callers that need
  vault titles should resolve them separately via the vault list.
* Item ``category`` is mapped via an explicit table to match
  :class:`CLIBackend`'s canonical upper-snake-case form — the SDK returns
  ``"SshKey"`` where the CLI returns ``"SSH_KEY"`` (see
  ``_SDK_CATEGORY_MAP``).

Error mapping is heuristic: the SDK surfaces most failures as plain
``Exception`` instances carrying the underlying error message as
text. We pattern-match on the message the same way
:mod:`op_core.backends.cli` does.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import importlib
import logging
import re
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from op_core.auth import ServiceAccountAuth
from op_core.backends._filters import validate_filter
from op_core.exceptions import (
    OpAuthError,
    OpError,
    OpNotFoundError,
    OpOfflineError,
    OpTimeoutError,
)
from op_core.items import Item, ItemField, ItemRef, ItemSection, ItemSummary, ItemURL, VaultSummary

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

log = logging.getLogger(__name__)

_INTEGRATION_NAME = "op-core"
_INTEGRATION_VERSION = "0.9.0"

# Maps the official SDK's PascalCase `ItemCategory` value (`member.value`,
# e.g. "SshKey") to op-core's canonical upper-snake-case form (e.g.
# "SSH_KEY") -- the same form CLIBackend gets for free from `op`'s own JSON
# output. A bare `.upper()` is only correct for the single-word members;
# every multi-word member needs an inserted underscore, so this is an
# explicit table rather than a regex guess. Keep in sync with
# ``onepassword.types.ItemCategory`` -- ``TestNormalizeCategory
# .test_covers_every_real_sdk_member`` fails if the installed SDK adds a
# member this table doesn't know about.
_SDK_CATEGORY_MAP: dict[str, str] = {
    "Login": "LOGIN",
    "SecureNote": "SECURE_NOTE",
    "CreditCard": "CREDIT_CARD",
    "CryptoWallet": "CRYPTO_WALLET",
    "Identity": "IDENTITY",
    "Password": "PASSWORD",
    "Document": "DOCUMENT",
    "ApiCredentials": "API_CREDENTIALS",
    "BankAccount": "BANK_ACCOUNT",
    "Database": "DATABASE",
    "DriverLicense": "DRIVER_LICENSE",
    "Email": "EMAIL",
    "MedicalRecord": "MEDICAL_RECORD",
    "Membership": "MEMBERSHIP",
    "OutdoorLicense": "OUTDOOR_LICENSE",
    "Passport": "PASSPORT",
    "Rewards": "REWARDS",
    "Router": "ROUTER",
    "Server": "SERVER",
    "SshKey": "SSH_KEY",
    "SocialSecurityNumber": "SOCIAL_SECURITY_NUMBER",
    "SoftwareLicense": "SOFTWARE_LICENSE",
    "Person": "PERSON",
    "Unsupported": "UNSUPPORTED",
}

_CATEGORY_WORD_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")

_AUTH_PHRASES = (
    "unauthorized",
    "invalid token",
    "authentication",
    "not signed in",
    "session expired",
)
_NOT_FOUND_PHRASES = (
    "not found",
    "no item",
    "itemnotfound",
    "vaultnotfound",
    "fieldnotfound",
    "does not exist",
    "doesn't exist",
)
_TIMEOUT_PHRASES = (
    "timed out",
    "timeout",
    "deadline exceeded",
)


def _load_sdk() -> Any:
    """Import the official ``onepassword`` SDK or raise :class:`OpError`.

    The import is lazy so ``import op_core.backends.sdk`` does not require
    the ``op-core[sdk]`` extra to be installed.
    """
    try:
        return importlib.import_module("onepassword")
    except ImportError as exc:
        raise OpError("op-core[sdk] not installed; pip install 'op-core[sdk]'") from exc


def _map_sdk_error(exc: BaseException) -> OpError:
    """Translate an SDK exception into an op-core exception.

    The SDK raises :class:`DesktopSessionExpiredException`,
    :class:`RateLimitExceededException`, or a bare ``Exception`` whose
    message carries the underlying error text. We pattern-match on the
    message so the classification is consistent with
    :func:`op_core.backends.cli._map_error`.
    """
    message = str(exc) or exc.__class__.__name__
    lowered = message.lower()
    if any(p in lowered for p in _NOT_FOUND_PHRASES):
        return OpNotFoundError(message)
    if any(p in lowered for p in _AUTH_PHRASES):
        return OpAuthError(message)
    if any(p in lowered for p in _TIMEOUT_PHRASES):
        return OpTimeoutError(message)
    return OpError(message)


def _stringify_enum(value: Any) -> str:
    """Return the raw string form of an SDK enum, or the value unchanged."""
    if value is None:
        return ""
    inner = getattr(value, "value", value)
    return inner if isinstance(inner, str) else str(inner)


def _normalize_category(value: Any) -> str:
    """Return the SDK category in op-core's canonical upper-snake-case form.

    Looks the raw SDK value (e.g. ``"SshKey"``) up in :data:`_SDK_CATEGORY_MAP`.
    A member the SDK added after this table was last updated falls back to
    splitting on capital-letter boundaries and upper-casing -- logged as a
    warning rather than raised, since an unrecognized category must not break
    reads, and rather than silently guessed, since a silent wrong answer is
    the exact failure mode this fallback exists to avoid.
    """
    raw = _stringify_enum(value)
    if not raw:
        return ""
    mapped = _SDK_CATEGORY_MAP.get(raw)
    if mapped is not None:
        return mapped
    fallback = _CATEGORY_WORD_BOUNDARY.sub("_", raw).upper()
    log.warning(
        "op_core.backends.sdk: unmapped SDK category %r; falling back to %r. "
        "Update _SDK_CATEGORY_MAP in op_core.backends.sdk.",
        raw,
        fallback,
    )
    return fallback


def _sdk_overview_to_summary(overview: Any) -> ItemSummary:
    """Convert an ``onepassword.types.ItemOverview`` to :class:`ItemSummary`.

    The SDK overview does not carry a vault title, so ``vault_name`` is
    always the empty string. Callers that need the title should look it
    up via the vault list.
    """
    return ItemSummary(
        id=overview.id,
        title=overview.title,
        vault_id=overview.vault_id,
        vault_name="",
        category=_normalize_category(getattr(overview, "category", "")),
        tags=tuple(getattr(overview, "tags", ()) or ()),
    )


def _sdk_item_to_canonical(sdk_item: Any) -> Item:
    """Convert an ``onepassword.types.Item`` to the canonical :class:`Item`.

    The SDK ``Item`` exposes ``fields`` and ``sections`` as lists of
    pydantic models. We map each, preserving the SDK's string field-type
    values (e.g. ``"Concealed"``, ``"Text"``). Empty string values are
    mapped to ``None`` on the canonical ``ItemField`` to match the CLI
    backend's nullable-value shape.

    URLs are sourced from ``sdk_item.websites`` (the SDK's term for what
    the CLI calls ``urls``) and mapped to :class:`ItemURL`. Missing or
    empty labels are populated with the convention ``"website"`` (matching
    the CLI parser). The SDK's ``Website`` type has no ``primary`` flag —
    only ``url``, ``label``, and ``autofill_behavior`` — so every
    SDK-sourced URL carries ``primary=False``. Callers that need the
    primary marker must use :class:`~op_core.backends.cli.CLIBackend`.
    """
    sections = tuple(
        ItemSection(id=s.id, label=getattr(s, "title", "") or "") for s in getattr(sdk_item, "sections", []) or []
    )
    fields = tuple(
        ItemField(
            id=f.id,
            label=getattr(f, "title", "") or "",
            value=(f.value if getattr(f, "value", "") != "" else None),
            type=_stringify_enum(getattr(f, "field_type", "")),
            section_id=getattr(f, "section_id", None),
        )
        for f in getattr(sdk_item, "fields", []) or []
    )
    urls = tuple(
        ItemURL(
            href=getattr(w, "url", "") or "",
            label=(getattr(w, "label", "") or "website"),
            primary=False,
        )
        for w in getattr(sdk_item, "websites", []) or []
        if getattr(w, "url", "")
    )
    return Item(
        id=sdk_item.id,
        title=sdk_item.title,
        vault_id=sdk_item.vault_id,
        vault_name="",
        category=_normalize_category(getattr(sdk_item, "category", "")),
        tags=tuple(getattr(sdk_item, "tags", ()) or ()),
        sections=sections,
        fields=fields,
        urls=urls,
    )


class AsyncSDKBackend:
    """Native async backend wrapping the official ``onepassword`` SDK.

    Only :class:`ServiceAccountAuth` is accepted — the SDK has no
    desktop-auth code path. The underlying ``onepassword.Client`` is
    constructed lazily on the first method call so that creating an
    ``AsyncSDKBackend`` outside an event loop is cheap and does not
    perform I/O.

    A ``client`` seam is provided for tests: pass any object exposing
    ``secrets``, ``items``, and ``vaults`` namespaces and the backend
    will use it directly without loading the real SDK.
    """

    def __init__(
        self,
        auth: ServiceAccountAuth,
        *,
        client: Any | None = None,
    ) -> None:
        if not isinstance(auth, ServiceAccountAuth):
            raise TypeError(
                "AsyncSDKBackend only supports ServiceAccountAuth; the official 1Password SDK has no desktop-auth path"
            )
        self._auth = auth
        self._client: Any | None = client
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                sdk = _load_sdk()
                try:
                    self._client = await sdk.Client.authenticate(
                        self._auth.token,
                        _INTEGRATION_NAME,
                        _INTEGRATION_VERSION,
                    )
                except Exception as exc:
                    raise _map_sdk_error(exc) from exc
        return self._client

    async def read(
        self,
        reference: str,
        *,
        default_value: str | None = None,
        online: bool = True,
    ) -> str:
        if not online:
            raise OpOfflineError(f"AsyncSDKBackend cannot satisfy {reference} offline")
        client = await self._get_client()
        try:
            return await client.secrets.resolve(reference)
        except Exception as exc:
            mapped = _map_sdk_error(exc)
            if isinstance(mapped, OpNotFoundError) and default_value is not None:
                return default_value
            raise mapped from exc

    async def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        validate_filter("tags", tags)
        validate_filter("categories", categories)
        client = await self._get_client()
        vault_ids: list[str]
        if vault is not None:
            vault_ids = [vault]
        else:
            try:
                overviews = await client.vaults.list()
            except Exception as exc:
                raise _map_sdk_error(exc) from exc
            vault_ids = [v.id for v in overviews]

        summaries: list[ItemSummary] = []
        for vid in vault_ids:
            try:
                raw = await client.items.list(vid)
            except Exception as exc:
                raise _map_sdk_error(exc) from exc
            summaries.extend(_sdk_overview_to_summary(o) for o in raw)

        if tags is not None:
            wanted_tags = set(tags)
            summaries = [s for s in summaries if wanted_tags.intersection(s.tags)]
        if categories is not None:
            wanted_cats = set(categories)
            summaries = [s for s in summaries if s.category in wanted_cats]
        return summaries

    async def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        item_id = item if isinstance(item, str) else item.id
        effective_vault = vault
        if effective_vault is None and not isinstance(item, str):
            effective_vault = item.vault_id
        if not effective_vault:
            raise OpError(
                "AsyncSDKBackend.get_item requires a vault: pass vault= or an ItemSummary/Item carrying vault_id"
            )
        client = await self._get_client()
        try:
            raw = await client.items.get(effective_vault, item_id)
        except Exception as exc:
            raise _map_sdk_error(exc) from exc
        return _sdk_item_to_canonical(raw)

    async def list_vaults(self) -> list[VaultSummary]:
        # The SDK's VaultOverview exposes `id` and `title`; op-core's
        # canonical shape names the human label `name` (matching CLIBackend
        # and `op vault list --format json`), so we map title -> name here.
        client = await self._get_client()
        try:
            overviews = await client.vaults.list()
        except Exception as exc:
            raise _map_sdk_error(exc) from exc
        return [VaultSummary(id=v.id, name=v.title or "") for v in overviews]


class SDKBackend:
    """Sync facade that drives an :class:`AsyncSDKBackend` on a background loop.

    A daemon thread runs a dedicated :class:`asyncio.AbstractEventLoop`
    for the lifetime of the process; every sync call submits its
    coroutine via :func:`asyncio.run_coroutine_threadsafe` and blocks
    on the resulting future. Because the thread is a daemon, the
    interpreter will tear it down on exit; an :mod:`atexit` hook also
    stops the loop cleanly if the backend is still reachable.

    Only :class:`ServiceAccountAuth` is accepted — the underlying SDK
    has no desktop-auth path.
    """

    def __init__(self, auth: ServiceAccountAuth) -> None:
        if not isinstance(auth, ServiceAccountAuth):
            raise TypeError(
                "SDKBackend only supports ServiceAccountAuth; the official 1Password SDK has no desktop-auth path"
            )
        self._async = AsyncSDKBackend(auth)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="op-core-sdk-backend",
            daemon=True,
        )
        self._thread.start()
        atexit.register(self._shutdown)

    def _shutdown(self) -> None:
        loop = self._loop
        if loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):  # pragma: no cover - loop already torn down
            loop.call_soon_threadsafe(loop.stop)

    def _run(self, coro: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def read(
        self,
        reference: str,
        *,
        default_value: str | None = None,
        online: bool = True,
    ) -> str:
        if not online:
            raise OpOfflineError(f"SDKBackend cannot satisfy {reference} offline")
        return self._run(self._async.read(reference, default_value=default_value, online=online))

    def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        return self._run(self._async.list_items(vault=vault, tags=tags, categories=categories))

    def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        return self._run(self._async.get_item(item, vault=vault))

    def list_vaults(self) -> list[VaultSummary]:
        return self._run(self._async.list_vaults())
