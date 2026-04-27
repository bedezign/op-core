"""Tests for SDKBackend and AsyncSDKBackend.

Two tiers:

* Tier A (always run): uses hand-rolled fakes that mimic the shape of
  the official ``onepassword.Client``. Covers wrapping logic, filter
  validation, error mapping, ItemRef handling, and the sync wrapper's
  thread/loop plumbing.
* Tier B (skipped if ``onepassword`` is not installed): verifies that
  the real SDK exposes the attributes ``sdk.py`` depends on and that
  the mapping helpers work on objects built from the real pydantic
  types. No network or real API calls.
"""

from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import dataclass, field
from typing import Any

import pytest

from op_core.auth import DesktopAuth, ServiceAccountAuth
from op_core.backends.sdk import (
    AsyncSDKBackend,
    SDKBackend,
    _map_sdk_error,
    _sdk_item_to_canonical,
    _sdk_overview_to_summary,
)
from op_core.exceptions import (
    OpAuthError,
    OpError,
    OpNotFoundError,
    OpTimeoutError,
)
from op_core.items import Item, ItemSummary, VaultSummary

# ---------- minimal SDK-shaped fakes (Tier A) ----------


@dataclass
class FakeSection:
    id: str
    title: str = ""


@dataclass
class FakeField:
    id: str
    title: str = ""
    section_id: str | None = None
    field_type: str = "Text"
    value: str = ""
    details: Any = None


@dataclass
class FakeOverview:
    id: str
    title: str
    vault_id: str
    category: str = "Login"
    tags: list[str] = field(default_factory=list)


@dataclass
class FakeItem:
    id: str
    title: str
    vault_id: str
    category: str = "Login"
    tags: list[str] = field(default_factory=list)
    fields: list[FakeField] = field(default_factory=list)
    sections: list[FakeSection] = field(default_factory=list)


@dataclass
class FakeVaultOverview:
    id: str
    title: str = ""


class FakeSecrets:
    def __init__(self) -> None:
        self.resolve_calls: list[str] = []
        self.values: dict[str, str] = {}
        self.raise_on: Exception | None = None

    async def resolve(self, reference: str) -> str:
        self.resolve_calls.append(reference)
        if self.raise_on is not None:
            raise self.raise_on
        if reference not in self.values:
            raise Exception(f"item not found: {reference}")
        return self.values[reference]


class FakeItems:
    def __init__(self) -> None:
        self.list_calls: list[str] = []
        self.get_calls: list[tuple[str, str]] = []
        self.overviews_by_vault: dict[str, list[FakeOverview]] = {}
        self.items_by_key: dict[tuple[str, str], FakeItem] = {}
        self.raise_on_list: Exception | None = None
        self.raise_on_get: Exception | None = None

    async def list(self, vault_id: str, *filters: Any) -> list[FakeOverview]:
        self.list_calls.append(vault_id)
        if self.raise_on_list is not None:
            raise self.raise_on_list
        return list(self.overviews_by_vault.get(vault_id, []))

    async def get(self, vault_id: str, item_id: str) -> FakeItem:
        self.get_calls.append((vault_id, item_id))
        if self.raise_on_get is not None:
            raise self.raise_on_get
        key = (vault_id, item_id)
        if key not in self.items_by_key:
            raise Exception("item not found")
        return self.items_by_key[key]


class FakeVaults:
    def __init__(self) -> None:
        self.list_calls = 0
        self.vaults: list[FakeVaultOverview] = []

    async def list(self) -> list[FakeVaultOverview]:
        self.list_calls += 1
        return list(self.vaults)


class FakeClient:
    def __init__(self) -> None:
        self.secrets = FakeSecrets()
        self.items = FakeItems()
        self.vaults = FakeVaults()


def _auth() -> ServiceAccountAuth:
    return ServiceAccountAuth(token="ops_fake_token")


# ---------- _map_sdk_error ----------


class TestMapSdkError:
    def test_not_found(self):
        assert isinstance(_map_sdk_error(Exception("item not found")), OpNotFoundError)
        assert isinstance(_map_sdk_error(Exception("VaultNotFound")), OpNotFoundError)
        assert isinstance(_map_sdk_error(Exception("no such field")), OpError)  # "no such" != "no item"

    def test_auth(self):
        assert isinstance(_map_sdk_error(Exception("invalid token")), OpAuthError)
        assert isinstance(_map_sdk_error(Exception("authentication failed")), OpAuthError)

    def test_timeout(self):
        assert isinstance(_map_sdk_error(Exception("request timed out")), OpTimeoutError)

    def test_generic(self):
        exc = _map_sdk_error(Exception("banana split"))
        assert type(exc) is OpError
        assert "banana split" in str(exc)

    def test_blank_message_falls_back_to_class_name(self):
        exc = _map_sdk_error(ValueError(""))
        assert "ValueError" in str(exc)


# ---------- mapping helpers ----------


class TestMappingHelpers:
    def test_overview_to_summary(self):
        ov = FakeOverview(
            id="i1",
            title="My Login",
            vault_id="v1",
            category="Login",
            tags=["prod", "critical"],
        )
        s = _sdk_overview_to_summary(ov)
        assert s == ItemSummary(
            id="i1",
            title="My Login",
            vault_id="v1",
            vault_name="",
            category="LOGIN",
            tags=("prod", "critical"),
        )

    def test_item_to_canonical_empty_sections_fields(self):
        fi = FakeItem(id="i1", title="T", vault_id="v1", category="Login")
        canonical = _sdk_item_to_canonical(fi)
        assert canonical.id == "i1"
        assert canonical.vault_id == "v1"
        assert canonical.vault_name == ""
        assert canonical.sections == ()
        assert canonical.fields == ()

    def test_item_to_canonical_with_sections_and_fields(self):
        fi = FakeItem(
            id="i1",
            title="T",
            vault_id="v1",
            category="Login",
            tags=["a"],
            sections=[FakeSection(id="s1", title="Section A")],
            fields=[
                FakeField(
                    id="f1",
                    title="username",
                    section_id=None,
                    field_type="Text",
                    value="alice",
                ),
                FakeField(
                    id="f2",
                    title="password",
                    section_id="s1",
                    field_type="Concealed",
                    value="",  # empty -> None
                ),
            ],
        )
        canonical = _sdk_item_to_canonical(fi)
        assert canonical.tags == ("a",)
        assert len(canonical.sections) == 1
        assert canonical.sections[0].label == "Section A"
        assert len(canonical.fields) == 2
        assert canonical.fields[0].label == "username"
        assert canonical.fields[0].value == "alice"
        assert canonical.fields[0].type == "Text"
        assert canonical.fields[0].section_id is None
        assert canonical.fields[1].value is None
        assert canonical.fields[1].type == "Concealed"
        assert canonical.fields[1].section_id == "s1"


# ---------- AsyncSDKBackend ----------


class TestAsyncSDKBackendAuth:
    def test_rejects_desktop_auth(self):
        with pytest.raises(TypeError):
            AsyncSDKBackend(DesktopAuth())  # type: ignore[arg-type]


class TestAsyncSDKBackendRead:
    async def test_read_success(self):
        fc = FakeClient()
        fc.secrets.values["op://vault/item/field"] = "s3cret"
        backend = AsyncSDKBackend(_auth(), client=fc)
        got = await backend.read("op://vault/item/field")
        assert got == "s3cret"
        assert fc.secrets.resolve_calls == ["op://vault/item/field"]

    async def test_read_missing_raises(self):
        fc = FakeClient()
        backend = AsyncSDKBackend(_auth(), client=fc)
        with pytest.raises(OpNotFoundError):
            await backend.read("op://vault/item/missing")

    async def test_read_missing_returns_default(self):
        fc = FakeClient()
        backend = AsyncSDKBackend(_auth(), client=fc)
        got = await backend.read("op://vault/item/missing", default_value="fallback")
        assert got == "fallback"

    async def test_read_empty_string_default(self):
        fc = FakeClient()
        backend = AsyncSDKBackend(_auth(), client=fc)
        got = await backend.read("op://vault/item/missing", default_value="")
        assert got == ""

    async def test_read_auth_error_not_swallowed_by_default(self):
        fc = FakeClient()
        fc.secrets.raise_on = Exception("invalid token")
        backend = AsyncSDKBackend(_auth(), client=fc)
        with pytest.raises(OpAuthError):
            await backend.read("op://x/y/z", default_value="fallback")


class TestAsyncSDKBackendListItems:
    async def test_list_items_filter_validation_tags_empty(self):
        backend = AsyncSDKBackend(_auth(), client=FakeClient())
        with pytest.raises(ValueError):
            await backend.list_items(tags=[])

    async def test_list_items_filter_validation_comma_in_tag(self):
        backend = AsyncSDKBackend(_auth(), client=FakeClient())
        with pytest.raises(ValueError):
            await backend.list_items(tags=["a,b"])

    async def test_list_items_filter_validation_categories_empty(self):
        backend = AsyncSDKBackend(_auth(), client=FakeClient())
        with pytest.raises(ValueError):
            await backend.list_items(categories=[])

    async def test_list_items_single_vault(self):
        fc = FakeClient()
        fc.items.overviews_by_vault["v1"] = [
            FakeOverview(id="i1", title="A", vault_id="v1", category="Login", tags=["t1"]),
            FakeOverview(id="i2", title="B", vault_id="v1", category="SecureNote", tags=["t2"]),
        ]
        backend = AsyncSDKBackend(_auth(), client=fc)
        out = await backend.list_items(vault="v1")
        assert len(out) == 2
        assert fc.items.list_calls == ["v1"]
        assert fc.vaults.list_calls == 0

    async def test_list_items_enumerates_vaults_when_none(self):
        fc = FakeClient()
        fc.vaults.vaults = [FakeVaultOverview(id="v1"), FakeVaultOverview(id="v2")]
        fc.items.overviews_by_vault["v1"] = [
            FakeOverview(id="i1", title="A", vault_id="v1"),
        ]
        fc.items.overviews_by_vault["v2"] = [
            FakeOverview(id="i2", title="B", vault_id="v2"),
        ]
        backend = AsyncSDKBackend(_auth(), client=fc)
        out = await backend.list_items()
        assert [s.id for s in out] == ["i1", "i2"]
        assert fc.vaults.list_calls == 1
        assert sorted(fc.items.list_calls) == ["v1", "v2"]

    async def test_list_items_tag_filter(self):
        fc = FakeClient()
        fc.items.overviews_by_vault["v1"] = [
            FakeOverview(id="i1", title="A", vault_id="v1", tags=["prod"]),
            FakeOverview(id="i2", title="B", vault_id="v1", tags=["dev"]),
            FakeOverview(id="i3", title="C", vault_id="v1", tags=["prod", "critical"]),
        ]
        backend = AsyncSDKBackend(_auth(), client=fc)
        out = await backend.list_items(vault="v1", tags=["prod"])
        assert [s.id for s in out] == ["i1", "i3"]

    async def test_list_items_category_filter(self):
        fc = FakeClient()
        fc.items.overviews_by_vault["v1"] = [
            FakeOverview(id="i1", title="A", vault_id="v1", category="Login"),
            FakeOverview(id="i2", title="B", vault_id="v1", category="SecureNote"),
        ]
        backend = AsyncSDKBackend(_auth(), client=fc)
        out = await backend.list_items(vault="v1", categories=["LOGIN"])
        assert [s.id for s in out] == ["i1"]


class TestAsyncSDKBackendListVaults:
    async def test_empty_account(self):
        fc = FakeClient()
        backend = AsyncSDKBackend(_auth(), client=fc)
        assert await backend.list_vaults() == []
        assert fc.vaults.list_calls == 1

    async def test_parses_vaults(self):
        fc = FakeClient()
        fc.vaults.vaults = [
            FakeVaultOverview(id="v1", title="Personal"),
            FakeVaultOverview(id="v2", title="Shared"),
        ]
        backend = AsyncSDKBackend(_auth(), client=fc)
        result = await backend.list_vaults()
        assert result == [
            VaultSummary(id="v1", name="Personal"),
            VaultSummary(id="v2", name="Shared"),
        ]

    async def test_maps_sdk_errors(self):
        fc = FakeClient()

        # The FakeVaults stub doesn't expose a raise_on hook the way FakeItems does;
        # patch directly to simulate an SDK exception with auth-shaped text.
        async def boom() -> list[FakeVaultOverview]:
            raise Exception("invalid token")

        fc.vaults.list = boom  # type: ignore[method-assign]
        backend = AsyncSDKBackend(_auth(), client=fc)
        with pytest.raises(OpAuthError):
            await backend.list_vaults()


class TestAsyncSDKBackendGetItem:
    def _populated(self) -> FakeClient:
        fc = FakeClient()
        fc.items.items_by_key[("v1", "i1")] = FakeItem(id="i1", title="T", vault_id="v1", category="Login")
        fc.items.items_by_key[("v2", "i1")] = FakeItem(id="i1", title="Other", vault_id="v2", category="Login")
        return fc

    async def test_get_by_string_requires_vault(self):
        fc = self._populated()
        backend = AsyncSDKBackend(_auth(), client=fc)
        with pytest.raises(OpError):
            await backend.get_item("i1")

    async def test_get_by_string_with_explicit_vault(self):
        fc = self._populated()
        backend = AsyncSDKBackend(_auth(), client=fc)
        it = await backend.get_item("i1", vault="v1")
        assert isinstance(it, Item)
        assert fc.items.get_calls == [("v1", "i1")]

    async def test_get_by_summary_uses_summary_vault(self):
        fc = self._populated()
        backend = AsyncSDKBackend(_auth(), client=fc)
        summary = ItemSummary(id="i1", title="T", vault_id="v2", vault_name="", category="Login", tags=())
        it = await backend.get_item(summary)
        assert it.title == "Other"
        assert fc.items.get_calls == [("v2", "i1")]

    async def test_explicit_vault_wins_over_summary(self):
        fc = self._populated()
        backend = AsyncSDKBackend(_auth(), client=fc)
        summary = ItemSummary(id="i1", title="T", vault_id="v2", vault_name="", category="Login", tags=())
        it = await backend.get_item(summary, vault="v1")
        assert it.title == "T"
        assert fc.items.get_calls == [("v1", "i1")]

    async def test_get_not_found_raises(self):
        fc = FakeClient()
        backend = AsyncSDKBackend(_auth(), client=fc)
        with pytest.raises(OpNotFoundError):
            await backend.get_item("ghost", vault="v1")


# ---------- SDKBackend sync wrapper ----------


class RecordingAsync:
    """Pretend to be an AsyncSDKBackend; records calls for the sync wrapper."""

    def __init__(self) -> None:
        self.read_calls: list[tuple[str, str | None]] = []
        self.list_calls: list[tuple[str | None, Any, Any]] = []
        self.list_vaults_calls = 0
        self.get_calls: list[tuple[Any, str | None]] = []

    async def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
        self.read_calls.append((reference, default_value))
        return f"value:{reference}"

    async def list_items(self, *, vault=None, tags=None, categories=None):
        self.list_calls.append((vault, tags, categories))
        return []

    async def list_vaults(self):
        self.list_vaults_calls += 1
        return []

    async def get_item(self, item, *, vault=None):
        self.get_calls.append((item, vault))
        return Item(
            id=item if isinstance(item, str) else item.id,
            title="",
            vault_id=vault or "",
            vault_name="",
            category="",
            tags=(),
            sections=(),
            fields=(),
        )


class TestSDKBackendSync:
    def test_rejects_desktop_auth(self):
        with pytest.raises(TypeError):
            SDKBackend(DesktopAuth())  # type: ignore[arg-type]

    def test_read_drives_async(self):
        backend = SDKBackend(_auth())
        recorder = RecordingAsync()
        backend._async = recorder  # type: ignore[assignment]
        got = backend.read("op://x/y/z")
        assert got == "value:op://x/y/z"
        assert recorder.read_calls == [("op://x/y/z", None)]

    def test_read_forwards_default(self):
        backend = SDKBackend(_auth())
        recorder = RecordingAsync()
        backend._async = recorder  # type: ignore[assignment]
        backend.read("r", default_value="d")
        assert recorder.read_calls == [("r", "d")]

    def test_list_items_forwards_args(self):
        backend = SDKBackend(_auth())
        recorder = RecordingAsync()
        backend._async = recorder  # type: ignore[assignment]
        backend.list_items(vault="v1", tags=["a"], categories=["Login"])
        assert recorder.list_calls == [("v1", ["a"], ["Login"])]

    def test_get_item_forwards_args(self):
        backend = SDKBackend(_auth())
        recorder = RecordingAsync()
        backend._async = recorder  # type: ignore[assignment]
        backend.get_item("i1", vault="v1")
        assert recorder.get_calls == [("i1", "v1")]

    def test_list_vaults_drives_async(self):
        backend = SDKBackend(_auth())
        recorder = RecordingAsync()
        backend._async = recorder  # type: ignore[assignment]
        assert backend.list_vaults() == []
        assert recorder.list_vaults_calls == 1

    def test_background_loop_actually_runs(self):
        backend = SDKBackend(_auth())
        assert backend._thread.is_alive()
        # submit a trivial coroutine directly to prove the loop works
        fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(0, result=42), backend._loop)
        assert fut.result(timeout=2) == 42


# ---------- Tier B: real SDK installed ----------


def _sdk_available() -> bool:
    return importlib.util.find_spec("onepassword") is not None


@pytest.mark.skipif(not _sdk_available(), reason="op-core[sdk] not installed")
class TestRealSDKShape:
    def test_expected_attributes_exist(self):
        import onepassword

        assert hasattr(onepassword, "Client")
        assert hasattr(onepassword.Client, "authenticate")

    def test_client_namespaces_exist(self):
        import onepassword

        # class-level annotations declare these namespaces
        for attr in ("secrets", "items", "vaults"):
            assert attr in onepassword.Client.__annotations__

    def test_mapping_against_real_item_field(self):
        from onepassword.types import (
            Item as SdkItem,
        )
        from onepassword.types import (
            ItemCategory,
            ItemFieldType,
        )
        from onepassword.types import (
            ItemField as SdkField,
        )
        from onepassword.types import (
            ItemSection as SdkSection,
        )

        field_ = SdkField(
            id="f1",
            title="username",
            section_id=None,
            field_type=ItemFieldType.TEXT,
            value="alice",
            details=None,
        )
        section = SdkSection(id="s1", title="Section A")
        item = SdkItem(
            id="i1",
            title="Login",
            category=ItemCategory.LOGIN,
            vault_id="v1",
            fields=[field_],
            sections=[section],
            notes="",
            tags=["prod"],
            websites=[],
            version=1,
            files=[],
            document=None,
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        canonical = _sdk_item_to_canonical(item)
        assert canonical.id == "i1"
        assert canonical.category == "LOGIN"
        assert canonical.tags == ("prod",)
        assert len(canonical.fields) == 1
        assert canonical.fields[0].label == "username"
        assert canonical.fields[0].value == "alice"
        assert canonical.fields[0].type == "Text"
        assert canonical.sections[0].label == "Section A"

    def test_async_backend_construction_with_real_sdk_does_not_crash(self):
        backend = AsyncSDKBackend(ServiceAccountAuth(token="ops_fake"))
        # Lazy: no authentication should have happened yet
        assert backend._client is None
