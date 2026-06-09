"""Tests for :mod:`op_core.cli.emit` — env/json output formatting."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from op_core.cli.emit import format_env, format_json

# ---------------------------------------------------------------------------
# format_env — shell-safe KEY='value' lines
# ---------------------------------------------------------------------------


class TestFormatEnv:
    def test_single_pair(self) -> None:
        assert format_env({"FOO": "bar"}) == "FOO='bar'"

    def test_keys_sorted(self) -> None:
        assert format_env({"B": "2", "A": "1"}) == "A='1'\nB='2'"

    def test_empty_mapping_is_empty_string(self) -> None:
        assert format_env({}) == ""

    def test_empty_value(self) -> None:
        assert format_env({"FOO": ""}) == "FOO=''"

    def test_single_quote_escaped(self) -> None:
        # POSIX idiom: close quote, escaped literal quote, reopen quote.
        assert format_env({"FOO": "it's"}) == "FOO='it'\\''s'"

    def test_value_with_spaces(self) -> None:
        assert format_env({"FOO": "a b c"}) == "FOO='a b c'"


# ---------------------------------------------------------------------------
# format_env — round-trips through `eval` without expansion or injection
# ---------------------------------------------------------------------------

bash_required = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def _eval_and_read(formatted: str, key: str) -> str:
    script = f'set -a; eval "$1"; set +a; printf "%s" "${key}"'
    proc = subprocess.run(
        ["bash", "-c", script, "_", formatted],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


@bash_required
class TestFormatEnvEvalRoundTrip:
    @pytest.mark.parametrize(
        "value",
        [
            "plain",
            "with spaces",
            "it's a quote",
            "trailing#hash",
            "semi;colon&amp",
            "tab\tseparated",
            "newline\ninside",
            "café-π",
            "op://Vault/Item/field",
        ],
    )
    def test_value_survives_eval(self, value: str) -> None:
        assert _eval_and_read(format_env({"K": value}), "K") == value

    def test_command_substitution_not_executed(self) -> None:
        # The whole point of single-quoting: $(...) and backticks stay literal.
        malicious = "$(touch /tmp/op_core_pwned)`echo no`"
        assert _eval_and_read(format_env({"K": malicious}), "K") == malicious

    def test_multiple_keys_all_set(self) -> None:
        formatted = format_env({"A": "1", "B": "two's", "C": "$PATH"})
        assert _eval_and_read(formatted, "A") == "1"
        assert _eval_and_read(formatted, "B") == "two's"
        assert _eval_and_read(formatted, "C") == "$PATH"


# ---------------------------------------------------------------------------
# format_json
# ---------------------------------------------------------------------------


class TestFormatJson:
    def test_round_trips(self) -> None:
        env = {"A": "1", "B": "two", "TOKEN": "s3cr3t"}
        assert json.loads(format_json(env)) == env

    def test_empty_mapping(self) -> None:
        assert json.loads(format_json({})) == {}

    def test_keys_sorted(self) -> None:
        assert list(json.loads(format_json({"Z": "1", "A": "2"})).keys()) == ["A", "Z"]

    def test_unicode_preserved(self) -> None:
        env = {"NAME": "café-π"}
        assert json.loads(format_json(env)) == env

    def test_special_chars_preserved(self) -> None:
        env = {"K": 'line1\nline2\t"quoted"'}
        assert json.loads(format_json(env)) == env
