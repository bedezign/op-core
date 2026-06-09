"""Tests for :mod:`op_core.cli.compose`."""

from __future__ import annotations

import pytest

from op_core import InMemoryBackend, OnePassword
from op_core.cli.compose import (
    cache_bucket,
    check_required,
    compose_env,
    is_op_reference,
    reference_values,
    resolve_env,
)
from op_core.cli.errors import MissingKeysError, ResolutionError


def _op(**refs: str) -> OnePassword:
    return OnePassword(InMemoryBackend(refs=refs))


# ---------------------------------------------------------------------------
# is_op_reference
# ---------------------------------------------------------------------------


class TestIsOpReference:
    @pytest.mark.parametrize(
        "value",
        [
            "op://Vault/Item/field",
            "ops://Vault/Item/field",
            "op://V/I/a||op://V/I/b",
            "op://V/I/a||literal-default",
            "literal||op://V/I/b",
        ],
    )
    def test_references(self, value: str) -> None:
        assert is_op_reference(value) is True

    @pytest.mark.parametrize(
        "value",
        ["", "plain", "https://example.com", "a||b||c", "postgres://host/db"],
    )
    def test_non_references(self, value: str) -> None:
        assert is_op_reference(value) is False


# ---------------------------------------------------------------------------
# compose_env
# ---------------------------------------------------------------------------


class TestComposeEnv:
    def test_files_override_parent(self) -> None:
        result = compose_env({"K": "from-parent"}, [{"K": "from-file"}], override=False)
        assert result["K"] == "from-file"

    def test_parent_fills_gaps(self) -> None:
        result = compose_env({"A": "parent"}, [{"B": "file"}], override=False)
        assert result == {"A": "parent", "B": "file"}

    def test_first_file_wins_among_files_by_default(self) -> None:
        result = compose_env({}, [{"K": "first"}, {"K": "second"}], override=False)
        assert result["K"] == "first"

    def test_override_later_file_wins(self) -> None:
        result = compose_env({}, [{"K": "first"}, {"K": "second"}], override=True)
        assert result["K"] == "second"

    def test_empty_parent_yields_file_content(self) -> None:
        result = compose_env({}, [{"A": "1"}], override=False)
        assert result == {"A": "1"}

    def test_parent_not_aliased(self) -> None:
        parent = {"A": "1"}
        result = compose_env(parent, [], override=False)
        result["A"] = "mutated"
        assert parent["A"] == "1"


# ---------------------------------------------------------------------------
# cache_bucket
# ---------------------------------------------------------------------------


class TestCacheBucket:
    def test_deterministic(self) -> None:
        env = {"A": "op://V/I/a"}
        assert cache_bucket(env) == cache_bucket(dict(env))

    def test_order_and_key_name_independent(self) -> None:
        env1 = {"A": "op://V/I/a", "B": "op://V/I/b"}
        env2 = {"Y": "op://V/I/b", "X": "op://V/I/a"}  # same ref set, different keys/order
        assert cache_bucket(env1) == cache_bucket(env2)

    def test_different_ref_sets_differ(self) -> None:
        assert cache_bucket({"A": "op://V/I/a"}) != cache_bucket({"A": "op://V/I/c"})

    def test_non_reference_values_do_not_affect_bucket(self) -> None:
        assert cache_bucket({"A": "op://V/I/a", "B": "plain"}) == cache_bucket({"A": "op://V/I/a"})

    def test_no_references_is_stable(self) -> None:
        assert cache_bucket({"A": "plain", "B": "literal"}) == cache_bucket({})

    def test_reflects_precedence_outcome(self) -> None:
        # The bucket is computed from the *composed* env, so --override changing
        # which file's reference wins changes the bucket.
        files = [{"K": "op://V/I/first"}, {"K": "op://V/I/second"}]
        first_wins = cache_bucket(compose_env({}, files, override=False))
        last_wins = cache_bucket(compose_env({}, files, override=True))
        assert first_wins != last_wins

    def test_is_short_hex(self) -> None:
        bucket = cache_bucket({"A": "op://V/I/a"})
        assert len(bucket) == 16
        assert all(c in "0123456789abcdef" for c in bucket)


class TestReferenceValues:
    def test_sorted_and_deduped(self) -> None:
        env = {"A": "op://V/I/b", "B": "op://V/I/a", "C": "op://V/I/a", "D": "plain"}
        assert reference_values(env) == ["op://V/I/a", "op://V/I/b"]


# ---------------------------------------------------------------------------
# resolve_env
# ---------------------------------------------------------------------------


class TestResolveEnv:
    def test_resolves_reference(self) -> None:
        env = {"TOKEN": "op://V/I/tok"}
        out = resolve_env(env, _op(**{"op://V/I/tok": "s3cr3t"}))
        assert out == {"TOKEN": "s3cr3t"}

    def test_plain_values_pass_through(self) -> None:
        env = {"HOST": "db.internal", "PORT": "5432"}
        out = resolve_env(env, _op())
        assert out == env

    def test_chain_returns_first_hit(self) -> None:
        env = {"K": "op://V/I/missing||op://V/I/backup"}
        out = resolve_env(env, _op(**{"op://V/I/backup": "fallback-value"}))
        assert out == {"K": "fallback-value"}

    def test_chain_literal_fallback(self) -> None:
        env = {"K": "op://V/I/missing||plain-default"}
        out = resolve_env(env, _op())
        assert out == {"K": "plain-default"}

    def test_unresolved_reference_raises(self) -> None:
        with pytest.raises(ResolutionError):
            resolve_env({"K": "op://V/I/missing"}, _op())

    def test_error_names_reference_not_value(self) -> None:
        with pytest.raises(ResolutionError) as exc:
            resolve_env({"API_KEY": "op://V/I/missing"}, _op())
        message = str(exc.value)
        assert "op://V/I/missing" in message
        assert "API_KEY" in message

    def test_mixed_env(self) -> None:
        env = {"TOKEN": "op://V/I/tok", "HOST": "localhost"}
        out = resolve_env(env, _op(**{"op://V/I/tok": "abc"}))
        assert out == {"TOKEN": "abc", "HOST": "localhost"}


# ---------------------------------------------------------------------------
# check_required
# ---------------------------------------------------------------------------


class TestCheckRequired:
    def test_present_keys_pass(self) -> None:
        check_required({"A": "1", "B": "2"}, ["A", "B"])  # no raise

    def test_empty_require_list_passes(self) -> None:
        check_required({}, [])

    def test_missing_key_raises(self) -> None:
        with pytest.raises(MissingKeysError):
            check_required({"A": "1"}, ["A", "B"])

    def test_empty_value_counts_as_missing(self) -> None:
        with pytest.raises(MissingKeysError):
            check_required({"A": ""}, ["A"])

    def test_error_names_missing_keys(self) -> None:
        with pytest.raises(MissingKeysError) as exc:
            check_required({}, ["FOO", "BAR"])
        assert "FOO" in str(exc.value)
        assert "BAR" in str(exc.value)
