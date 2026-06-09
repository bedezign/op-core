"""Tests for :mod:`op_core.cli.envfile`.

Two kinds of tests live here:

* **Contract tests** — pin the python-dotenv behaviours ``op-env`` relies on
  (quote stripping, ``export`` prefix, ``#`` comments, ``=`` in values). These
  break first if a dotenv upgrade changes parsing in a way that matters to us.
* **Wrapper tests** — cover the logic this module adds on top of dotenv: the
  malformed-line ``None`` → :class:`EnvFileError` translation, missing-file
  handling, and the missing-``[cli]``-extra hint.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from op_core.cli import envfile
from op_core.cli.envfile import EnvFileError, expand_variables, load_env_file, parse_env


class TestDotenvContract:
    def test_simple_key_value(self) -> None:
        assert parse_env("FOO=bar") == {"FOO": "bar"}

    def test_export_prefix_stripped(self) -> None:
        assert parse_env("export FOO=bar") == {"FOO": "bar"}

    def test_double_quotes_stripped(self) -> None:
        assert parse_env('FOO="bar"') == {"FOO": "bar"}

    def test_single_quotes_stripped(self) -> None:
        assert parse_env("FOO='bar'") == {"FOO": "bar"}

    def test_full_line_comment_ignored(self) -> None:
        assert parse_env("# a comment\nFOO=bar") == {"FOO": "bar"}

    def test_blank_lines_ignored(self) -> None:
        assert parse_env("\n\nA=1\n\nB=2\n") == {"A": "1", "B": "2"}

    def test_equals_in_value_preserved(self) -> None:
        assert parse_env("URL=a=b=c") == {"URL": "a=b=c"}

    def test_empty_value_is_empty_string(self) -> None:
        assert parse_env("FOO=") == {"FOO": ""}

    def test_op_reference_value(self) -> None:
        assert parse_env("TOKEN=op://Vault/Item/field") == {"TOKEN": "op://Vault/Item/field"}

    def test_op_chain_value(self) -> None:
        assert parse_env("TOKEN=op://V/I/a||op://V/I/b||literal") == {
            "TOKEN": "op://V/I/a||op://V/I/b||literal",
        }

    def test_quoted_value_protects_hash(self) -> None:
        # A '#' inside a quoted value is part of the value, not a comment.
        assert parse_env("FOO='a # b'") == {"FOO": "a # b"}

    def test_parse_is_raw_no_interpolation(self) -> None:
        # parse loads raw — ${A} survives verbatim. Interpolation is a separate,
        # post-merge step (expand_variables), not done at parse time.
        assert parse_env("A=1\nB=${A}") == {"A": "1", "B": "${A}"}


class TestParseEnvEmpty:
    def test_empty_text_yields_empty_dict(self) -> None:
        assert parse_env("") == {}

    def test_unicode_value_preserved(self) -> None:
        assert parse_env("NAME=café-π") == {"NAME": "café-π"}


class TestMalformedLines:
    def test_bare_key_raises(self) -> None:
        with pytest.raises(EnvFileError):
            parse_env("GARBAGE")

    def test_error_names_the_key(self) -> None:
        with pytest.raises(EnvFileError) as exc:
            parse_env("NEEDS_VALUE")
        assert "NEEDS_VALUE" in str(exc.value)

    def test_export_without_assignment_raises(self) -> None:
        with pytest.raises(EnvFileError):
            parse_env("export FOO")

    def test_source_name_in_error(self) -> None:
        with pytest.raises(EnvFileError) as exc:
            parse_env("GARBAGE", source="app.env")
        assert "app.env" in str(exc.value)


class TestLoadEnvFile:
    def test_reads_and_parses_file(self, tmp_path: Path) -> None:
        p = tmp_path / "app.env"
        p.write_text("FOO=bar\n# comment\nexport BAZ=qux\n", encoding="utf-8")
        assert load_env_file(p) == {"FOO": "bar", "BAZ": "qux"}

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(EnvFileError):
            load_env_file(tmp_path / "does-not-exist.env")

    def test_missing_file_message_includes_path(self, tmp_path: Path) -> None:
        target = tmp_path / "nope.env"
        with pytest.raises(EnvFileError) as exc:
            load_env_file(target)
        assert "nope.env" in str(exc.value)

    def test_malformed_file_message_includes_path(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.env"
        p.write_text("GARBAGE\n", encoding="utf-8")
        with pytest.raises(EnvFileError) as exc:
            load_env_file(p)
        assert "bad.env" in str(exc.value)

    def test_accepts_str_path(self, tmp_path: Path) -> None:
        p = tmp_path / "app.env"
        p.write_text("FOO=bar\n", encoding="utf-8")
        assert load_env_file(str(p)) == {"FOO": "bar"}

    def test_directory_is_not_a_file(self, tmp_path: Path) -> None:
        with pytest.raises(EnvFileError):
            load_env_file(tmp_path)


class TestExpandVariables:
    def test_expands_introduced_from_source(self) -> None:
        out = expand_variables({"VENV": "${BASE}/python"}, introduced={"VENV"}, source={"BASE": "/opt/app"})
        assert out == {"VENV": "/opt/app/python"}

    def test_non_introduced_value_passes_through(self) -> None:
        # An inherited value containing ${...} must NOT be expanded; only file keys are.
        out = expand_variables({"INHERITED": "${X}", "F": "${X}"}, introduced={"F"}, source={"X": "v"})
        assert out["INHERITED"] == "${X}"
        assert out["F"] == "v"

    def test_self_reference_reads_source_not_itself(self) -> None:
        # PATH=${PATH}/new must read the inherited PATH, not loop on its own value.
        out = expand_variables({"PATH": "${PATH}/new"}, introduced={"PATH"}, source={"PATH": "/usr/bin"})
        assert out == {"PATH": "/usr/bin/new"}

    def test_empty_source_expands_to_empty(self) -> None:
        out = expand_variables({"X": "${PATH}/y"}, introduced={"X"}, source={})
        assert out == {"X": "/y"}

    def test_no_implicit_os_environ_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A real process env var must NOT leak in when the source is empty.
        monkeypatch.setenv("OPC_LEAK_TEST", "leaked")
        out = expand_variables({"X": "${OPC_LEAK_TEST}"}, introduced={"X"}, source={})
        assert out == {"X": ""}

    def test_cross_var_earlier_in_order(self) -> None:
        out = expand_variables({"A": "1", "B": "${A}/x"}, introduced={"A", "B"}, source={})
        assert out == {"A": "1", "B": "1/x"}

    def test_default_syntax(self) -> None:
        out = expand_variables({"X": "${MISSING:-fallback}"}, introduced={"X"}, source={})
        assert out == {"X": "fallback"}

    def test_forward_reference_resolves_empty(self) -> None:
        # Single forward pass: ZZ references YY which comes later -> empty.
        out = expand_variables({"ZZ": "${YY}", "YY": "1"}, introduced={"ZZ", "YY"}, source={})
        assert out == {"ZZ": "", "YY": "1"}


class TestMissingExtra:
    def test_missing_dotenv_gives_actionable_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Poison the import so `from dotenv import dotenv_values` fails as it would
        # on a base install without the [cli] extra.
        monkeypatch.setitem(sys.modules, "dotenv", None)
        with pytest.raises(EnvFileError) as exc:
            envfile._dotenv_values()
        assert "op-core[cli]" in str(exc.value)
