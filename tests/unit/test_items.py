"""Tests for op_core.items."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from op_core.items import Item, ItemField, ItemRef, ItemSection, ItemSummary, ItemURL, VaultSummary


class TestItemSection:
    def test_construction(self):
        section = ItemSection(id="sec1", label="Credentials")
        assert section.id == "sec1"
        assert section.label == "Credentials"

    def test_frozen(self):
        section = ItemSection(id="sec1", label="Credentials")
        with pytest.raises(FrozenInstanceError):
            section.label = "changed"  # type: ignore[misc]

    def test_equality(self):
        a = ItemSection(id="sec1", label="Credentials")
        b = ItemSection(id="sec1", label="Credentials")
        assert a == b


class TestItemField:
    def test_construction_with_all_fields(self):
        field = ItemField(
            id="f1",
            label="password",
            value="hunter2",
            type="CONCEALED",
            section_id="sec1",
        )
        assert field.id == "f1"
        assert field.label == "password"
        assert field.value == "hunter2"
        assert field.type == "CONCEALED"
        assert field.section_id == "sec1"

    def test_construction_with_null_value(self):
        field = ItemField(id="f1", label="notes", value=None, type="STRING", section_id=None)
        assert field.value is None
        assert field.section_id is None

    def test_frozen(self):
        field = ItemField(id="f1", label="pw", value="x", type="CONCEALED", section_id=None)
        with pytest.raises(FrozenInstanceError):
            field.value = "changed"  # type: ignore[misc]


class TestItemURL:
    def test_construction_with_all_fields(self):
        url = ItemURL(href="https://github.com", label="website", primary=True)
        assert url.href == "https://github.com"
        assert url.label == "website"
        assert url.primary is True

    def test_label_and_primary_default(self):
        """Only href is required. Label defaults to '"website"' (1Password's UI
        convention for an unlabeled URL); primary defaults to False."""
        url = ItemURL(href="https://example.com")
        assert url.label == "website"
        assert url.primary is False

    def test_frozen(self):
        url = ItemURL(href="https://example.com")
        with pytest.raises(FrozenInstanceError):
            url.href = "https://changed.example.com"  # type: ignore[misc]

    def test_equality(self):
        a = ItemSection(id="x", label="y")  # sanity: different types unequal
        b = ItemURL(href="https://example.com", label="website", primary=True)
        c = ItemURL(href="https://example.com", label="website", primary=True)
        assert b == c
        assert a != b

    def test_hashable(self):
        a = ItemURL(href="https://example.com", label="website", primary=True)
        b = ItemURL(href="https://example.com", label="website", primary=True)
        assert hash(a) == hash(b)


class TestItem:
    def _make_item(self, **overrides) -> Item:
        defaults = {
            "id": "itm1",
            "title": "GitHub",
            "vault_id": "v1",
            "vault_name": "Personal",
            "category": "LOGIN",
            "tags": ("dev", "ssh"),
            "sections": (ItemSection(id="sec1", label="Credentials"),),
            "fields": (
                ItemField(id="f1", label="username", value="alice", type="STRING", section_id=None),
                ItemField(id="f2", label="password", value="hunter2", type="CONCEALED", section_id="sec1"),
            ),
        }
        return Item(**(defaults | overrides))

    def test_construction(self):
        item = self._make_item()
        assert item.id == "itm1"
        assert item.title == "GitHub"
        assert item.vault_id == "v1"
        assert item.vault_name == "Personal"
        assert item.category == "LOGIN"

    def test_tags_are_tuple(self):
        item = self._make_item()
        assert isinstance(item.tags, tuple)
        assert item.tags == ("dev", "ssh")

    def test_fields_are_tuple_of_item_fields(self):
        item = self._make_item()
        assert isinstance(item.fields, tuple)
        assert all(isinstance(f, ItemField) for f in item.fields)
        assert len(item.fields) == 2

    def test_sections_are_tuple_of_item_sections(self):
        item = self._make_item()
        assert isinstance(item.sections, tuple)
        assert all(isinstance(s, ItemSection) for s in item.sections)

    def test_empty_collections(self):
        item = self._make_item(tags=(), sections=(), fields=())
        assert item.tags == ()
        assert item.sections == ()
        assert item.fields == ()

    def test_urls_default_empty_for_backward_compat(self):
        """Existing Item(...) callers that pre-date the urls field keep working."""
        item = self._make_item()
        assert item.urls == ()
        assert isinstance(item.urls, tuple)

    def test_urls_populated(self):
        item = self._make_item(
            urls=(
                ItemURL(href="https://github.com", label="website", primary=True),
                ItemURL(href="https://api.github.com", label="api", primary=False),
            )
        )
        assert len(item.urls) == 2
        assert all(isinstance(u, ItemURL) for u in item.urls)
        assert item.urls[0].label == "website"

    def test_frozen(self):
        item = self._make_item()
        with pytest.raises(FrozenInstanceError):
            item.title = "changed"  # type: ignore[misc]

    def test_equality(self):
        a = self._make_item()
        b = self._make_item()
        assert a == b

    def test_hashable(self):
        """Frozen dataclass with tuple collections must be hashable."""
        item = self._make_item()
        assert hash(item) == hash(self._make_item())


class TestItemFieldLookup:
    def _make_item(self) -> Item:
        return Item(
            id="itm1",
            title="GitHub",
            vault_id="v1",
            vault_name="Personal",
            category="LOGIN",
            tags=(),
            sections=(
                ItemSection(id="sec1", label="Credentials"),
                ItemSection(id="sec2", label="Recovery"),
            ),
            fields=(
                ItemField(id="f1", label="username", value="alice", type="STRING", section_id=None),
                ItemField(id="f2", label="password", value="hunter2", type="CONCEALED", section_id="sec1"),
                ItemField(id="f3", label="notes", value=None, type="STRING", section_id=None),
                ItemField(id="f4", label="backup_code", value="abc123", type="CONCEALED", section_id="sec2"),
            ),
        )

    def test_field_found(self):
        item = self._make_item()
        f = item.field("password")
        assert f is not None
        assert f.id == "f2"

    def test_field_missing(self):
        item = self._make_item()
        assert item.field("no_such_field") is None

    def test_field_is_case_sensitive(self):
        item = self._make_item()
        assert item.field("Password") is None
        assert item.field("password") is not None

    def test_fields_in_by_section_id(self):
        item = self._make_item()
        fields = item.fields_in("sec1")
        assert isinstance(fields, tuple)
        assert len(fields) == 1
        assert fields[0].id == "f2"

    def test_fields_in_by_section_label(self):
        item = self._make_item()
        fields = item.fields_in("Recovery")
        assert len(fields) == 1
        assert fields[0].id == "f4"

    def test_fields_in_by_item_section(self):
        item = self._make_item()
        section = item.sections[0]
        fields = item.fields_in(section)
        assert len(fields) == 1
        assert fields[0].id == "f2"

    def test_fields_in_item_section_with_no_fields(self):
        """A valid ItemSection that no field references still returns ()."""
        empty_section = ItemSection(id="empty", label="Empty")
        item = Item(
            id="itm1",
            title="x",
            vault_id="v1",
            vault_name="Personal",
            category="LOGIN",
            tags=(),
            sections=(empty_section,),
            fields=(ItemField(id="f1", label="u", value="a", type="STRING", section_id=None),),
        )
        assert item.fields_in(empty_section) == ()

    def test_fields_in_unknown_section_returns_empty(self):
        item = self._make_item()
        assert item.fields_in("nope") == ()

    def test_top_level_fields(self):
        item = self._make_item()
        top = item.top_level_fields()
        assert isinstance(top, tuple)
        assert {f.id for f in top} == {"f1", "f3"}


class TestItemURLLookup:
    def _make_item(self, urls: tuple[ItemURL, ...] = ()) -> Item:
        return Item(
            id="itm1",
            title="GitHub",
            vault_id="v1",
            vault_name="Personal",
            category="LOGIN",
            tags=(),
            sections=(),
            fields=(),
            urls=urls,
        )

    def test_url_found(self):
        item = self._make_item(
            urls=(
                ItemURL(href="https://github.com", label="website", primary=True),
                ItemURL(href="https://api.github.com", label="api", primary=False),
            )
        )
        u = item.url("api")
        assert u is not None
        assert u.href == "https://api.github.com"

    def test_url_missing(self):
        item = self._make_item(urls=(ItemURL(href="https://github.com", label="website"),))
        assert item.url("nope") is None

    def test_url_is_case_sensitive(self):
        item = self._make_item(urls=(ItemURL(href="https://github.com", label="website"),))
        assert item.url("Website") is None
        assert item.url("website") is not None

    def test_url_matches_first_with_label_when_duplicates(self):
        item = self._make_item(
            urls=(
                ItemURL(href="https://a.example.com", label="dup"),
                ItemURL(href="https://b.example.com", label="dup"),
            )
        )
        assert item.url("dup").href == "https://a.example.com"  # type: ignore[union-attr]

    def test_url_with_no_urls_returns_none(self):
        assert self._make_item().url("anything") is None

    def test_unlabeled_url_matches_default_label(self):
        """A URL constructed without a label has the default '"website"' and
        is findable by that name — mirrors 1Password's UI convention."""
        item = self._make_item(urls=(ItemURL(href="https://example.com"),))
        assert item.url("website") is not None
        assert item.url("") is None  # empty string is not the default

    def test_primary_url_found(self):
        item = self._make_item(
            urls=(
                ItemURL(href="https://a.example.com", label="alt", primary=False),
                ItemURL(href="https://b.example.com", label="website", primary=True),
            )
        )
        primary = item.primary_url()
        assert primary is not None
        assert primary.href == "https://b.example.com"

    def test_primary_url_returns_first_when_multiple_marked(self):
        item = self._make_item(
            urls=(
                ItemURL(href="https://a.example.com", primary=True),
                ItemURL(href="https://b.example.com", primary=True),
            )
        )
        primary = item.primary_url()
        assert primary is not None
        assert primary.href == "https://a.example.com"

    def test_primary_url_returns_none_when_no_primary(self):
        """Does not guess by falling back to the first entry."""
        item = self._make_item(
            urls=(
                ItemURL(href="https://a.example.com", primary=False),
                ItemURL(href="https://b.example.com", primary=False),
            )
        )
        assert item.primary_url() is None

    def test_primary_url_with_no_urls_returns_none(self):
        assert self._make_item().primary_url() is None


class TestItemSummary:
    def _make(self) -> ItemSummary:
        return ItemSummary(
            id="abc123",
            title="Test Item",
            vault_id="v1",
            vault_name="Personal",
            category="LOGIN",
            tags=("prod", "ssh"),
        )

    def test_construction(self):
        s = self._make()
        assert s.id == "abc123"
        assert s.title == "Test Item"
        assert s.vault_id == "v1"
        assert s.vault_name == "Personal"
        assert s.category == "LOGIN"
        assert s.tags == ("prod", "ssh")

    def test_frozen(self):
        s = self._make()
        with pytest.raises(FrozenInstanceError):
            s.title = "changed"  # type: ignore[misc]

    def test_equality(self):
        assert self._make() == self._make()

    def test_distinct_from_item(self):
        """An ItemSummary is not an Item — callers can type-distinguish the two."""
        s = self._make()
        assert not isinstance(s, Item)


class TestVaultSummary:
    def _make(self) -> VaultSummary:
        return VaultSummary(id="vqxm5hdjdy3f7hfgbk5p3ybrqe", name="Personal")

    def test_construction(self):
        v = self._make()
        assert v.id == "vqxm5hdjdy3f7hfgbk5p3ybrqe"
        assert v.name == "Personal"

    def test_frozen(self):
        v = self._make()
        with pytest.raises(FrozenInstanceError):
            v.name = "changed"  # type: ignore[misc]

    def test_equality(self):
        assert self._make() == self._make()

    def test_hashable(self):
        assert hash(self._make()) == hash(self._make())


class TestItemRef:
    def test_accepts_str(self):
        ref: ItemRef = "abc"
        assert isinstance(ref, str)

    def test_accepts_summary(self):
        ref: ItemRef = ItemSummary(id="id", title="t", vault_id="v", vault_name="n", category="LOGIN", tags=())
        assert isinstance(ref, ItemSummary)

    def test_accepts_item(self):
        ref: ItemRef = Item(
            id="id",
            title="t",
            vault_id="v",
            vault_name="n",
            category="LOGIN",
            tags=(),
            sections=(),
            fields=(),
        )
        assert isinstance(ref, Item)
