"""Tests for CLIBackend — the subprocess-based 1Password CLI backend."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

import pytest

from op_core.auth import DesktopAuth, ServiceAccountAuth
from op_core.backends import cli as cli_module
from op_core.backends.cli import (
    AsyncCLIBackend,
    CLIBackend,
    _map_error,
    _parse_item,
    _parse_item_summary,
)
from op_core.exceptions import (
    OpAuthError,
    OpError,
    OpNotFoundError,
    OpTimeoutError,
)
from op_core.items import Item, ItemSummary, ItemURL, VaultSummary

# Captured verbatim from `op read op://V/I/website` against a real desktop-auth
# vault where the item exists but the field has been removed. Contract fixture:
# rewriting it disconnects every test that uses it from the wire format. If a
# new op version rephrases the message, those tests fail and a fresh captured
# fixture should replace this one.
MISSING_FIELD_STDERR_FROM_OP_CLI = (
    "[ERROR] could not read secret 'op://VAULT/ITEM/website': item 'VAULT/ITEM' does not have a field 'website'"
)


# ---------- fake subprocess ----------


@dataclass
class FakeCompleted:
    returncode: int
    stdout: str
    stderr: str = ""


class SubprocessRecorder:
    """Replaces subprocess.run, capturing calls and returning queued results."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self._next_result: FakeCompleted | Exception = FakeCompleted(0, "")

    def set_result(self, result: FakeCompleted | Exception) -> None:
        self._next_result = result

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        if isinstance(self._next_result, Exception):
            raise self._next_result
        return self._next_result


@pytest.fixture
def recorder(monkeypatch):
    r = SubprocessRecorder()
    monkeypatch.setattr(subprocess, "run", r)
    return r


# ---------- _map_error ----------


class TestMapError:
    @pytest.mark.parametrize(
        "stderr",
        [
            "error: you are not currently signed in",
            "error: session expired, please sign in again",
            "please sign in: no session",
            "invalid token provided",
        ],
    )
    def test_auth_errors(self, stderr):
        exc = _map_error(1, stderr)
        assert isinstance(exc, OpAuthError)

    @pytest.mark.parametrize(
        "stderr",
        [
            'error: "foo" isn\'t an item',
            "item not found in vault",
            "the requested item doesn't exist",
            "no item with that id",
        ],
    )
    def test_not_found_errors(self, stderr):
        exc = _map_error(1, stderr)
        assert isinstance(exc, OpNotFoundError)

    def test_unknown_error_is_generic(self):
        exc = _map_error(1, "something weird went wrong")
        assert isinstance(exc, OpError)
        assert not isinstance(exc, OpAuthError | OpNotFoundError)

    def test_includes_stderr_in_message(self):
        exc = _map_error(1, "something weird went wrong")
        assert "something weird" in str(exc)

    def test_missing_field_classified_as_not_found(self):
        exc = _map_error(1, MISSING_FIELD_STDERR_FROM_OP_CLI)
        assert isinstance(exc, OpNotFoundError)


# ---------- parsers ----------


class TestParseItemSummary:
    def test_tags_as_strings(self):
        data = {
            "id": "abc",
            "title": "GitHub",
            "vault": {"id": "v1", "name": "Personal"},
            "category": "LOGIN",
            "tags": ["dev", "web"],
        }
        s = _parse_item_summary(data)
        assert s == ItemSummary(
            id="abc",
            title="GitHub",
            vault_id="v1",
            vault_name="Personal",
            category="LOGIN",
            tags=("dev", "web"),
        )

    def test_tags_as_dicts(self):
        data = {
            "id": "abc",
            "title": "GitHub",
            "vault": {"id": "v1", "name": "Personal"},
            "category": "LOGIN",
            "tags": [{"name": "dev"}, {"name": "web"}],
        }
        s = _parse_item_summary(data)
        assert s.tags == ("dev", "web")

    def test_missing_tags(self):
        data = {
            "id": "abc",
            "title": "GitHub",
            "vault": {"id": "v1", "name": "Personal"},
            "category": "LOGIN",
        }
        assert _parse_item_summary(data).tags == ()


class TestParseItem:
    def test_full_item_with_sections(self):
        data = {
            "id": "itm1",
            "title": "My Server",
            "vault": {"id": "v1", "name": "Work"},
            "category": "LOGIN",
            "tags": ["ssh"],
            "sections": [
                {"id": "s1", "label": "Credentials"},
                {"id": "s2", "label": "Details"},
            ],
            "fields": [
                {
                    "id": "f1",
                    "label": "username",
                    "value": "admin",
                    "type": "STRING",
                },
                {
                    "id": "f2",
                    "label": "password",
                    "value": "hunter2",
                    "type": "CONCEALED",
                    "section": {"id": "s1", "label": "Credentials"},
                },
                {
                    "id": "f3",
                    "label": "notes",
                    "value": None,
                    "type": "STRING",
                    "section": {"id": "s2", "label": "Details"},
                },
            ],
        }
        item = _parse_item(data)
        assert item.id == "itm1"
        assert item.title == "My Server"
        assert item.vault_id == "v1"
        assert item.vault_name == "Work"
        assert item.category == "LOGIN"
        assert item.tags == ("ssh",)
        assert len(item.sections) == 2
        assert item.sections[0].id == "s1"
        assert len(item.fields) == 3
        assert item.fields[0].section_id is None
        assert item.fields[1].section_id == "s1"
        assert item.fields[2].section_id == "s2"
        assert item.fields[2].value is None

    def test_empty_item(self):
        data = {
            "id": "itm0",
            "title": "Empty",
            "vault": {"id": "v1", "name": "P"},
            "category": "SECURE_NOTE",
        }
        item = _parse_item(data)
        assert item.tags == ()
        assert item.sections == ()
        assert item.fields == ()
        assert item.urls == ()

    def test_urls_labeled_primary(self):
        # Captured shape from `op item get --format json`: per-URL entries
        # carry `label`, `primary`, `href`. `label` and `primary` are
        # optional; `href` is the only guaranteed key.
        data = {
            "id": "itm1",
            "title": "Homelab NAS",
            "vault": {"id": "v1", "name": "Personal"},
            "category": "LOGIN",
            "urls": [{"label": "website", "primary": True, "href": "nas.example.com"}],
        }
        item = _parse_item(data)
        assert item.urls == (ItemURL(href="nas.example.com", label="website", primary=True),)

    def test_urls_unlabeled_and_not_primary(self):
        """When `label` and `primary` are absent in op's JSON, label defaults
        to '"website"' (1Password's UI convention) and primary defaults to False."""
        data = {
            "id": "itm1",
            "title": "T",
            "vault": {"id": "v1", "name": "P"},
            "category": "LOGIN",
            "urls": [{"href": "https://example.com"}],
        }
        item = _parse_item(data)
        assert len(item.urls) == 1
        u = item.urls[0]
        assert u.href == "https://example.com"
        assert u.label == "website"
        assert u.primary is False

    def test_urls_empty_label_string_defaults_to_website(self):
        """An explicit empty-string label is treated the same as missing —
        both fall back to the 1Password default '"website"'."""
        data = {
            "id": "itm1",
            "title": "T",
            "vault": {"id": "v1", "name": "P"},
            "category": "LOGIN",
            "urls": [{"label": "", "href": "https://example.com"}],
        }
        item = _parse_item(data)
        assert item.urls[0].label == "website"

    def test_urls_missing_href_skipped(self):
        """An URL entry with no `href` carries no destination — drop it."""
        data = {
            "id": "itm1",
            "title": "T",
            "vault": {"id": "v1", "name": "P"},
            "category": "LOGIN",
            "urls": [
                {"label": "broken", "primary": True},  # no href — skipped
                {"label": "good", "href": "https://ok.example.com"},
            ],
        }
        item = _parse_item(data)
        assert len(item.urls) == 1
        assert item.urls[0].label == "good"

    def test_urls_empty_href_skipped(self):
        data = {
            "id": "itm1",
            "title": "T",
            "vault": {"id": "v1", "name": "P"},
            "category": "LOGIN",
            "urls": [{"label": "empty", "href": ""}],
        }
        assert _parse_item(data).urls == ()

    def test_urls_none_href_skipped(self):
        # None is falsy — same filter path as a missing key, but explicit coverage
        # confirms the parser doesn't branch on type.
        data = {
            "id": "itm1",
            "title": "T",
            "vault": {"id": "v1", "name": "P"},
            "category": "LOGIN",
            "urls": [{"label": "x", "href": None}],
        }
        assert _parse_item(data).urls == ()

    def test_urls_unicode_label(self):
        data = {
            "id": "itm1",
            "title": "T",
            "vault": {"id": "v1", "name": "P"},
            "category": "LOGIN",
            "urls": [{"label": "サイト", "href": "https://example.jp"}],
        }
        item = _parse_item(data)
        assert len(item.urls) == 1
        assert item.urls[0].label == "サイト"
        assert item.urls[0].href == "https://example.jp"

    def test_multiple_urls_preserve_order(self):
        data = {
            "id": "itm1",
            "title": "T",
            "vault": {"id": "v1", "name": "P"},
            "category": "LOGIN",
            "urls": [
                {"label": "primary-site", "primary": True, "href": "https://a.example.com"},
                {"label": "mirror", "href": "https://b.example.com"},
                {"href": "https://c.example.com"},
            ],
        }
        item = _parse_item(data)
        assert [u.href for u in item.urls] == [
            "https://a.example.com",
            "https://b.example.com",
            "https://c.example.com",
        ]
        assert [u.primary for u in item.urls] == [True, False, False]

    def test_urls_primary_is_not_always_first(self):
        # Captured shape (sanitized hrefs) from a real `op item get` JSON
        # response. The `primary` entry is *last* and the two preceding
        # entries omit both `label` and `primary` entirely. Guards against
        # any future temptation to infer primacy from list position — the
        # explicit flag is the only authoritative marker.
        data = {
            "id": "itm1",
            "title": "Storage",
            "vault": {"id": "v1", "name": "P"},
            "category": "LOGIN",
            "urls": [
                {"href": "https://storage.example.com/"},
                {"href": "https://storage/"},
                {"label": "website", "primary": True, "href": "nas.example.com"},
            ],
        }
        item = _parse_item(data)
        assert len(item.urls) == 3
        # Order is preserved...
        assert [u.href for u in item.urls] == [
            "https://storage.example.com/",
            "https://storage/",
            "nas.example.com",
        ]
        # ...but primary is identified by the flag, not by position.
        assert [u.primary for u in item.urls] == [False, False, True]
        primary = item.primary_url()
        assert primary is not None
        assert primary.href == "nas.example.com"
        # The two unlabeled entries default to label='website' (1Password's UI
        # convention) — same as if the user had typed "website" explicitly.
        assert item.urls[0].label == "website"
        assert item.urls[1].label == "website"

    def test_urls_against_real_op_payload(self):
        # Captured from `op item get <id> --format json` against a real
        # desktop-auth vault (sanitized: ids/hrefs synthetic, schema preserved).
        # Reproduces the trigger that motivated this change: a LOGIN item whose
        # `host` field stores `op://././website` — the `website` token is the
        # label of a primary URL on the same item, not a field, so the CLI
        # rejects `op read op://V/I/website`. After the URL is exposed on
        # Item.urls, a validator can distinguish "URL label" from "missing field"
        # without re-fetching the raw JSON.
        op_item_get_payload = {
            "id": "axhomelabnas000000000000000",
            "title": "Homelab NAS",
            "version": 3,
            "vault": {"id": "vqxm5hdjdy3f7hfgbk5p3ybrqe", "name": "Personal"},
            "category": "LOGIN",
            "last_edited_by": "U7JFCKH5RJB5RFEVBYZWLA4SUI",
            "created_at": "2024-01-15T10:11:12Z",
            "updated_at": "2024-08-30T07:21:33Z",
            "additional_information": "admin",
            "urls": [
                {"label": "website", "primary": True, "href": "nas.example.com"},
                {"label": "admin", "href": "https://nas.example.com:8443"},
            ],
            "sections": [],
            "fields": [
                {
                    "id": "host",
                    "label": "host",
                    "value": "op://././website",
                    "type": "STRING",
                },
                {
                    "id": "username",
                    "label": "username",
                    "value": "admin",
                    "type": "STRING",
                },
            ],
            "tags": [],
        }
        item = _parse_item(op_item_get_payload)
        assert len(item.urls) == 2
        assert item.url("website") is not None
        primary = item.primary_url()
        assert primary is not None
        assert primary.href == "nas.example.com"
        # And the field that references it is still parsed normally — both
        # facts coexist on the same Item.
        host = item.field("host")
        assert host is not None
        assert host.value == "op://././website"


# ---------- CLIBackend ----------


class TestCLIBackendRun:
    def test_happy_path_returns_stdout(self, recorder):
        recorder.set_result(FakeCompleted(0, "hello\n"))
        backend = CLIBackend()
        assert backend.read("op://v/i/f") == "hello"

    def test_non_zero_maps_error(self, recorder):
        recorder.set_result(FakeCompleted(1, "", "not found in vault"))
        backend = CLIBackend()
        with pytest.raises(OpNotFoundError):
            backend.read("op://v/i/missing")

    def test_binary_not_found(self, recorder):
        recorder.set_result(FileNotFoundError("no such file"))
        backend = CLIBackend()
        with pytest.raises(OpError, match="op CLI not found"):
            backend.read("op://v/i/f")

    def test_timeout(self, recorder):
        recorder.set_result(subprocess.TimeoutExpired(cmd="op", timeout=120))
        backend = CLIBackend()
        with pytest.raises(OpTimeoutError):
            backend.read("op://v/i/f")

    def test_service_account_auth_sets_env(self, recorder, monkeypatch):
        monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
        recorder.set_result(FakeCompleted(0, "v"))
        backend = CLIBackend(auth=ServiceAccountAuth(token="ops_abc"))
        backend.read("op://v/i/f")
        env = recorder.calls[0]["env"]
        assert env["OP_SERVICE_ACCOUNT_TOKEN"] == "ops_abc"

    def test_desktop_auth_leaves_env_unchanged(self, recorder, monkeypatch):
        monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
        recorder.set_result(FakeCompleted(0, "v"))
        backend = CLIBackend(auth=DesktopAuth())
        backend.read("op://v/i/f")
        env = recorder.calls[0]["env"]
        assert "OP_SERVICE_ACCOUNT_TOKEN" not in env

    def test_custom_binary(self, recorder):
        recorder.set_result(FakeCompleted(0, "v"))
        backend = CLIBackend(binary="/opt/1p/op")
        backend.read("op://v/i/f")
        assert recorder.calls[0]["args"][0] == "/opt/1p/op"

    def test_custom_timeout(self, recorder):
        recorder.set_result(FakeCompleted(0, "v"))
        backend = CLIBackend(timeout=30)
        backend.read("op://v/i/f")
        assert recorder.calls[0]["timeout"] == 30

    def test_non_positive_timeout_rejected(self):
        with pytest.raises(ValueError, match="timeout"):
            CLIBackend(timeout=0)
        with pytest.raises(ValueError, match="timeout"):
            CLIBackend(timeout=-1)


class TestCLIBackendRead:
    def test_passes_reference(self, recorder):
        recorder.set_result(FakeCompleted(0, "secret"))
        CLIBackend().read("op://v/i/field")
        assert recorder.calls[0]["args"][1:] == ["read", "op://v/i/field"]

    def test_missing_raises_without_default(self, recorder):
        recorder.set_result(FakeCompleted(1, "", "item not found"))
        with pytest.raises(OpNotFoundError):
            CLIBackend().read("op://v/i/nope")

    def test_missing_returns_default_value(self, recorder):
        recorder.set_result(FakeCompleted(1, "", "item not found"))
        assert CLIBackend().read("op://v/i/nope", default_value="fallback") == "fallback"

    def test_missing_with_empty_default(self, recorder):
        recorder.set_result(FakeCompleted(1, "", "item not found"))
        assert CLIBackend().read("op://v/i/nope", default_value="") == ""

    def test_default_value_none_still_raises(self, recorder):
        recorder.set_result(FakeCompleted(1, "", "item not found"))
        with pytest.raises(OpNotFoundError):
            CLIBackend().read("op://v/i/nope", default_value=None)

    def test_auth_error_not_swallowed_by_default(self, recorder):
        recorder.set_result(FakeCompleted(1, "", "you are not signed in"))
        with pytest.raises(OpAuthError):
            CLIBackend().read("op://v/i/nope", default_value="fallback")


class TestCLIBackendListItems:
    def test_no_filters(self, recorder):
        recorder.set_result(FakeCompleted(0, "[]"))
        result = CLIBackend().list_items()
        assert result == []
        assert recorder.calls[0]["args"][1:] == ["item", "list", "--format", "json"]

    def test_vault_filter(self, recorder):
        recorder.set_result(FakeCompleted(0, "[]"))
        CLIBackend().list_items(vault="Personal")
        args = recorder.calls[0]["args"]
        assert "--vault" in args
        assert args[args.index("--vault") + 1] == "Personal"

    def test_tags_filter(self, recorder):
        recorder.set_result(FakeCompleted(0, "[]"))
        CLIBackend().list_items(tags=["dev", "prod"])
        args = recorder.calls[0]["args"]
        assert "--tags" in args
        assert args[args.index("--tags") + 1] == "dev,prod"

    def test_categories_filter_not_forwarded_to_op(self, recorder):
        recorder.set_result(FakeCompleted(0, "[]"))
        CLIBackend().list_items(categories=["LOGIN", "SECURE_NOTE"])
        args = recorder.calls[0]["args"]
        assert "--categories" not in args

    def test_categories_filter_applied_client_side(self, recorder):
        payload = json.dumps(
            [
                {
                    "id": "a",
                    "title": "GitHub",
                    "vault": {"id": "v1", "name": "P"},
                    "category": "LOGIN",
                    "tags": [],
                },
                {
                    "id": "b",
                    "title": "id_rsa",
                    "vault": {"id": "v1", "name": "P"},
                    "category": "SSH_KEY",
                    "tags": [],
                },
                {
                    "id": "c",
                    "title": "Recovery codes",
                    "vault": {"id": "v1", "name": "P"},
                    "category": "SECURE_NOTE",
                    "tags": [],
                },
            ]
        )
        recorder.set_result(FakeCompleted(0, payload))
        result = CLIBackend().list_items(categories=["SSH_KEY", "SECURE_NOTE"])
        assert {s.id for s in result} == {"b", "c"}

    def test_empty_tags_rejected(self):
        with pytest.raises(ValueError, match="tags"):
            CLIBackend().list_items(tags=[])

    def test_empty_categories_rejected(self):
        with pytest.raises(ValueError, match="categories"):
            CLIBackend().list_items(categories=[])

    def test_tag_with_comma_rejected(self):
        with pytest.raises(ValueError, match="comma"):
            CLIBackend().list_items(tags=["dev", "infra,prod"])

    def test_category_with_comma_rejected(self):
        with pytest.raises(ValueError, match="comma"):
            CLIBackend().list_items(categories=["LOGIN,NOTE"])

    def test_parses_summaries(self, recorder):
        payload = json.dumps(
            [
                {
                    "id": "a",
                    "title": "A",
                    "vault": {"id": "v1", "name": "P"},
                    "category": "LOGIN",
                    "tags": ["t1"],
                },
                {
                    "id": "b",
                    "title": "B",
                    "vault": {"id": "v1", "name": "P"},
                    "category": "SSH_KEY",
                    "tags": [],
                },
            ]
        )
        recorder.set_result(FakeCompleted(0, payload))
        result = CLIBackend().list_items()
        assert len(result) == 2
        assert all(isinstance(x, ItemSummary) for x in result)
        assert result[0].id == "a"
        assert result[1].category == "SSH_KEY"

    def test_categories_filter_against_real_op_payload(self, recorder):
        # Captured from `op item list --format json` against a real desktop-auth
        # vault (sanitized: ids/titles synthetic, schema preserved). Includes
        # fields the parser ignores (version, urls, additional_information,
        # timestamps) so the contract test stays faithful to the op CLI shape
        # rather than the parser's minimum required keys.
        op_item_list_payload = json.dumps(
            [
                {
                    "id": "ax7m2qpkzv6ntzz4smgz4gxkny",
                    "title": "GitHub",
                    "version": 3,
                    "vault": {"id": "vqxm5hdjdy3f7hfgbk5p3ybrqe", "name": "Personal"},
                    "category": "LOGIN",
                    "last_edited_by": "U7JFCKH5RJB5RFEVBYZWLA4SUI",
                    "created_at": "2024-01-15T10:11:12Z",
                    "updated_at": "2024-08-30T07:21:33Z",
                    "additional_information": "alice@example.com",
                    "urls": [{"primary": True, "href": "https://github.com"}],
                    "tags": ["dev", "web"],
                },
                {
                    "id": "bx7m2qpkzv6ntzz4smgz4gxkne",
                    "title": "id_ed25519 — homelab",
                    "version": 1,
                    "vault": {"id": "vqxm5hdjdy3f7hfgbk5p3ybrqe", "name": "Personal"},
                    "category": "SSH_KEY",
                    "last_edited_by": "U7JFCKH5RJB5RFEVBYZWLA4SUI",
                    "created_at": "2024-03-02T22:09:01Z",
                    "updated_at": "2024-03-02T22:09:01Z",
                    "tags": ["SSH Host"],
                },
                {
                    "id": "cx7m2qpkzv6ntzz4smgz4gxknq",
                    "title": "id_rsa — work jumpbox",
                    "version": 2,
                    "vault": {"id": "vqxm5hdjdy3f7hfgbk5p3ybrqe", "name": "Personal"},
                    "category": "SSH_KEY",
                    "last_edited_by": "U7JFCKH5RJB5RFEVBYZWLA4SUI",
                    "created_at": "2024-02-18T09:00:00Z",
                    "updated_at": "2024-09-10T13:14:15Z",
                    "tags": ["SSH Host", "work"],
                },
                {
                    "id": "dx7m2qpkzv6ntzz4smgz4gxknu",
                    "title": "Wifi recovery codes",
                    "version": 1,
                    "vault": {"id": "vqxm5hdjdy3f7hfgbk5p3ybrqe", "name": "Personal"},
                    "category": "SECURE_NOTE",
                    "last_edited_by": "U7JFCKH5RJB5RFEVBYZWLA4SUI",
                    "created_at": "2024-05-04T11:30:00Z",
                    "updated_at": "2024-05-04T11:30:00Z",
                    "tags": [],
                },
                {
                    "id": "ex7m2qpkzv6ntzz4smgz4gxkn2",
                    "title": "Visa — personal",
                    "version": 4,
                    "vault": {"id": "vqxm5hdjdy3f7hfgbk5p3ybrqe", "name": "Personal"},
                    "category": "CREDIT_CARD",
                    "last_edited_by": "U7JFCKH5RJB5RFEVBYZWLA4SUI",
                    "created_at": "2023-11-01T08:00:00Z",
                    "updated_at": "2024-10-15T16:00:00Z",
                    "tags": [],
                },
            ]
        )
        recorder.set_result(FakeCompleted(0, op_item_list_payload))

        result = CLIBackend().list_items(categories=["SSH_KEY", "SECURE_NOTE"])

        # The op CLI was invoked WITHOUT --categories (canonical names would
        # error with "Unknown item category SSH_KEY"); filtering happens after.
        argv = recorder.calls[0]["args"]
        assert "--categories" not in argv
        assert {s.id for s in result} == {
            "bx7m2qpkzv6ntzz4smgz4gxkne",
            "cx7m2qpkzv6ntzz4smgz4gxknq",
            "dx7m2qpkzv6ntzz4smgz4gxknu",
        }
        assert {s.category for s in result} == {"SSH_KEY", "SECURE_NOTE"}


class TestCLIBackendListVaults:
    def test_empty_account(self, recorder):
        recorder.set_result(FakeCompleted(0, "[]"))
        result = CLIBackend().list_vaults()
        assert result == []
        assert recorder.calls[0]["args"][1:] == ["vault", "list", "--format", "json"]

    def test_parses_vaults(self, recorder):
        payload = json.dumps(
            [
                {"id": "vqxm5hdjdy3f7hfgbk5p3ybrqe", "name": "Personal", "content_version": 1},
                {"id": "vp6yqr5lhblk4gctaaprynwv2u", "name": "Shared", "content_version": 7},
            ]
        )
        recorder.set_result(FakeCompleted(0, payload))
        result = CLIBackend().list_vaults()
        assert result == [
            VaultSummary(id="vqxm5hdjdy3f7hfgbk5p3ybrqe", name="Personal"),
            VaultSummary(id="vp6yqr5lhblk4gctaaprynwv2u", name="Shared"),
        ]

    def test_returns_vault_summary_instances(self, recorder):
        payload = json.dumps([{"id": "v1", "name": "P"}])
        recorder.set_result(FakeCompleted(0, payload))
        result = CLIBackend().list_vaults()
        assert all(isinstance(v, VaultSummary) for v in result)

    def test_timeout_propagates(self, recorder):
        recorder.set_result(subprocess.TimeoutExpired(cmd="op", timeout=1))
        with pytest.raises(OpTimeoutError):
            CLIBackend().list_vaults()

    def test_auth_error_propagates(self, recorder):
        recorder.set_result(FakeCompleted(1, "", "[ERROR] you are not currently signed in"))
        with pytest.raises(OpAuthError):
            CLIBackend().list_vaults()

    def test_against_real_op_payload(self, recorder):
        # Captured from `op vault list --format json` shape (sanitized: ids
        # synthetic, schema preserved). Includes content_version which the
        # parser ignores, so the contract test stays faithful to the op CLI
        # output rather than the parser's minimum required keys.
        op_vault_list_payload = json.dumps(
            [
                {"id": "vqxm5hdjdy3f7hfgbk5p3ybrqe", "name": "Personal", "content_version": 47},
                {"id": "vp6yqr5lhblk4gctaaprynwv2u", "name": "Shared", "content_version": 12},
                {"id": "vh7yqr5lhblk4gctaaprynwv8e", "name": "Work", "content_version": 1},
            ]
        )
        recorder.set_result(FakeCompleted(0, op_vault_list_payload))
        result = CLIBackend().list_vaults()
        assert {v.id for v in result} == {
            "vqxm5hdjdy3f7hfgbk5p3ybrqe",
            "vp6yqr5lhblk4gctaaprynwv2u",
            "vh7yqr5lhblk4gctaaprynwv8e",
        }
        assert {v.name for v in result} == {"Personal", "Shared", "Work"}


class TestCLIBackendGetItem:
    def _make_item_json(self, *, item_id="itm1", vault_id="v1") -> str:
        return json.dumps(
            {
                "id": item_id,
                "title": "T",
                "vault": {"id": vault_id, "name": "P"},
                "category": "LOGIN",
                "tags": [],
                "sections": [],
                "fields": [],
            }
        )

    def test_with_string_id(self, recorder):
        recorder.set_result(FakeCompleted(0, self._make_item_json()))
        item = CLIBackend().get_item("itm1")
        assert isinstance(item, Item)
        args = recorder.calls[0]["args"]
        assert args[1:4] == ["item", "get", "itm1"]
        assert "--vault" not in args

    def test_with_string_id_and_vault(self, recorder):
        recorder.set_result(FakeCompleted(0, self._make_item_json()))
        CLIBackend().get_item("itm1", vault="v1")
        args = recorder.calls[0]["args"]
        assert "--vault" in args
        assert args[args.index("--vault") + 1] == "v1"

    def test_with_summary_uses_its_vault(self, recorder):
        recorder.set_result(FakeCompleted(0, self._make_item_json()))
        summary = ItemSummary(
            id="itm1",
            title="T",
            vault_id="v1",
            vault_name="P",
            category="LOGIN",
            tags=(),
        )
        CLIBackend().get_item(summary)
        args = recorder.calls[0]["args"]
        assert args[args.index("--vault") + 1] == "v1"

    def test_explicit_vault_overrides_summary(self, recorder):
        recorder.set_result(FakeCompleted(0, self._make_item_json()))
        summary = ItemSummary(
            id="itm1",
            title="T",
            vault_id="v1",
            vault_name="P",
            category="LOGIN",
            tags=(),
        )
        CLIBackend().get_item(summary, vault="v2")
        args = recorder.calls[0]["args"]
        assert args[args.index("--vault") + 1] == "v2"

    def test_with_item_instance(self, recorder):
        recorder.set_result(FakeCompleted(0, self._make_item_json()))
        existing = Item(
            id="itm1",
            title="T",
            vault_id="v1",
            vault_name="P",
            category="LOGIN",
            tags=(),
            sections=(),
            fields=(),
        )
        CLIBackend().get_item(existing)
        args = recorder.calls[0]["args"]
        assert args[1:4] == ["item", "get", "itm1"]
        assert args[args.index("--vault") + 1] == "v1"


class TestCLIBackendChainFallThrough:
    """End-to-end: missing-field stderr from `op` must classify as
    OpNotFoundError so OnePassword.resolve walks past it instead of aborting.

    The bug this guards against: if the classifier returns generic OpError on
    "does not have a field", OnePassword.read does not catch it (only
    OpNotFoundError is caught), so the OpError propagates out of resolve_chain
    and aborts the walk on what should be a fall-through case.
    """

    def test_missing_field_at_end_of_chain_returns_none(self, recorder):
        from op_core.client import OnePassword
        from op_core.field import FieldValue

        recorder.set_result(FakeCompleted(1, "", MISSING_FIELD_STDERR_FROM_OP_CLI))
        client = OnePassword(backend=CLIBackend())
        field = FieldValue.from_raw("op://Vault/Item/website", "hostname")
        assert client.resolve(field) is None

    def test_missing_field_falls_through_to_literal(self, recorder):
        from op_core.client import OnePassword
        from op_core.field import FieldValue

        recorder.set_result(FakeCompleted(1, "", MISSING_FIELD_STDERR_FROM_OP_CLI))
        client = OnePassword(backend=CLIBackend())
        field = FieldValue.from_raw("op://Vault/Item/website||fallback.example.com", "hostname")
        assert client.resolve(field) == "fallback.example.com"


# ---------- fake async subprocess ----------


class FakeProcess:
    """Minimal fake of asyncio.subprocess.Process."""

    def __init__(
        self,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        hang: bool = False,
    ):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            import asyncio as _a

            await _a.sleep(10)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        return self.returncode or 0


class AsyncSubprocessRecorder:
    """Replaces asyncio.create_subprocess_exec; records calls, returns queued Process."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self._next: FakeProcess | Exception = FakeProcess()

    def set_result(self, proc: FakeProcess | Exception) -> None:
        self._next = proc

    async def __call__(self, *args, **kwargs):
        self.calls.append({"args": list(args), **kwargs})
        if isinstance(self._next, Exception):
            raise self._next
        return self._next


@pytest.fixture
def arecorder(monkeypatch):
    r = AsyncSubprocessRecorder()
    monkeypatch.setattr(cli_module.asyncio, "create_subprocess_exec", r)
    return r


class TestAsyncCLIBackendRun:
    async def test_happy_path_returns_stdout(self, arecorder):
        arecorder.set_result(FakeProcess(0, b"hello\n"))
        backend = AsyncCLIBackend()
        assert await backend.read("op://v/i/f") == "hello"

    async def test_non_zero_maps_error(self, arecorder):
        arecorder.set_result(FakeProcess(1, b"", b"not found in vault"))
        backend = AsyncCLIBackend()
        with pytest.raises(OpNotFoundError):
            await backend.read("op://v/i/missing")

    async def test_binary_not_found(self, arecorder):
        arecorder.set_result(FileNotFoundError("no such file"))
        backend = AsyncCLIBackend()
        with pytest.raises(OpError, match="op CLI not found"):
            await backend.read("op://v/i/f")

    async def test_timeout_kills_process(self, arecorder):
        hanging = FakeProcess(hang=True)
        arecorder.set_result(hanging)
        # Small but positive timeout — the hanging process sleeps 10s, so the
        # deadline genuinely expires during communicate() rather than before
        # it starts running.
        backend = AsyncCLIBackend(timeout=0.05)
        with pytest.raises(OpTimeoutError):
            await backend.read("op://v/i/f")
        assert hanging.killed
        assert hanging.waited

    async def test_non_positive_timeout_rejected(self):
        with pytest.raises(ValueError, match="timeout"):
            AsyncCLIBackend(timeout=0)
        with pytest.raises(ValueError, match="timeout"):
            AsyncCLIBackend(timeout=-1)

    async def test_service_account_auth_sets_env(self, arecorder, monkeypatch):
        monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
        arecorder.set_result(FakeProcess(0, b"v"))
        backend = AsyncCLIBackend(auth=ServiceAccountAuth(token="ops_abc"))
        await backend.read("op://v/i/f")
        env = arecorder.calls[0]["env"]
        assert env["OP_SERVICE_ACCOUNT_TOKEN"] == "ops_abc"

    async def test_desktop_auth_leaves_env_unchanged(self, arecorder, monkeypatch):
        monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
        arecorder.set_result(FakeProcess(0, b"v"))
        backend = AsyncCLIBackend(auth=DesktopAuth())
        await backend.read("op://v/i/f")
        env = arecorder.calls[0]["env"]
        assert "OP_SERVICE_ACCOUNT_TOKEN" not in env

    async def test_custom_binary(self, arecorder):
        arecorder.set_result(FakeProcess(0, b"v"))
        backend = AsyncCLIBackend(binary="/opt/1p/op")
        await backend.read("op://v/i/f")
        assert arecorder.calls[0]["args"][0] == "/opt/1p/op"


class TestAsyncCLIBackendRead:
    async def test_passes_reference(self, arecorder):
        arecorder.set_result(FakeProcess(0, b"secret"))
        await AsyncCLIBackend().read("op://v/i/field")
        assert arecorder.calls[0]["args"][1:] == ["read", "op://v/i/field"]

    async def test_missing_raises_without_default(self, arecorder):
        arecorder.set_result(FakeProcess(1, b"", b"item not found"))
        with pytest.raises(OpNotFoundError):
            await AsyncCLIBackend().read("op://v/i/nope")

    async def test_missing_returns_default_value(self, arecorder):
        arecorder.set_result(FakeProcess(1, b"", b"item not found"))
        result = await AsyncCLIBackend().read("op://v/i/nope", default_value="fallback")
        assert result == "fallback"

    async def test_missing_with_empty_default(self, arecorder):
        arecorder.set_result(FakeProcess(1, b"", b"item not found"))
        assert await AsyncCLIBackend().read("op://v/i/nope", default_value="") == ""

    async def test_default_value_none_still_raises(self, arecorder):
        arecorder.set_result(FakeProcess(1, b"", b"item not found"))
        with pytest.raises(OpNotFoundError):
            await AsyncCLIBackend().read("op://v/i/nope", default_value=None)

    async def test_auth_error_not_swallowed_by_default(self, arecorder):
        arecorder.set_result(FakeProcess(1, b"", b"you are not signed in"))
        with pytest.raises(OpAuthError):
            await AsyncCLIBackend().read("op://v/i/nope", default_value="fallback")


class TestAsyncCLIBackendListItems:
    async def test_no_filters(self, arecorder):
        arecorder.set_result(FakeProcess(0, b"[]"))
        result = await AsyncCLIBackend().list_items()
        assert result == []
        assert arecorder.calls[0]["args"][1:] == ["item", "list", "--format", "json"]

    async def test_vault_filter(self, arecorder):
        arecorder.set_result(FakeProcess(0, b"[]"))
        await AsyncCLIBackend().list_items(vault="Personal")
        args = arecorder.calls[0]["args"]
        assert args[args.index("--vault") + 1] == "Personal"

    async def test_tags_filter(self, arecorder):
        arecorder.set_result(FakeProcess(0, b"[]"))
        await AsyncCLIBackend().list_items(tags=["dev", "prod"])
        args = arecorder.calls[0]["args"]
        assert args[args.index("--tags") + 1] == "dev,prod"

    async def test_categories_filter_not_forwarded_to_op(self, arecorder):
        arecorder.set_result(FakeProcess(0, b"[]"))
        await AsyncCLIBackend().list_items(categories=["LOGIN", "SECURE_NOTE"])
        args = arecorder.calls[0]["args"]
        assert "--categories" not in args

    async def test_categories_filter_applied_client_side(self, arecorder):
        payload = json.dumps(
            [
                {
                    "id": "a",
                    "title": "GitHub",
                    "vault": {"id": "v1", "name": "P"},
                    "category": "LOGIN",
                    "tags": [],
                },
                {
                    "id": "b",
                    "title": "id_rsa",
                    "vault": {"id": "v1", "name": "P"},
                    "category": "SSH_KEY",
                    "tags": [],
                },
            ]
        ).encode()
        arecorder.set_result(FakeProcess(0, payload))
        result = await AsyncCLIBackend().list_items(categories=["SSH_KEY"])
        assert [s.id for s in result] == ["b"]

    async def test_empty_tags_rejected(self):
        with pytest.raises(ValueError, match="tags"):
            await AsyncCLIBackend().list_items(tags=[])

    async def test_empty_categories_rejected(self):
        with pytest.raises(ValueError, match="categories"):
            await AsyncCLIBackend().list_items(categories=[])

    async def test_tag_with_comma_rejected(self):
        with pytest.raises(ValueError, match="comma"):
            await AsyncCLIBackend().list_items(tags=["infra,prod"])

    async def test_category_with_comma_rejected(self):
        with pytest.raises(ValueError, match="comma"):
            await AsyncCLIBackend().list_items(categories=["LOGIN,NOTE"])

    async def test_parses_summaries(self, arecorder):
        payload = json.dumps(
            [
                {
                    "id": "a",
                    "title": "A",
                    "vault": {"id": "v1", "name": "P"},
                    "category": "LOGIN",
                    "tags": ["t1"],
                },
            ]
        ).encode()
        arecorder.set_result(FakeProcess(0, payload))
        result = await AsyncCLIBackend().list_items()
        assert len(result) == 1
        assert isinstance(result[0], ItemSummary)
        assert result[0].id == "a"


class TestAsyncCLIBackendGetItem:
    def _make_item_json(self) -> bytes:
        return json.dumps(
            {
                "id": "itm1",
                "title": "T",
                "vault": {"id": "v1", "name": "P"},
                "category": "LOGIN",
                "tags": [],
                "sections": [],
                "fields": [],
            }
        ).encode()

    async def test_with_string_id(self, arecorder):
        arecorder.set_result(FakeProcess(0, self._make_item_json()))
        item = await AsyncCLIBackend().get_item("itm1")
        assert isinstance(item, Item)
        args = arecorder.calls[0]["args"]
        assert args[1:4] == ["item", "get", "itm1"]
        assert "--vault" not in args

    async def test_with_summary_uses_its_vault(self, arecorder):
        arecorder.set_result(FakeProcess(0, self._make_item_json()))
        summary = ItemSummary(
            id="itm1",
            title="T",
            vault_id="v1",
            vault_name="P",
            category="LOGIN",
            tags=(),
        )
        await AsyncCLIBackend().get_item(summary)
        args = arecorder.calls[0]["args"]
        assert args[args.index("--vault") + 1] == "v1"

    async def test_explicit_vault_overrides_summary(self, arecorder):
        arecorder.set_result(FakeProcess(0, self._make_item_json()))
        summary = ItemSummary(
            id="itm1",
            title="T",
            vault_id="v1",
            vault_name="P",
            category="LOGIN",
            tags=(),
        )
        await AsyncCLIBackend().get_item(summary, vault="v2")
        args = arecorder.calls[0]["args"]
        assert args[args.index("--vault") + 1] == "v2"

    async def test_with_item_instance(self, arecorder):
        arecorder.set_result(FakeProcess(0, self._make_item_json()))
        existing = Item(
            id="itm1",
            title="T",
            vault_id="v1",
            vault_name="P",
            category="LOGIN",
            tags=(),
            sections=(),
            fields=(),
        )
        await AsyncCLIBackend().get_item(existing)
        args = arecorder.calls[0]["args"]
        assert args[1:4] == ["item", "get", "itm1"]
        assert args[args.index("--vault") + 1] == "v1"

    async def test_get_item_urls_propagated(self, arecorder):
        # Verifies the async path flows through _parse_item and surfaces URLs on
        # the returned Item. Two entries: one primary, one not.
        payload = json.dumps(
            {
                "id": "itm1",
                "title": "Multi-URL",
                "vault": {"id": "v1", "name": "P"},
                "category": "LOGIN",
                "tags": [],
                "sections": [],
                "fields": [],
                "urls": [
                    {"label": "website", "primary": True, "href": "https://example.com"},
                    {"label": "api", "primary": False, "href": "https://api.example.com"},
                ],
            }
        ).encode()
        arecorder.set_result(FakeProcess(0, payload))
        item = await AsyncCLIBackend().get_item("itm1")
        assert len(item.urls) == 2
        assert item.urls[0] == ItemURL(href="https://example.com", label="website", primary=True)
        assert item.urls[1] == ItemURL(href="https://api.example.com", label="api", primary=False)
        primary = item.primary_url()
        assert primary is not None
        assert primary.href == "https://example.com"


class TestAsyncCLIBackendListVaults:
    async def test_empty_account(self, arecorder):
        arecorder.set_result(FakeProcess(0, b"[]"))
        result = await AsyncCLIBackend().list_vaults()
        assert result == []
        assert arecorder.calls[0]["args"][1:] == ["vault", "list", "--format", "json"]

    async def test_parses_vaults(self, arecorder):
        payload = json.dumps(
            [
                {"id": "v1", "name": "Personal", "content_version": 1},
                {"id": "v2", "name": "Shared", "content_version": 7},
            ]
        ).encode()
        arecorder.set_result(FakeProcess(0, payload))
        result = await AsyncCLIBackend().list_vaults()
        assert result == [
            VaultSummary(id="v1", name="Personal"),
            VaultSummary(id="v2", name="Shared"),
        ]

    async def test_timeout_kills_process(self, arecorder):
        hanging = FakeProcess(hang=True)
        arecorder.set_result(hanging)
        backend = AsyncCLIBackend(timeout=0.05)
        with pytest.raises(OpTimeoutError):
            await backend.list_vaults()
        assert hanging.killed

    async def test_auth_error_propagates(self, arecorder):
        arecorder.set_result(FakeProcess(1, b"", b"[ERROR] you are not currently signed in"))
        with pytest.raises(OpAuthError):
            await AsyncCLIBackend().list_vaults()
