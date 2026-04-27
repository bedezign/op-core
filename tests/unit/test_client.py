"""Tests for OnePassword and AsyncOnePassword facades."""

from __future__ import annotations

import pytest

from op_core.backends.memory import AsyncInMemoryBackend, InMemoryBackend
from op_core.client import AsyncOnePassword, OnePassword
from op_core.exceptions import OpNotFoundError, OpOfflineError
from op_core.field import FieldValue
from op_core.items import Item, ItemField, ItemSection, VaultSummary
from op_core.opref import OpRef


def _make_item(item_id: str = "itm1", *, vault_id: str = "v1") -> Item:
    return Item(
        id=item_id,
        title="T",
        vault_id=vault_id,
        vault_name="Personal",
        category="LOGIN",
        tags=("dev",),
        sections=(ItemSection(id="s1", label="S"),),
        fields=(ItemField(id="f1", label="password", value="p", type="CONCEALED", section_id=None),),
    )


# ---------- OnePassword (sync) ----------


class TestOnePasswordInit:
    def test_accepts_backend(self):
        fake = InMemoryBackend()
        client = OnePassword(backend=fake)
        assert client.backend is fake


class TestOnePasswordRead:
    def test_hit_returns_value(self):
        client = OnePassword(backend=InMemoryBackend(refs={"op://v/i/f": "secret"}))
        assert client.read("op://v/i/f") == "secret"

    def test_miss_returns_none(self):
        client = OnePassword(backend=InMemoryBackend())
        assert client.read("op://v/i/missing") is None

    def test_accepts_opref(self):
        client = OnePassword(backend=InMemoryBackend(refs={"op://v/i/f": "secret"}))
        ref = OpRef.parse("op://v/i/f")
        assert client.read(ref) == "secret"


class TestOnePasswordResolve:
    def test_reference_chain_returns_first_hit(self):
        backend = InMemoryBackend(refs={"op://v/i/b": "second"})
        client = OnePassword(backend=backend)
        field = FieldValue.from_raw("op://v/i/a||op://v/i/b", "password")
        assert client.resolve(field) == "second"

    def test_literal_fallback(self):
        client = OnePassword(backend=InMemoryBackend())
        field = FieldValue.from_raw("op://v/i/missing||literal", "api_key")
        assert client.resolve(field) == "literal"

    def test_all_missing_returns_none(self):
        client = OnePassword(backend=InMemoryBackend())
        field = FieldValue.from_raw("op://v/i/x||op://v/i/y", "api_key")
        assert client.resolve(field) is None


class TestOnePasswordPassthrough:
    def test_list_items(self):
        backend = InMemoryBackend(items=[_make_item("a"), _make_item("b")])
        client = OnePassword(backend=backend)
        out = client.list_items()
        assert [s.id for s in out] == ["a", "b"]

    def test_get_item(self):
        backend = InMemoryBackend(items=[_make_item("a")])
        client = OnePassword(backend=backend)
        assert client.get_item("a").id == "a"

    def test_get_item_missing_raises(self):
        client = OnePassword(backend=InMemoryBackend())
        with pytest.raises(OpNotFoundError):
            client.get_item("ghost")

    def test_list_vaults(self):
        backend = InMemoryBackend(
            items=[
                _make_item("a", vault_id="v1"),
                _make_item("b", vault_id="v2"),
            ],
        )
        client = OnePassword(backend=backend)
        result = client.list_vaults()
        assert {v.id for v in result} == {"v1", "v2"}
        assert all(isinstance(v, VaultSummary) for v in result)

    def test_list_vaults_empty(self):
        client = OnePassword(backend=InMemoryBackend())
        assert client.list_vaults() == []


# ---------- AsyncOnePassword ----------


class TestAsyncOnePasswordInit:
    def test_accepts_backend(self):
        fake = AsyncInMemoryBackend()
        client = AsyncOnePassword(backend=fake)
        assert client.backend is fake


class TestAsyncOnePasswordRead:
    async def test_hit_returns_value(self):
        client = AsyncOnePassword(backend=AsyncInMemoryBackend(refs={"op://v/i/f": "secret"}))
        assert await client.read("op://v/i/f") == "secret"

    async def test_miss_returns_none(self):
        client = AsyncOnePassword(backend=AsyncInMemoryBackend())
        assert await client.read("op://v/i/missing") is None

    async def test_accepts_opref(self):
        client = AsyncOnePassword(backend=AsyncInMemoryBackend(refs={"op://v/i/f": "secret"}))
        assert await client.read(OpRef.parse("op://v/i/f")) == "secret"


class TestAsyncOnePasswordResolve:
    async def test_reference_chain_returns_first_hit(self):
        backend = AsyncInMemoryBackend(refs={"op://v/i/b": "second"})
        client = AsyncOnePassword(backend=backend)
        field = FieldValue.from_raw("op://v/i/a||op://v/i/b", "password")
        assert await client.resolve(field) == "second"

    async def test_literal_fallback(self):
        client = AsyncOnePassword(backend=AsyncInMemoryBackend())
        field = FieldValue.from_raw("op://v/i/missing||literal", "api_key")
        assert await client.resolve(field) == "literal"

    async def test_all_missing_returns_none(self):
        client = AsyncOnePassword(backend=AsyncInMemoryBackend())
        field = FieldValue.from_raw("op://v/i/x||op://v/i/y", "api_key")
        assert await client.resolve(field) is None


class TestAsyncOnePasswordPassthrough:
    async def test_list_items(self):
        backend = AsyncInMemoryBackend(items=[_make_item("a")])
        client = AsyncOnePassword(backend=backend)
        out = await client.list_items()
        assert [s.id for s in out] == ["a"]

    async def test_get_item(self):
        backend = AsyncInMemoryBackend(items=[_make_item("a")])
        client = AsyncOnePassword(backend=backend)
        assert (await client.get_item("a")).id == "a"

    async def test_get_item_missing_raises(self):
        client = AsyncOnePassword(backend=AsyncInMemoryBackend())
        with pytest.raises(OpNotFoundError):
            await client.get_item("ghost")

    async def test_list_vaults(self):
        backend = AsyncInMemoryBackend(
            items=[
                _make_item("a", vault_id="v1"),
                _make_item("b", vault_id="v2"),
            ],
        )
        client = AsyncOnePassword(backend=backend)
        result = await client.list_vaults()
        assert {v.id for v in result} == {"v1", "v2"}
        assert all(isinstance(v, VaultSummary) for v in result)

    async def test_list_vaults_empty(self):
        client = AsyncOnePassword(backend=AsyncInMemoryBackend())
        assert await client.list_vaults() == []


# ---------- facade online= propagation ----------


class TestOnePasswordOnline:
    def test_read_online_false_local_hit(self):
        client = OnePassword(backend=InMemoryBackend(refs={"op://v/i/f": "secret"}))
        assert client.read("op://v/i/f", online=False) == "secret"

    def test_read_online_false_miss_raises_offline(self):
        client = OnePassword(backend=InMemoryBackend())
        with pytest.raises(OpOfflineError):
            client.read("op://v/i/missing", online=False)

    def test_read_online_true_miss_returns_none(self):
        client = OnePassword(backend=InMemoryBackend())
        assert client.read("op://v/i/missing") is None

    def test_resolve_online_false_uses_local_only(self):
        backend = InMemoryBackend(refs={"op://v/i/a": "found"})
        client = OnePassword(backend=backend)
        field = FieldValue.from_raw("op://v/i/a||op://v/i/b", "password")
        assert client.resolve(field, online=False) == "found"

    def test_resolve_online_false_propagates_offline_error(self):
        # First segment misses → OpOfflineError propagates, walk terminates
        # (does NOT try the second segment, because caller forbade network).
        client = OnePassword(backend=InMemoryBackend())
        field = FieldValue.from_raw("op://v/i/a||op://v/i/b", "password")
        with pytest.raises(OpOfflineError):
            client.resolve(field, online=False)


class TestAsyncOnePasswordOnline:
    async def test_read_online_false_local_hit(self):
        client = AsyncOnePassword(backend=AsyncInMemoryBackend(refs={"op://v/i/f": "secret"}))
        assert await client.read("op://v/i/f", online=False) == "secret"

    async def test_read_online_false_miss_raises_offline(self):
        client = AsyncOnePassword(backend=AsyncInMemoryBackend())
        with pytest.raises(OpOfflineError):
            await client.read("op://v/i/missing", online=False)

    async def test_resolve_online_false_uses_local_only(self):
        backend = AsyncInMemoryBackend(refs={"op://v/i/a": "found"})
        client = AsyncOnePassword(backend=backend)
        field = FieldValue.from_raw("op://v/i/a||op://v/i/b", "password")
        assert await client.resolve(field, online=False) == "found"

    async def test_resolve_online_false_propagates_offline_error(self):
        client = AsyncOnePassword(backend=AsyncInMemoryBackend())
        field = FieldValue.from_raw("op://v/i/a||op://v/i/b", "password")
        with pytest.raises(OpOfflineError):
            await client.resolve(field, online=False)
