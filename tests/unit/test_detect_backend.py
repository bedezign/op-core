"""Unit tests for :mod:`op_core.backends.detect`."""

from __future__ import annotations

import importlib.util
import shutil
import sys
import types

import pytest

from op_core.auth import SERVICE_ACCOUNT_ENV_VAR, DesktopAuth, ServiceAccountAuth
from op_core.backends import detect as detect_module
from op_core.backends.cli import AsyncCLIBackend, CLIBackend
from op_core.backends.detect import (
    _resolve_binary,
    detect_async_backend,
    detect_backend,
)
from op_core.exceptions import OpError

# -- SDK stubs --------------------------------------------------------------
#
# ``op_core.backends.sdk`` is produced by a parallel task and may be absent
# from this worktree. Tests that need the SDK path install a stub module
# into ``sys.modules`` so ``from op_core.backends.sdk import SDKBackend``
# resolves against the stub.


class _StubSDKBackend:
    def __init__(self, auth):
        self.auth = auth


class _StubAsyncSDKBackend:
    def __init__(self, auth):
        self.auth = auth


def _install_sdk_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = types.SimpleNamespace(
        SDKBackend=_StubSDKBackend,
        AsyncSDKBackend=_StubAsyncSDKBackend,
    )
    monkeypatch.setitem(sys.modules, "op_core.backends.sdk", stub)


def _mark_sdk_available(monkeypatch: pytest.MonkeyPatch, available: bool) -> None:
    real_find_spec = importlib.util.find_spec

    def fake(name: str, *args, **kwargs):
        if name == "onepassword":
            return object() if available else None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake)


def _forbid_which(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        pytest.fail("shutil.which should not be called on the optimal path")

    monkeypatch.setattr(shutil, "which", boom)


def _fake_which(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str | None]) -> None:
    def fake(name: str, *args, **kwargs):
        return mapping.get(name)

    monkeypatch.setattr(shutil, "which", fake)


# -- _resolve_binary --------------------------------------------------------


class TestResolveBinary:
    def test_none_delegates_to_which_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_which(monkeypatch, {"op": "/usr/bin/op"})
        assert _resolve_binary(None) == "/usr/bin/op"

    def test_none_returns_none_when_op_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_which(monkeypatch, {})
        assert _resolve_binary(None) is None

    def test_explicit_path_resolves_via_which(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_which(monkeypatch, {"/opt/op": "/opt/op"})
        assert _resolve_binary("/opt/op") == "/opt/op"

    def test_explicit_unresolvable_is_hard_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_which(monkeypatch, {})
        with pytest.raises(OpError, match="not found or not executable"):
            _resolve_binary("nonexistent-op")

    def test_real_executable_file(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        exe = tmp_path / "op-fake"
        exe.write_text("#!/bin/sh\necho hi\n")
        exe.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        assert _resolve_binary("op-fake") == str(exe)


# -- Decision matrix: sync --------------------------------------------------


class TestDetectBackendDecisionMatrix:
    def test_token_explicit_binary_routes_to_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SERVICE_ACCOUNT_ENV_VAR, "ops_fake")
        _mark_sdk_available(monkeypatch, True)  # should be ignored
        _install_sdk_stub(monkeypatch)
        _fake_which(monkeypatch, {"/opt/op": "/opt/op"})

        result = detect_backend(binary="/opt/op")

        assert isinstance(result, CLIBackend)
        assert result._binary == "/opt/op"
        assert isinstance(result._auth, ServiceAccountAuth)
        assert result._auth.token == "ops_fake"

    def test_token_sdk_installed_prefers_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SERVICE_ACCOUNT_ENV_VAR, "ops_fake")
        _mark_sdk_available(monkeypatch, True)
        _install_sdk_stub(monkeypatch)
        _forbid_which(monkeypatch)

        result = detect_backend()

        assert isinstance(result, _StubSDKBackend)
        assert isinstance(result.auth, ServiceAccountAuth)
        assert result.auth.token == "ops_fake"

    def test_token_no_sdk_falls_back_to_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SERVICE_ACCOUNT_ENV_VAR, "ops_fake")
        _mark_sdk_available(monkeypatch, False)
        _fake_which(monkeypatch, {"op": "/usr/bin/op"})

        result = detect_backend()

        assert isinstance(result, CLIBackend)
        assert result._binary == "/usr/bin/op"
        assert isinstance(result._auth, ServiceAccountAuth)

    def test_token_no_sdk_no_cli_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SERVICE_ACCOUNT_ENV_VAR, "ops_fake")
        _mark_sdk_available(monkeypatch, False)
        _fake_which(monkeypatch, {})

        with pytest.raises(OpError, match=r"\[sdk\] extra or the op CLI"):
            detect_backend()

    def test_no_token_explicit_binary_desktop_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SERVICE_ACCOUNT_ENV_VAR, raising=False)
        _fake_which(monkeypatch, {"/opt/op": "/opt/op"})

        result = detect_backend(binary="/opt/op")

        assert isinstance(result, CLIBackend)
        assert result._binary == "/opt/op"
        assert isinstance(result._auth, DesktopAuth)

    def test_no_token_cli_on_path_desktop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SERVICE_ACCOUNT_ENV_VAR, raising=False)
        _fake_which(monkeypatch, {"op": "/usr/bin/op"})

        result = detect_backend()

        assert isinstance(result, CLIBackend)
        assert result._binary == "/usr/bin/op"
        assert isinstance(result._auth, DesktopAuth)

    def test_no_token_no_cli_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SERVICE_ACCOUNT_ENV_VAR, raising=False)
        _fake_which(monkeypatch, {})

        with pytest.raises(OpError, match="op CLI for desktop auth"):
            detect_backend()


# -- Decision matrix: async -------------------------------------------------


class TestDetectAsyncBackendDecisionMatrix:
    def test_token_explicit_binary_routes_to_async_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SERVICE_ACCOUNT_ENV_VAR, "ops_fake")
        _mark_sdk_available(monkeypatch, True)
        _install_sdk_stub(monkeypatch)
        _fake_which(monkeypatch, {"/opt/op": "/opt/op"})

        result = detect_async_backend(binary="/opt/op")

        assert isinstance(result, AsyncCLIBackend)
        assert result._binary == "/opt/op"
        assert isinstance(result._auth, ServiceAccountAuth)

    def test_token_sdk_installed_prefers_async_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SERVICE_ACCOUNT_ENV_VAR, "ops_fake")
        _mark_sdk_available(monkeypatch, True)
        _install_sdk_stub(monkeypatch)
        _forbid_which(monkeypatch)

        result = detect_async_backend()

        assert isinstance(result, _StubAsyncSDKBackend)
        assert isinstance(result.auth, ServiceAccountAuth)

    def test_token_no_sdk_falls_back_to_async_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SERVICE_ACCOUNT_ENV_VAR, "ops_fake")
        _mark_sdk_available(monkeypatch, False)
        _fake_which(monkeypatch, {"op": "/usr/bin/op"})

        result = detect_async_backend()

        assert isinstance(result, AsyncCLIBackend)
        assert result._binary == "/usr/bin/op"
        assert isinstance(result._auth, ServiceAccountAuth)

    def test_token_no_sdk_no_cli_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SERVICE_ACCOUNT_ENV_VAR, "ops_fake")
        _mark_sdk_available(monkeypatch, False)
        _fake_which(monkeypatch, {})

        with pytest.raises(OpError, match=r"\[sdk\] extra or the op CLI"):
            detect_async_backend()

    def test_no_token_explicit_binary_async_desktop_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SERVICE_ACCOUNT_ENV_VAR, raising=False)
        _fake_which(monkeypatch, {"/opt/op": "/opt/op"})

        result = detect_async_backend(binary="/opt/op")

        assert isinstance(result, AsyncCLIBackend)
        assert isinstance(result._auth, DesktopAuth)

    def test_no_token_async_cli_on_path_desktop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SERVICE_ACCOUNT_ENV_VAR, raising=False)
        _fake_which(monkeypatch, {"op": "/usr/bin/op"})

        result = detect_async_backend()

        assert isinstance(result, AsyncCLIBackend)
        assert result._binary == "/usr/bin/op"
        assert isinstance(result._auth, DesktopAuth)

    def test_no_token_no_cli_async_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SERVICE_ACCOUNT_ENV_VAR, raising=False)
        _fake_which(monkeypatch, {})

        with pytest.raises(OpError, match="op CLI for desktop auth"):
            detect_async_backend()


# -- Optimization guarantees ------------------------------------------------


class TestNoDiskIOInOptimalPath:
    def test_sync_optimal_path_avoids_which(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SERVICE_ACCOUNT_ENV_VAR, "ops_fake")
        _mark_sdk_available(monkeypatch, True)
        _install_sdk_stub(monkeypatch)
        _forbid_which(monkeypatch)

        detect_backend()

    def test_async_optimal_path_avoids_which(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SERVICE_ACCOUNT_ENV_VAR, "ops_fake")
        _mark_sdk_available(monkeypatch, True)
        _install_sdk_stub(monkeypatch)
        _forbid_which(monkeypatch)

        detect_async_backend()


class TestExplicitBinaryAlwaysCLI:
    def test_explicit_binary_overrides_sdk_preference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SERVICE_ACCOUNT_ENV_VAR, "ops_fake")
        _mark_sdk_available(monkeypatch, True)
        _install_sdk_stub(monkeypatch)
        _fake_which(monkeypatch, {"/opt/op": "/opt/op"})

        result = detect_backend(binary="/opt/op")

        assert isinstance(result, CLIBackend)
        assert not isinstance(result, _StubSDKBackend)

    def test_explicit_unresolvable_binary_hard_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SERVICE_ACCOUNT_ENV_VAR, "ops_fake")
        _mark_sdk_available(monkeypatch, True)
        _install_sdk_stub(monkeypatch)
        _fake_which(monkeypatch, {})

        with pytest.raises(OpError, match="not found or not executable"):
            detect_backend(binary="nonexistent")


def test_module_exports_detect_functions() -> None:
    from op_core.backends import detect_async_backend as reexp_async
    from op_core.backends import detect_backend as reexp_sync

    assert reexp_sync is detect_module.detect_backend
    assert reexp_async is detect_module.detect_async_backend
