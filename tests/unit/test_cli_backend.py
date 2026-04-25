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
from op_core.items import Item, ItemSummary

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
