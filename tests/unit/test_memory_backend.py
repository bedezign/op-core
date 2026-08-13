"""Tests for InMemoryBackend / AsyncInMemoryBackend."""

from __future__ import annotations

import pytest

from op_core.backends.memory import AsyncInMemoryBackend, InMemoryBackend
from op_core.exceptions import OpNotFoundError, OpOfflineError
from op_core.items import Item, ItemField, ItemSection, ItemSummary, ItemURL, VaultSummary


def _make_item(
    *,
    item_id: str = "itm1",
    title: str = "T",
    vault_id: str = "v1",
    vault_name: str = "Personal",
    category: str = "LOGIN",
    tags: tuple[str, ...] = (),
    sections: tuple[ItemSection, ...] | None = None,
    fields: tuple[ItemField, ...] | None = None,
) -> Item:
    return Item(
        id=item_id,
        title=title,
        vault_id=vault_id,
        vault_name=vault_name,
        category=category,
        tags=tags,
        sections=sections if sections is not None else (ItemSection(id="s1", label="S"),),
        fields=fields
        if fields is not None
        else (
            ItemField(id="f1", label="username", value="u", type="STRING", section_id=None),
            ItemField(id="f2", label="password", value="p", type="CONCEALED", section_id="s1"),
        ),
    )


# ---------- construction ----------


class TestConstruction:
    def test_empty_fake(self):
        backend = InMemoryBackend()
        assert backend.list_items() == []

    def test_with_refs(self):
        backend = InMemoryBackend(refs={"op://v/i/f": "secret"})
        assert backend.read("op://v/i/f") == "secret"

    def test_with_items(self):
        item = _make_item()
        backend = InMemoryBackend(items=[item])
        assert len(backend.list_items()) == 1


# ---------- read ----------


class TestRead:
    def test_exact_match(self):
        backend = InMemoryBackend(refs={"op://v/i/f": "secret"})
        assert backend.read("op://v/i/f") == "secret"

    def test_case_sensitive(self):
        backend = InMemoryBackend(refs={"op://v/i/Field": "secret"})
        with pytest.raises(OpNotFoundError):
            backend.read("op://v/i/field")

    def test_missing_raises(self):
        backend = InMemoryBackend(refs={"op://v/i/f": "secret"})
        with pytest.raises(OpNotFoundError, match="op://v/i/missing"):
            backend.read("op://v/i/missing")

    def test_missing_with_default_returns_default(self):
        backend = InMemoryBackend()
        assert backend.read("op://v/i/missing", default_value="fallback") == "fallback"

    def test_missing_with_empty_default(self):
        backend = InMemoryBackend()
        assert backend.read("op://v/i/missing", default_value="") == ""

    def test_default_value_none_still_raises(self):
        backend = InMemoryBackend()
        with pytest.raises(OpNotFoundError):
            backend.read("op://v/i/missing", default_value=None)


# ---------- list_items ----------


class TestListItems:
    def test_no_filter_returns_all(self):
        items = [_make_item(item_id="a"), _make_item(item_id="b")]
        backend = InMemoryBackend(items=items)
        result = backend.list_items()
        assert {s.id for s in result} == {"a", "b"}
        assert all(isinstance(s, ItemSummary) for s in result)

    def test_vault_filter_by_id(self):
        items = [
            _make_item(item_id="a", vault_id="v1", vault_name="V One"),
            _make_item(item_id="b", vault_id="v2", vault_name="V Two"),
        ]
        backend = InMemoryBackend(items=items)
        result = backend.list_items(vault="v1")
        assert {s.id for s in result} == {"a"}

    def test_vault_filter_by_name(self):
        items = [
            _make_item(item_id="a", vault_id="v1", vault_name="V One"),
            _make_item(item_id="b", vault_id="v2", vault_name="V Two"),
        ]
        backend = InMemoryBackend(items=items)
        result = backend.list_items(vault="V Two")
        assert {s.id for s in result} == {"b"}

    def test_tags_filter_intersect(self):
        items = [
            _make_item(item_id="a", tags=("dev", "ssh")),
            _make_item(item_id="b", tags=("prod",)),
            _make_item(item_id="c", tags=("dev",)),
        ]
        backend = InMemoryBackend(items=items)
        result = backend.list_items(tags=["dev"])
        assert {s.id for s in result} == {"a", "c"}

    def test_categories_filter(self):
        items = [
            _make_item(item_id="a", category="LOGIN"),
            _make_item(item_id="b", category="SSH_KEY"),
            _make_item(item_id="c", category="SECURE_NOTE"),
        ]
        backend = InMemoryBackend(items=items)
        result = backend.list_items(categories=["LOGIN", "SSH_KEY"])
        assert {s.id for s in result} == {"a", "b"}

    def test_combined_filters(self):
        items = [
            _make_item(item_id="a", vault_id="v1", category="LOGIN", tags=("dev",)),
            _make_item(item_id="b", vault_id="v1", category="SSH_KEY", tags=("dev",)),
            _make_item(item_id="c", vault_id="v2", category="LOGIN", tags=("dev",)),
        ]
        backend = InMemoryBackend(items=items)
        result = backend.list_items(vault="v1", categories=["LOGIN"], tags=["dev"])
        assert {s.id for s in result} == {"a"}

    def test_empty_tags_rejected(self):
        with pytest.raises(ValueError, match="tags"):
            InMemoryBackend().list_items(tags=[])

    def test_tag_with_comma_rejected(self):
        with pytest.raises(ValueError, match="comma"):
            InMemoryBackend().list_items(tags=["infra,prod"])

    def test_empty_categories_rejected(self):
        with pytest.raises(ValueError, match="categories"):
            InMemoryBackend().list_items(categories=[])

    def test_category_with_comma_rejected(self):
        with pytest.raises(ValueError, match="comma"):
            InMemoryBackend().list_items(categories=["LOGIN,NOTE"])


# ---------- get_item ----------


class TestGetItem:
    def _backend(self) -> InMemoryBackend:
        return InMemoryBackend(
            items=[
                _make_item(item_id="a", vault_id="v1"),
                _make_item(item_id="a", vault_id="v2"),
                _make_item(item_id="b", vault_id="v1"),
            ]
        )

    def test_by_string_id_no_vault(self):
        # Returns the first match when vault is unspecified.
        item = self._backend().get_item("a")
        assert item.id == "a"

    def test_by_string_id_with_vault(self):
        item = self._backend().get_item("a", vault="v2")
        assert item.id == "a"
        assert item.vault_id == "v2"

    def test_by_summary_uses_its_vault(self):
        summary = ItemSummary(
            id="a",
            title="T",
            vault_id="v2",
            vault_name="P",
            category="LOGIN",
            tags=(),
        )
        item = self._backend().get_item(summary)
        assert item.vault_id == "v2"

    def test_explicit_vault_overrides_summary(self):
        summary = ItemSummary(
            id="a",
            title="T",
            vault_id="v2",
            vault_name="P",
            category="LOGIN",
            tags=(),
        )
        item = self._backend().get_item(summary, vault="v1")
        assert item.vault_id == "v1"

    def test_by_item_instance(self):
        existing = _make_item(item_id="b", vault_id="v1")
        item = self._backend().get_item(existing)
        assert item.id == "b"

    def test_missing_id_raises(self):
        with pytest.raises(OpNotFoundError, match="nope"):
            self._backend().get_item("nope")

    def test_vault_mismatch_raises(self):
        with pytest.raises(OpNotFoundError):
            self._backend().get_item("b", vault="v2")


# ---------- list_vaults ----------


class TestListVaults:
    def test_empty_returns_empty(self):
        backend = InMemoryBackend()
        assert backend.list_vaults() == []

    def test_single_vault_from_items(self):
        items = [_make_item(item_id="a"), _make_item(item_id="b")]
        backend = InMemoryBackend(items=items)
        result = backend.list_vaults()
        assert result == [VaultSummary(id="v1", name="Personal")]

    def test_distinct_vaults_deduped(self):
        items = [
            _make_item(item_id="a", vault_id="v1", vault_name="Personal"),
            _make_item(item_id="b", vault_id="v2", vault_name="Shared"),
            _make_item(item_id="c", vault_id="v1", vault_name="Personal"),
        ]
        backend = InMemoryBackend(items=items)
        result = backend.list_vaults()
        assert result == [
            VaultSummary(id="v1", name="Personal"),
            VaultSummary(id="v2", name="Shared"),
        ]

    def test_preserves_first_seen_order(self):
        items = [
            _make_item(item_id="a", vault_id="v2", vault_name="Shared"),
            _make_item(item_id="b", vault_id="v1", vault_name="Personal"),
            _make_item(item_id="c", vault_id="v3", vault_name="Work"),
        ]
        backend = InMemoryBackend(items=items)
        assert [v.id for v in backend.list_vaults()] == ["v2", "v1", "v3"]

    def test_first_seen_name_wins_on_collision(self):
        # Two items share vault_id but disagree on vault_name — first-seen wins
        # silently. In practice items from the same vault carry the same name;
        # this guards the dedup contract rather than a real-world scenario.
        items = [
            _make_item(item_id="a", vault_id="v1", vault_name="Personal"),
            _make_item(item_id="b", vault_id="v1", vault_name="Personal Renamed"),
        ]
        backend = InMemoryBackend(items=items)
        assert backend.list_vaults() == [VaultSummary(id="v1", name="Personal")]

    def test_returns_vault_summary_instances(self):
        backend = InMemoryBackend(items=[_make_item()])
        assert all(isinstance(v, VaultSummary) for v in backend.list_vaults())


# ---------- async wrapper ----------


class TestAsyncInMemoryBackend:
    async def test_read_delegates(self):
        backend = AsyncInMemoryBackend(refs={"op://v/i/f": "secret"})
        assert await backend.read("op://v/i/f") == "secret"

    async def test_read_default_value(self):
        backend = AsyncInMemoryBackend()
        assert await backend.read("op://v/i/missing", default_value="x") == "x"

    async def test_list_items(self):
        backend = AsyncInMemoryBackend(items=[_make_item(item_id="a")])
        result = await backend.list_items()
        assert [s.id for s in result] == ["a"]

    async def test_get_item(self):
        backend = AsyncInMemoryBackend(items=[_make_item(item_id="a")])
        item = await backend.get_item("a")
        assert item.id == "a"

    async def test_list_items_validation(self):
        with pytest.raises(ValueError, match="tags"):
            await AsyncInMemoryBackend().list_items(tags=[])

    async def test_list_vaults_empty(self):
        assert await AsyncInMemoryBackend().list_vaults() == []

    async def test_list_vaults_dedupes(self):
        items = [
            _make_item(item_id="a", vault_id="v1", vault_name="Personal"),
            _make_item(item_id="b", vault_id="v2", vault_name="Shared"),
            _make_item(item_id="c", vault_id="v1", vault_name="Personal"),
        ]
        backend = AsyncInMemoryBackend(items=items)
        result = await backend.list_vaults()
        assert result == [
            VaultSummary(id="v1", name="Personal"),
            VaultSummary(id="v2", name="Shared"),
        ]


# ---------- item auto-indexing ----------


class TestItemAutoIndex:
    def test_top_level_field_readable_by_label(self):
        backend = InMemoryBackend(items=[_make_item()])
        assert backend.read("op://v1/itm1/username") == "u"

    def test_top_level_field_readable_by_id(self):
        backend = InMemoryBackend(items=[_make_item()])
        assert backend.read("op://v1/itm1/f1") == "u"

    def test_sectioned_field_bare_label_not_indexed(self):
        # A bare label unambiguously names a top-level field; a sectioned
        # field's label is only addressable qualified by its section.
        backend = InMemoryBackend(items=[_make_item()])
        with pytest.raises(OpNotFoundError):
            backend.read("op://v1/itm1/password")

    def test_sectioned_field_bare_label_falls_through_to_fallback(self):
        fallback = _RecordingBackend(refs={"op://v1/itm1/password": "from-fallback"})
        backend = InMemoryBackend(items=[_make_item()], fallback=fallback)
        assert backend.read("op://v1/itm1/password") == "from-fallback"
        assert fallback.calls == [("op://v1/itm1/password", True)]

    def test_sectioned_field_readable_by_id(self):
        backend = InMemoryBackend(items=[_make_item()])
        assert backend.read("op://v1/itm1/f2") == "p"

    def test_sectioned_field_readable_by_all_form_combinations(self):
        backend = InMemoryBackend(items=[_make_item()])
        assert backend.read("op://v1/itm1/S/password") == "p"
        assert backend.read("op://v1/itm1/S/f2") == "p"
        assert backend.read("op://v1/itm1/s1/password") == "p"
        assert backend.read("op://v1/itm1/s1/f2") == "p"

    def test_unknown_section_id_addressable_by_raw_id(self):
        item = _make_item(
            sections=(ItemSection(id="s1", label="S"),),
            fields=(ItemField(id="f9", label="ghost-field", value="v", type="STRING", section_id="ghost"),),
        )
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/ghost/ghost-field") == "v"
        assert backend.read("op://v1/itm1/ghost/f9") == "v"
        assert backend.read("op://v1/itm1/f9") == "v"

    def test_section_label_equals_id_no_collision(self):
        item = _make_item(
            sections=(ItemSection(id="sec", label="sec"),),
            fields=(ItemField(id="f1", label="pw", value="p", type="CONCEALED", section_id="sec"),),
        )
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/sec/pw") == "p"

    def test_section_label_with_slash_not_addressable_by_label(self):
        item = _make_item(
            sections=(ItemSection(id="sX", label="Router / admin"),),
            fields=(ItemField(id="f9", label="user", value="u", type="STRING", section_id="sX"),),
        )
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/sX/user") == "u"
        assert backend.read("op://v1/itm1/f9") == "u"
        with pytest.raises(OpNotFoundError):
            backend.read("op://v1/itm1/Router / admin/user")

    def test_top_level_field_label_with_slash_has_no_label_key(self):
        item = _make_item(fields=(ItemField(id="idab", label="a/b", value="v", type="STRING", section_id=None),))
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/idab") == "v"
        with pytest.raises(OpNotFoundError):
            backend.read("op://v1/itm1/a/b")

    def test_top_level_and_sectioned_fields_share_label_both_addressable(self):
        # Motivating scenario: a top-level field and a sectioned field share a
        # label, plus a third field with that same label whose value is a
        # self-reference. All three coexist without colliding because the
        # reference is never indexed and the sectioned field never claims the
        # bare label.
        item = _make_item(
            sections=(ItemSection(id="s1", label="Section"),),
            fields=(
                ItemField(id="top", label="username", value="pi", type="STRING", section_id=None),
                ItemField(id="sec", label="username", value="admin", type="STRING", section_id="s1"),
                ItemField(id="ref", label="username", value="op://././username", type="STRING", section_id="s1"),
            ),
        )
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/username") == "pi"
        assert backend.read("op://v1/itm1/Section/username") == "admin"
        with pytest.raises(OpNotFoundError):
            backend.read("op://v1/itm1/ref")

    def test_empty_items_list_produces_empty_index(self):
        backend = InMemoryBackend(items=[])
        with pytest.raises(OpNotFoundError):
            backend.read("op://v1/itm1/anything")

    def test_item_with_zero_fields_produces_no_keys(self):
        item = _make_item(fields=())
        backend = InMemoryBackend(items=[item])
        with pytest.raises(OpNotFoundError):
            backend.read("op://v1/itm1/username")

    def test_empty_string_label_indexed_without_crash(self):
        item = _make_item(fields=(ItemField(id="idx", label="", value="v", type="STRING", section_id=None),))
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/idx") == "v"
        assert backend.read("op://v1/itm1/") == "v"

    def test_none_value_field_skipped(self):
        item = _make_item(fields=(ItemField(id="f1", label="empty", value=None, type="STRING", section_id=None),))
        backend = InMemoryBackend(items=[item])
        with pytest.raises(OpNotFoundError):
            backend.read("op://v1/itm1/empty")

    def test_refs_override_item_index_on_collision(self):
        backend = InMemoryBackend(
            items=[_make_item()],
            refs={"op://v1/itm1/username": "override"},
        )
        assert backend.read("op://v1/itm1/username") == "override"

    def test_miss_falls_through_to_fallback(self):
        fallback = _RecordingBackend(refs={"op://v1/itm1/other": "upstream"})
        backend = InMemoryBackend(items=[_make_item()], fallback=fallback)
        # Indexed field served locally.
        assert backend.read("op://v1/itm1/username") == "u"
        assert fallback.calls == []
        # Unknown field delegates.
        assert backend.read("op://v1/itm1/other") == "upstream"
        assert fallback.calls == [("op://v1/itm1/other", True)]

    def test_offline_indexed_field_still_served(self):
        backend = InMemoryBackend(items=[_make_item()])
        assert backend.read("op://v1/itm1/username", online=False) == "u"

    def test_label_equals_id_single_entry_no_error(self):
        item = _make_item(fields=(ItemField(id="host", label="host", value="h", type="STRING", section_id=None),))
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/host") == "h"

    def test_sensitive_fields_included(self):
        # Sensitivity is a storage marker (ops://), not an access gate. If the
        # consumer handed us the Item, the value is already in memory.
        item = _make_item(fields=(ItemField(id="f1", label="password", value="p", type="CONCEALED", section_id=None),))
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/password") == "p"

    def test_unicode_field_label(self):
        item = _make_item(
            fields=(
                ItemField(id="f1", label="contraseña", value="hunter2", type="CONCEALED", section_id=None),
                ItemField(id="f2", label="🔑 token", value="abc", type="STRING", section_id=None),
            )
        )
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/contraseña") == "hunter2"
        assert backend.read("op://v1/itm1/🔑 token") == "abc"

    def test_duplicate_top_level_label_raises_value_error(self):
        # When two literal-valued fields on the same item would resolve to
        # the same key, index construction raises rather than picking a
        # winner silently.
        item = _make_item(
            fields=(
                ItemField(id="f1", label="username", value="first", type="STRING", section_id=None),
                ItemField(id="f2", label="username", value="second", type="STRING", section_id=None),
            )
        )
        with pytest.raises(ValueError) as exc_info:
            InMemoryBackend(items=[item])
        message = str(exc_info.value)
        assert "op://v1/itm1/username" in message
        assert "f1" in message
        assert "f2" in message

    def test_duplicate_label_in_same_section_raises_value_error(self):
        item = _make_item(
            fields=(
                ItemField(id="f1", label="dup", value="first", type="STRING", section_id="s1"),
                ItemField(id="f2", label="dup", value="second", type="STRING", section_id="s1"),
            )
        )
        with pytest.raises(ValueError) as exc_info:
            InMemoryBackend(items=[item])
        message = str(exc_info.value)
        assert "f1" in message
        assert "f2" in message

    def test_field_id_equal_to_other_field_label_label_wins(self):
        # "beta" is field alpha's label-derived key AND field beta's
        # id-derived key. A label-derived key and an id-derived key from
        # different fields coinciding is not an error; the label-derived
        # form wins.
        item = _make_item(
            fields=(
                ItemField(id="alpha", label="beta", value="first", type="STRING", section_id=None),
                ItemField(id="beta", label="gamma", value="second", type="STRING", section_id=None),
            )
        )
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/beta") == "first"
        # field beta remains reachable via its own id and label keys.
        assert backend.read("op://v1/itm1/gamma") == "second"

    def test_cross_kind_collision_in_section_label_wins(self):
        item = _make_item(
            sections=(ItemSection(id="s1", label="S"),),
            fields=(
                ItemField(id="dup", label="alpha", value="from-id", type="STRING", section_id="s1"),
                ItemField(id="other", label="dup", value="from-label", type="STRING", section_id="s1"),
            ),
        )
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/S/dup") == "from-label"
        assert backend.read("op://v1/itm1/s1/dup") == "from-label"
        # The shadowed field's other forms stay reachable.
        assert backend.read("op://v1/itm1/S/alpha") == "from-id"
        assert backend.read("op://v1/itm1/dup") == "from-id"

    def test_builtin_url_id_shadowed_by_user_field_label_wins(self):
        # Real-world shape: a built-in URL field with fixed id "url" plus a
        # user-added custom field labelled "url" with a distinct id.
        item = _make_item(
            fields=(
                ItemField(id="url", label="URL", value="https://a.example", type="URL", section_id=None),
                ItemField(
                    id="4l3iq5fho44w4pj42mi543xwiq",
                    label="url",
                    value="https://b.example",
                    type="STRING",
                    section_id=None,
                ),
            )
        )
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/url") == "https://b.example"
        assert backend.read("op://v1/itm1/URL") == "https://a.example"
        assert backend.read("op://v1/itm1/4l3iq5fho44w4pj42mi543xwiq") == "https://b.example"

    def test_label_equals_id_field_shadowed_reachable_only_via_get_item(self):
        # field A's label equals its id, so its sole key is id-derived. field
        # B's label collides with that key. Label wins uniformly, so A's only
        # key is shadowed — A stops being addressable via read() but stays
        # reachable through the item itself.
        item = _make_item(
            fields=(
                ItemField(id="url", label="url", value="from-A", type="STRING", section_id=None),
                ItemField(id="user-added-id", label="url", value="from-B", type="STRING", section_id=None),
            )
        )
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/url") == "from-B"
        result_item = backend.get_item("itm1")
        assert any(f.id == "url" and f.value == "from-A" for f in result_item.fields)

    def test_reference_valued_field_not_indexed_falls_through_to_fallback(self):
        # A field whose value is itself a reference (e.g. a self-reference like
        # `op://././username` mirroring another field) must NOT be indexed as
        # a literal — doing so would make read() return the reference string
        # instead of the value it points at. Reference-valued fields must fall
        # through to the configured fallback.
        item = _make_item(
            fields=(
                ItemField(id="f1", label="hostname", value="op://Vault/Other/hostname", type="STRING", section_id=None),
            )
        )
        fallback = _RecordingBackend(refs={"op://v1/itm1/hostname": "from-fallback"})
        backend = InMemoryBackend(items=[item], fallback=fallback)
        assert backend.read("op://v1/itm1/hostname") == "from-fallback"
        assert fallback.calls == [("op://v1/itm1/hostname", True)]

    def test_chain_valued_field_not_indexed(self):
        # A `||` chain whose first segment is a reference starts with `op://`,
        # so the prefix check skips it — the whole chain falls through.
        item = _make_item(
            fields=(
                ItemField(
                    id="f1", label="token", value="op://Vault/Item/primary||literal", type="STRING", section_id=None
                ),
            )
        )
        fallback = _RecordingBackend(refs={"op://v1/itm1/token": "resolved"})
        backend = InMemoryBackend(items=[item], fallback=fallback)
        assert backend.read("op://v1/itm1/token") == "resolved"

    def test_url_value_indexed_as_literal(self):
        # `https://...` contains `://` but is NOT an op-core reference. The
        # prefix check (op://, ops://) must let URLs through to the index.
        item = _make_item(
            fields=(ItemField(id="f1", label="website", value="https://github.com", type="URL", section_id=None),)
        )
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/website") == "https://github.com"

    def test_ops_reference_not_indexed(self):
        # `ops://` (sensitive variant) is also a reference and must not be indexed.
        item = _make_item(
            fields=(
                ItemField(id="f1", label="api_key", value="ops://Vault/Item/secret", type="CONCEALED", section_id=None),
            )
        )
        fallback = _RecordingBackend(refs={"op://v1/itm1/api_key": "the-secret"})
        backend = InMemoryBackend(items=[item], fallback=fallback)
        assert backend.read("op://v1/itm1/api_key") == "the-secret"

    def test_reference_only_field_no_fallback_raises(self):
        # No fallback means the reference-valued field surfaces as a clean miss.
        item = _make_item(
            fields=(
                ItemField(id="f1", label="hostname", value="op://Vault/Other/hostname", type="STRING", section_id=None),
            )
        )
        backend = InMemoryBackend(items=[item])
        with pytest.raises(OpNotFoundError):
            backend.read("op://v1/itm1/hostname")

    def test_url_label_not_addressable_via_read(self):
        # Item.urls is exposed for inspection but is NOT a resolution target.
        # The 1Password CLI rejects `op read op://V/I/<url-label>` with a
        # "not a field" error — InMemoryBackend must match that contract so
        # callers do not get inconsistent results across backends. Without a
        # fallback, the read surfaces as a clean miss.
        item = Item(
            id="itm1",
            title="Homelab NAS",
            vault_id="v1",
            vault_name="Personal",
            category="LOGIN",
            tags=(),
            sections=(),
            fields=(ItemField(id="f1", label="username", value="admin", type="STRING", section_id=None),),
            urls=(ItemURL(href="nas.example.com", label="website", primary=True),),
        )
        backend = InMemoryBackend(items=[item])
        # Field is addressable as usual.
        assert backend.read("op://v1/itm1/username") == "admin"
        # URL label is NOT addressable — this is the contract that justifies
        # exposing Item.urls on the model in the first place.
        with pytest.raises(OpNotFoundError):
            backend.read("op://v1/itm1/website")

    def test_duplicate_label_with_reference_falls_through_to_literal(self):
        # Real-world shape: a top-level literal field "username"='root' plus a
        # section field with the same label whose value is a self-reference.
        # The literal must remain addressable; the reference must not overwrite
        # it during indexing.
        item = _make_item(
            fields=(
                ItemField(id="username", label="username", value="root", type="STRING", section_id=None),
                ItemField(
                    id="kinyxfl75abc", label="username", value="op://././username", type="STRING", section_id="s1"
                ),
            )
        )
        backend = InMemoryBackend(items=[item])
        assert backend.read("op://v1/itm1/username") == "root"
        # The reference-valued field's id is also not indexed — read falls
        # through and (no fallback set) raises NotFound.
        with pytest.raises(OpNotFoundError):
            backend.read("op://v1/itm1/kinyxfl75abc")


class TestAsyncItemAutoIndex:
    async def test_label_hit(self):
        backend = AsyncInMemoryBackend(items=[_make_item()])
        assert await backend.read("op://v1/itm1/username") == "u"

    async def test_id_hit(self):
        backend = AsyncInMemoryBackend(items=[_make_item()])
        assert await backend.read("op://v1/itm1/f2") == "p"

    async def test_section_qualified_read(self):
        backend = AsyncInMemoryBackend(items=[_make_item()])
        assert await backend.read("op://v1/itm1/S/password") == "p"

    async def test_refs_override(self):
        backend = AsyncInMemoryBackend(
            items=[_make_item()],
            refs={"op://v1/itm1/username": "override"},
        )
        assert await backend.read("op://v1/itm1/username") == "override"

    async def test_reference_valued_field_not_indexed(self):
        item = _make_item(
            fields=(
                ItemField(id="f1", label="hostname", value="op://Vault/Other/hostname", type="STRING", section_id=None),
            )
        )
        backend = AsyncInMemoryBackend(items=[item])
        with pytest.raises(OpNotFoundError):
            await backend.read("op://v1/itm1/hostname")

    async def test_builtin_url_id_shadowed_by_user_field_label_wins(self):
        item = _make_item(
            fields=(
                ItemField(id="url", label="URL", value="https://a.example", type="URL", section_id=None),
                ItemField(
                    id="4l3iq5fho44w4pj42mi543xwiq",
                    label="url",
                    value="https://b.example",
                    type="STRING",
                    section_id=None,
                ),
            )
        )
        backend = AsyncInMemoryBackend(items=[item])
        assert await backend.read("op://v1/itm1/url") == "https://b.example"
        assert await backend.read("op://v1/itm1/URL") == "https://a.example"


# ---------- fallback ----------


class _RecordingBackend:
    """Minimal Backend stub that records read() calls."""

    def __init__(self, refs: dict[str, str] | None = None) -> None:
        self._refs = refs or {}
        self.calls: list[tuple[str, bool]] = []

    def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
        self.calls.append((reference, online))
        if not online and reference not in self._refs:
            raise OpOfflineError(f"cannot satisfy {reference} offline")
        if reference in self._refs:
            return self._refs[reference]
        if default_value is not None:
            return default_value
        raise OpNotFoundError(reference)

    def list_items(self, **kwargs):
        return []

    def list_vaults(self):
        return []

    def get_item(self, item, *, vault=None):
        raise OpNotFoundError(item)


class TestFallback:
    def test_local_hit_does_not_delegate(self):
        fallback = _RecordingBackend(refs={"op://v/i/f": "upstream"})
        backend = InMemoryBackend(refs={"op://v/i/f": "local"}, fallback=fallback)
        assert backend.read("op://v/i/f") == "local"
        assert fallback.calls == []

    def test_miss_delegates_to_fallback(self):
        fallback = _RecordingBackend(refs={"op://v/i/f": "upstream"})
        backend = InMemoryBackend(fallback=fallback)
        assert backend.read("op://v/i/f") == "upstream"
        assert fallback.calls == [("op://v/i/f", True)]

    def test_miss_no_fallback_raises_not_found(self):
        backend = InMemoryBackend()
        with pytest.raises(OpNotFoundError):
            backend.read("op://v/i/missing")

    def test_miss_no_fallback_default_value(self):
        backend = InMemoryBackend()
        assert backend.read("op://v/i/missing", default_value="x") == "x"

    def test_miss_fallback_raises_default_applied(self):
        fallback = _RecordingBackend()
        backend = InMemoryBackend(fallback=fallback)
        assert backend.read("op://v/i/missing", default_value="x") == "x"

    def test_online_false_propagates_to_fallback(self):
        fallback = _RecordingBackend(refs={"op://v/i/f": "upstream"})
        backend = InMemoryBackend(fallback=fallback)
        assert backend.read("op://v/i/f", online=False) == "upstream"
        assert fallback.calls == [("op://v/i/f", False)]

    def test_online_false_no_fallback_raises_offline(self):
        backend = InMemoryBackend()
        with pytest.raises(OpOfflineError):
            backend.read("op://v/i/missing", online=False)

    def test_online_false_local_hit_succeeds(self):
        backend = InMemoryBackend(refs={"op://v/i/f": "local"})
        assert backend.read("op://v/i/f", online=False) == "local"

    def test_fallback_offline_error_propagates(self):
        fallback = _RecordingBackend()
        backend = InMemoryBackend(fallback=fallback)
        with pytest.raises(OpOfflineError):
            backend.read("op://v/i/f", online=False)


class TestAsyncFallback:
    class _AsyncRecording:
        def __init__(self, refs=None):
            self._refs = refs or {}
            self.calls = []

        async def read(self, reference, *, default_value=None, online=True):
            self.calls.append((reference, online))
            if not online and reference not in self._refs:
                raise OpOfflineError(reference)
            if reference in self._refs:
                return self._refs[reference]
            if default_value is not None:
                return default_value
            raise OpNotFoundError(reference)

        async def list_items(self, **kwargs):
            return []

        async def list_vaults(self):
            return []

        async def get_item(self, item, *, vault=None):
            raise OpNotFoundError(item)

    async def test_local_hit_no_delegation(self):
        fb = self._AsyncRecording(refs={"op://v/i/f": "upstream"})
        b = AsyncInMemoryBackend(refs={"op://v/i/f": "local"}, fallback=fb)
        assert await b.read("op://v/i/f") == "local"
        assert fb.calls == []

    async def test_miss_delegates(self):
        fb = self._AsyncRecording(refs={"op://v/i/f": "upstream"})
        b = AsyncInMemoryBackend(fallback=fb)
        assert await b.read("op://v/i/f") == "upstream"
        assert fb.calls == [("op://v/i/f", True)]

    async def test_online_false_propagates(self):
        fb = self._AsyncRecording()
        b = AsyncInMemoryBackend(fallback=fb)
        with pytest.raises(OpOfflineError):
            await b.read("op://v/i/f", online=False)
        assert fb.calls == [("op://v/i/f", False)]
