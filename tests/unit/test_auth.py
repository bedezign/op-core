"""Tests for the auth module."""

from __future__ import annotations

import dataclasses

import pytest

from op_core.auth import Auth, DesktopAuth, ServiceAccountAuth, detect_auth
from op_core.exceptions import OpAuthError


class TestServiceAccountAuth:
    def test_is_frozen(self):
        auth = ServiceAccountAuth(token="ops_abc")
        with pytest.raises(dataclasses.FrozenInstanceError):
            auth.token = "other"  # type: ignore[misc]

    def test_equality(self):
        assert ServiceAccountAuth(token="t") == ServiceAccountAuth(token="t")
        assert ServiceAccountAuth(token="a") != ServiceAccountAuth(token="b")

    def test_from_env_reads_default_var(self, monkeypatch):
        monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_xyz")
        assert ServiceAccountAuth.from_env() == ServiceAccountAuth(token="ops_xyz")

    def test_from_env_reads_custom_var(self, monkeypatch):
        monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
        monkeypatch.setenv("MY_OP_TOKEN", "ops_custom")
        assert ServiceAccountAuth.from_env(var="MY_OP_TOKEN") == ServiceAccountAuth(token="ops_custom")

    def test_from_env_missing_raises(self, monkeypatch):
        monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
        with pytest.raises(OpAuthError, match="OP_SERVICE_ACCOUNT_TOKEN"):
            ServiceAccountAuth.from_env()

    def test_from_env_empty_raises(self, monkeypatch):
        monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "")
        with pytest.raises(OpAuthError, match="OP_SERVICE_ACCOUNT_TOKEN"):
            ServiceAccountAuth.from_env()

    def test_from_env_custom_var_in_error_message(self, monkeypatch):
        monkeypatch.delenv("NOPE", raising=False)
        with pytest.raises(OpAuthError, match="NOPE"):
            ServiceAccountAuth.from_env(var="NOPE")


class TestDesktopAuth:
    def test_is_frozen(self):
        auth = DesktopAuth()
        with pytest.raises(dataclasses.FrozenInstanceError):
            auth.whatever = "x"  # type: ignore[attr-defined]

    def test_instances_equal(self):
        assert DesktopAuth() == DesktopAuth()


class TestDetectAuth:
    def test_returns_service_account_when_env_set(self, monkeypatch):
        monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_env")
        result = detect_auth()
        assert result == ServiceAccountAuth(token="ops_env")

    def test_returns_desktop_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
        assert detect_auth() == DesktopAuth()

    def test_returns_desktop_when_env_empty(self, monkeypatch):
        monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "")
        assert detect_auth() == DesktopAuth()


class TestAuthUnion:
    def test_match_dispatch(self):
        def describe(auth: Auth) -> str:
            match auth:
                case ServiceAccountAuth(token=t):
                    return f"service:{t}"
                case DesktopAuth():
                    return "desktop"

        assert describe(ServiceAccountAuth(token="abc")) == "service:abc"
        assert describe(DesktopAuth()) == "desktop"
