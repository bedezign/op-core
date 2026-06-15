"""Tests for :mod:`op_core.cli.main` — the ``op-env`` command.

Orchestration, precedence, resolution, and secret-non-leakage are tested
in-process via :func:`op_core.cli.main.run` with an injected
``InMemoryBackend``, a controlled ``environ``, and a stub ``exec_fn`` (a real
``os.execvpe`` would replace the test process). Two end-to-end tests drive the
real ``python -m op_core.cli`` entry point with plain (non-secret) values to
prove the exec and export paths work for real.

Caching semantics (design 5.3):
- Default (no ``--ttl``, or ``--ttl 0``): bare backend, no disk writes.
- ``--ttl N`` with N>0: one-writer ``ResolverStack`` over ``FileWriterLayer``.
- ``--no-cache`` is an unknown flag; argparse exits non-zero.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from op_core import InMemoryBackend
from op_core.cli.main import run


class ExecRecorder:
    """Stand-in for os.execvpe that records its call instead of replacing the process."""

    def __init__(self) -> None:
        self.file: str | None = None
        self.argv: list[str] | None = None
        self.env: dict[str, str] | None = None

    def __call__(self, file: str, argv: object, env: object) -> None:
        self.file = file
        self.argv = list(argv)  # type: ignore[arg-type]
        self.env = dict(env)  # type: ignore[arg-type]


def _backend(**refs: str) -> InMemoryBackend:
    return InMemoryBackend(refs=refs)


# ---------------------------------------------------------------------------
# exec mode
# ---------------------------------------------------------------------------


class TestExec:
    def test_resolved_value_reaches_child_env(self, tmp_path: Path) -> None:
        env_file = tmp_path / "app.env"
        env_file.write_text("TOKEN=op://V/I/tok\n", encoding="utf-8")
        rec = ExecRecorder()
        code = run(
            ["exec", "--env-file", str(env_file), "--", "mytool", "--flag"],
            backend=_backend(**{"op://V/I/tok": "s3cr3t"}),
            environ={},
            exec_fn=rec,
        )
        assert code == 0
        assert rec.env is not None
        assert rec.env["TOKEN"] == "s3cr3t"

    def test_child_argv_passed_through(self, tmp_path: Path) -> None:
        rec = ExecRecorder()
        run(
            ["exec", "--", "mytool", "--flag", "pos"],
            backend=_backend(),
            environ={},
            exec_fn=rec,
        )
        assert rec.file == "mytool"
        assert rec.argv == ["mytool", "--flag", "pos"]

    def test_inherited_env_excluded_by_default(self) -> None:
        rec = ExecRecorder()
        run(
            ["exec", "--", "tool"],
            backend=_backend(),
            environ={"EXISTING": "value"},
            exec_fn=rec,
        )
        assert rec.env == {}  # file-only by default — the inherited env is not passed through

    def test_env_inherited_with_flag(self) -> None:
        rec = ExecRecorder()
        run(
            ["exec", "--inherit-env", "--", "tool"],
            backend=_backend(),
            environ={"EXISTING": "value"},
            exec_fn=rec,
        )
        assert rec.env is not None
        assert rec.env["EXISTING"] == "value"

    def test_exec_prints_no_secret(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        env_file = tmp_path / "app.env"
        env_file.write_text("TOKEN=op://V/I/tok\n", encoding="utf-8")
        run(
            ["exec", "--env-file", str(env_file), "--", "tool"],
            backend=_backend(**{"op://V/I/tok": "TOP-SECRET"}),
            environ={},
            exec_fn=ExecRecorder(),
        )
        captured = capsys.readouterr()
        assert "TOP-SECRET" not in captured.out
        assert "TOP-SECRET" not in captured.err

    def test_missing_command_after_dash_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = run(["exec", "--"], backend=_backend(), environ={})
        assert code == 2
        assert "exec requires a command" in capsys.readouterr().err

    def test_unresolved_reference_fails_loudly(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        env_file = tmp_path / "app.env"
        env_file.write_text("TOKEN=op://V/I/missing\n", encoding="utf-8")
        rec = ExecRecorder()
        code = run(
            ["exec", "--env-file", str(env_file), "--", "tool"],
            backend=_backend(),
            environ={},
            exec_fn=rec,
        )
        assert code == 2
        assert "op://V/I/missing" in capsys.readouterr().err
        assert rec.file is None  # never reached exec


# ---------------------------------------------------------------------------
# export mode
# ---------------------------------------------------------------------------


class TestExport:
    def test_export_env_format(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        env_file = tmp_path / "app.env"
        env_file.write_text("TOKEN=op://V/I/tok\nHOST=localhost\n", encoding="utf-8")
        code = run(
            ["export", "--env-file", str(env_file)],
            backend=_backend(**{"op://V/I/tok": "abc"}),
            environ={},
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "TOKEN='abc'" in out
        assert "HOST='localhost'" in out

    def test_export_json_format(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        env_file = tmp_path / "app.env"
        env_file.write_text("TOKEN=op://V/I/tok\n", encoding="utf-8")
        run(
            ["export", "--env-file", str(env_file), "--format", "json"],
            backend=_backend(**{"op://V/I/tok": "abc"}),
            environ={},
        )
        assert json.loads(capsys.readouterr().out) == {"TOKEN": "abc"}

    def test_export_emits_only_env_file_keys(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        env_file = tmp_path / "app.env"
        env_file.write_text("APP_KEY=value\n", encoding="utf-8")
        run(
            ["export", "--env-file", str(env_file), "--format", "json"],
            backend=_backend(),
            environ={"UNRELATED": "should-not-appear", "PATH": "/usr/bin"},
        )
        emitted = json.loads(capsys.readouterr().out)
        assert emitted == {"APP_KEY": "value"}

    def test_export_rejects_trailing_command(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = run(["export", "--", "oops"], backend=_backend(), environ={})
        assert code == 2
        assert "export does not take a command" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Error surfacing
# ---------------------------------------------------------------------------


class TestErrorSurfacing:
    def test_exec_command_not_found(self, capsys: pytest.CaptureFixture[str]) -> None:
        def boom(file: str, argv: object, env: object) -> None:
            raise FileNotFoundError(f"no such file: {file}")

        code = run(
            ["exec", "--", "nonexistent-xyz"],
            backend=_backend(),
            environ={},
            exec_fn=boom,
        )
        assert code == 2
        assert "cannot exec" in capsys.readouterr().err

    def test_backend_error_surfaces_without_traceback(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from op_core.exceptions import OpAuthError

        class FailingBackend(InMemoryBackend):
            def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
                raise OpAuthError("not signed in")

        env_file = tmp_path / "app.env"
        env_file.write_text("TOKEN=op://V/I/tok\n", encoding="utf-8")
        code = run(
            ["export", "--env-file", str(env_file)],
            backend=FailingBackend(),
            environ={},
        )
        assert code == 2
        assert "op-env:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Precedence, require, no-refs
# ---------------------------------------------------------------------------


class TestComposition:
    def test_files_win_over_inherited_env(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        env_file = tmp_path / "app.env"
        env_file.write_text("SHARED=from-file\n", encoding="utf-8")
        run(
            ["export", "--inherit-env", "--env-file", str(env_file), "--format", "json"],
            backend=_backend(),
            environ={"SHARED": "from-proc"},
        )
        assert json.loads(capsys.readouterr().out)["SHARED"] == "from-file"

    def test_inherited_env_ignored_without_flag(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        env_file = tmp_path / "app.env"
        env_file.write_text("SHARED=from-file\n", encoding="utf-8")
        run(
            ["export", "--env-file", str(env_file), "--format", "json"],
            backend=_backend(),
            environ={"SHARED": "from-proc", "OTHER": "ambient"},
        )
        # Only the file key, and its file value — the inherited environment is ignored.
        assert json.loads(capsys.readouterr().out) == {"SHARED": "from-file"}

    def test_multi_file_layering(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        defaults = tmp_path / "defaults.env"
        defaults.write_text("PORT=1\nNAME=base\n", encoding="utf-8")
        tool = tmp_path / "tool.env"
        tool.write_text("PORT=2\n", encoding="utf-8")
        run(
            [
                "export",
                "--env-file",
                str(defaults),
                "--env-file",
                str(tool),
                "--override",
                "--format",
                "json",
            ],
            backend=_backend(),
            environ={},
        )
        emitted = json.loads(capsys.readouterr().out)
        assert emitted == {"PORT": "2", "NAME": "base"}

    def test_require_missing_key_fails(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        env_file = tmp_path / "app.env"
        env_file.write_text("PRESENT=x\n", encoding="utf-8")
        code = run(
            ["export", "--env-file", str(env_file), "--require", "ABSENT"],
            backend=_backend(),
            environ={},
        )
        assert code == 2
        assert "ABSENT" in capsys.readouterr().err

    def test_require_present_key_passes(self, tmp_path: Path) -> None:
        env_file = tmp_path / "app.env"
        env_file.write_text("PRESENT=x\n", encoding="utf-8")
        code = run(
            ["export", "--env-file", str(env_file), "--require", "PRESENT"],
            backend=_backend(),
            environ={},
        )
        assert code == 0

    def test_no_references_needs_no_backend(self, tmp_path: Path) -> None:
        # backend=None and no op:// refs: must not attempt detect_backend().
        env_file = tmp_path / "app.env"
        env_file.write_text("PLAIN=value\n", encoding="utf-8")
        rec = ExecRecorder()
        code = run(
            ["exec", "--env-file", str(env_file), "--", "tool"],
            environ={},
            exec_fn=rec,
        )
        assert code == 0
        assert rec.env is not None
        assert rec.env["PLAIN"] == "value"

    def test_bad_env_file_fails_loudly(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = run(
            ["export", "--env-file", str(tmp_path / "nonexistent.env")],
            backend=_backend(),
            environ={},
        )
        assert code == 2
        assert "nonexistent.env" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# ${VAR} interpolation
# ---------------------------------------------------------------------------


class TestInterpolation:
    def test_no_interpolation_source_without_inherit_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Default (no --inherit-env): the environment is not a source, so ${RUNTIME_DIR} -> empty.
        monkeypatch.setenv("RUNTIME_DIR", "/opt/run")
        env_file = tmp_path / "app.env"
        env_file.write_text("VENV=${RUNTIME_DIR}/python\n", encoding="utf-8")
        run(
            ["export", "--env-file", str(env_file), "--format", "json"],
            backend=_backend(),
            environ={},
        )
        assert json.loads(capsys.readouterr().out) == {"VENV": "/python"}

    def test_interpolates_from_inherited_env(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        env_file = tmp_path / "app.env"
        env_file.write_text("VENV=${RUNTIME_DIR}/python\n", encoding="utf-8")
        run(
            ["export", "--inherit-env", "--env-file", str(env_file), "--format", "json"],
            backend=_backend(),
            environ={"RUNTIME_DIR": "/opt/run"},
        )
        assert json.loads(capsys.readouterr().out) == {"VENV": "/opt/run/python"}

    def test_recuperating_path(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        env_file = tmp_path / "app.env"
        env_file.write_text("PATH=${PATH}/extra\n", encoding="utf-8")
        run(
            ["export", "--inherit-env", "--env-file", str(env_file), "--format", "json"],
            backend=_backend(),
            environ={"PATH": "/usr/bin:/bin"},
        )
        assert json.loads(capsys.readouterr().out) == {"PATH": "/usr/bin:/bin/extra"}

    def test_cross_file_interpolation(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        defaults = tmp_path / "defaults.env"
        defaults.write_text("BASE=/opt/app\n", encoding="utf-8")
        tool = tmp_path / "tool.env"
        tool.write_text("LOG=${BASE}/logs\n", encoding="utf-8")
        run(
            ["export", "--env-file", str(defaults), "--env-file", str(tool), "--format", "json"],
            backend=_backend(),
            environ={},
        )
        assert json.loads(capsys.readouterr().out)["LOG"] == "/opt/app/logs"

    def test_interpolation_into_op_reference(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        env_file = tmp_path / "app.env"
        env_file.write_text("TOKEN=op://${VAULT}/Item/tok\n", encoding="utf-8")
        run(
            ["export", "--inherit-env", "--env-file", str(env_file), "--format", "json"],
            backend=_backend(**{"op://Personal/Item/tok": "secret"}),
            environ={"VAULT": "Personal"},
        )
        assert json.loads(capsys.readouterr().out) == {"TOKEN": "secret"}

    def test_resolved_secret_with_braces_not_reinterpolated(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A secret whose value contains ${...} or $ is passed through verbatim — never
        # fed back through interpolation (interpolate happens before resolution).
        env_file = tmp_path / "app.env"
        env_file.write_text("PW=op://V/I/pw\n", encoding="utf-8")
        run(
            ["export", "--inherit-env", "--env-file", str(env_file), "--format", "json"],
            backend=_backend(**{"op://V/I/pw": "pa$$w0rd${HOME}"}),
            environ={"HOME": "/home/x"},
        )
        assert json.loads(capsys.readouterr().out) == {"PW": "pa$$w0rd${HOME}"}


# ---------------------------------------------------------------------------
# --keep / --drop inherited-env filtering
# ---------------------------------------------------------------------------


class TestInheritFilter:
    def test_keep_allowlist(self, capsys: pytest.CaptureFixture[str]) -> None:
        rec = ExecRecorder()
        run(
            ["exec", "--inherit-env", "--keep", "PATH", "--", "tool"],
            backend=_backend(),
            environ={"PATH": "/bin", "SECRET": "x"},
            exec_fn=rec,
        )
        assert rec.env == {"PATH": "/bin"}

    def test_drop_denylist(self, capsys: pytest.CaptureFixture[str]) -> None:
        rec = ExecRecorder()
        run(
            ["exec", "--inherit-env", "--drop", "SECRET", "--", "tool"],
            backend=_backend(),
            environ={"PATH": "/bin", "SECRET": "x"},
            exec_fn=rec,
        )
        assert rec.env == {"PATH": "/bin"}

    def test_keep_then_drop(self, capsys: pytest.CaptureFixture[str]) -> None:
        rec = ExecRecorder()
        run(
            ["exec", "--inherit-env", "--keep", "A", "--keep", "B", "--drop", "B", "--", "tool"],
            backend=_backend(),
            environ={"A": "1", "B": "2", "C": "3"},
            exec_fn=rec,
        )
        assert rec.env == {"A": "1"}

    def test_dropped_var_cannot_be_exfiltrated_via_interpolation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The whole point of filtering at the source: a dropped secret is not an
        # interpolation source, so LEAK=${AWS_KEY} cannot smuggle it out.
        env_file = tmp_path / "app.env"
        env_file.write_text("LEAK=${AWS_KEY}\n", encoding="utf-8")
        run(
            [
                "export",
                "--inherit-env",
                "--drop",
                "AWS_KEY",
                "--env-file",
                str(env_file),
                "--format",
                "json",
            ],
            backend=_backend(),
            environ={"AWS_KEY": "supersecret"},
        )
        out = capsys.readouterr().out
        assert json.loads(out) == {"LEAK": ""}
        assert "supersecret" not in out

    def test_keep_excludes_var_from_interpolation(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        env_file = tmp_path / "app.env"
        env_file.write_text("LEAK=${AWS_KEY}\n", encoding="utf-8")
        run(
            [
                "export",
                "--inherit-env",
                "--keep",
                "PATH",
                "--env-file",
                str(env_file),
                "--format",
                "json",
            ],
            backend=_backend(),
            environ={"PATH": "/bin", "AWS_KEY": "supersecret"},
        )
        out = capsys.readouterr().out
        assert json.loads(out) == {"LEAK": ""}
        assert "supersecret" not in out

    def test_keep_without_inherit_env_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = run(["export", "--keep", "PATH"], backend=_backend(), environ={})
        assert code == 2
        assert "require --inherit-env" in capsys.readouterr().err

    def test_drop_without_inherit_env_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = run(["export", "--drop", "SECRET"], backend=_backend(), environ={})
        assert code == 2
        assert "require --inherit-env" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --ascend wiring
# ---------------------------------------------------------------------------


class TestAscend:
    def test_collects_ancestor_env_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        home = tmp_path / "home"
        (home / "proj" / "sub").mkdir(parents=True)
        (home / ".env").write_text("FROM_HOME=h\n", encoding="utf-8")
        (home / "proj" / ".env").write_text("FROM_PROJ=p\n", encoding="utf-8")
        (home / "proj" / "sub" / ".env").write_text("FROM_SUB=s\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(home / "proj" / "sub")
        run(["export", "--ascend", "--format", "json"], backend=_backend(), environ={})
        assert json.loads(capsys.readouterr().out) == {
            "FROM_HOME": "h",
            "FROM_PROJ": "p",
            "FROM_SUB": "s",
        }

    def test_nearest_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        home = tmp_path / "home"
        (home / "sub").mkdir(parents=True)
        (home / ".env").write_text("SHARED=far\n", encoding="utf-8")
        (home / "sub" / ".env").write_text("SHARED=near\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(home / "sub")
        run(["export", "--ascend", "--format", "json"], backend=_backend(), environ={})
        assert json.loads(capsys.readouterr().out)["SHARED"] == "near"

    def test_ascend_until_without_ascend_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = run(["export", "--ascend-until", "/tmp"], backend=_backend(), environ={})
        assert code == 2
        assert "require --ascend" in capsys.readouterr().err

    def test_env_file_name_without_ascend_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = run(["export", "--env-file-name", ".env"], backend=_backend(), environ={})
        assert code == 2
        assert "require --ascend" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Caching wired through run() (cross-invocation)
# ---------------------------------------------------------------------------


class CountingBackend(InMemoryBackend):
    def __init__(self, **refs: str) -> None:
        super().__init__(refs=refs)
        self.read_count = 0

    def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
        self.read_count += 1
        return super().read(reference, default_value=default_value, online=online)


class TestCachingWiring:
    # --- default (ttl=0): no caching ---

    def test_default_no_cache_file_created(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Design 5.3: ttl=0 (the default) returns the bare backend; no cache file is written."""
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        env_file = tmp_path / "app.env"
        env_file.write_text("TOKEN=op://V/I/tok\n", encoding="utf-8")

        run(["export", "--env-file", str(env_file)], backend=CountingBackend(**{"op://V/I/tok": "s"}), environ={})

        cache_dir = tmp_path / "op-core"
        assert not cache_dir.exists(), "No cache directory should be created when ttl=0"

    def test_default_second_run_re_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Design 5.3: without --ttl, every run resolves through the backend (no persistence)."""
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        env_file = tmp_path / "app.env"
        env_file.write_text("TOKEN=op://V/I/tok\n", encoding="utf-8")

        first = CountingBackend(**{"op://V/I/tok": "secret"})
        run(["export", "--env-file", str(env_file)], backend=first, environ={})
        assert first.read_count == 1

        second = CountingBackend(**{"op://V/I/tok": "secret"})
        run(["export", "--env-file", str(env_file)], backend=second, environ={})
        assert second.read_count == 1  # re-resolved — no cache in the default

    def test_explicit_ttl_zero_no_cache_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Design 5.3: --ttl 0 is the same as the default — bare backend, no disk writes."""
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        env_file = tmp_path / "app.env"
        env_file.write_text("TOKEN=op://V/I/tok\n", encoding="utf-8")

        run(
            ["export", "--ttl", "0", "--env-file", str(env_file)],
            backend=CountingBackend(**{"op://V/I/tok": "s"}),
            environ={},
        )

        cache_dir = tmp_path / "op-core"
        assert not cache_dir.exists(), "No cache directory should be created when --ttl 0"

    # --- --ttl N>0: one-writer ResolverStack ---

    def test_ttl_positive_second_run_uses_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Design 5.3: --ttl N>0 builds a one-writer ResolverStack; second run is served from disk."""
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        env_file = tmp_path / "app.env"
        env_file.write_text("TOKEN=op://V/I/tok\n", encoding="utf-8")

        first = CountingBackend(**{"op://V/I/tok": "secret"})
        run(["export", "--ttl", "300", "--env-file", str(env_file)], backend=first, environ={})
        assert first.read_count == 1

        second = CountingBackend(**{"op://V/I/tok": "secret"})
        run(["export", "--ttl", "300", "--env-file", str(env_file)], backend=second, environ={})
        assert second.read_count == 0  # served from the on-disk cache

    def test_ttl_positive_all_runs_share_one_cache_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Different reference sets land as sets in a single cache file, not one file each."""
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        file_a = tmp_path / "a.env"
        file_a.write_text("TOKEN=op://V/I/a\n", encoding="utf-8")
        file_b = tmp_path / "b.env"
        file_b.write_text("TOKEN=op://V/I/b\n", encoding="utf-8")

        run(
            ["export", "--ttl", "300", "--env-file", str(file_a)],
            backend=CountingBackend(**{"op://V/I/a": "1"}),
            environ={},
        )
        run(
            ["export", "--ttl", "300", "--env-file", str(file_b)],
            backend=CountingBackend(**{"op://V/I/b": "2"}),
            environ={},
        )

        cache_dir = tmp_path / "op-core"
        cache_files = [p for p in cache_dir.iterdir() if not p.name.endswith(".lock")]
        assert [p.name for p in cache_files] == ["cache.bin"]

    # --- --no-cache is an unknown flag ---

    def test_no_cache_flag_is_unknown(self) -> None:
        """Design 9.4: --no-cache is removed outright; passing it is an argparse unknown-flag error."""
        with pytest.raises(SystemExit) as exc_info:
            run(["export", "--no-cache"], backend=_backend(), environ={})
        assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# End-to-end via the real entry point (plain values, no 1Password needed)
# ---------------------------------------------------------------------------


def _run_module(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "op_core.cli", *args],
        capture_output=True,
        text=True,
        **kwargs,  # type: ignore[arg-type]
    )


class TestEndToEnd:
    def test_exec_passes_env_to_real_child(self, tmp_path: Path) -> None:
        env_file = tmp_path / "plain.env"
        env_file.write_text("INJECTED=hello-child\n", encoding="utf-8")
        result = _run_module(
            [
                "exec",
                "--env-file",
                str(env_file),
                "--",
                sys.executable,
                "-c",
                "import os; print(os.environ['INJECTED'])",
            ]
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "hello-child"

    def test_export_json_via_real_entrypoint(self, tmp_path: Path) -> None:
        env_file = tmp_path / "plain.env"
        env_file.write_text("A=1\nB=two\n", encoding="utf-8")
        result = _run_module(["export", "--env-file", str(env_file), "--format", "json"])
        assert result.returncode == 0
        assert json.loads(result.stdout) == {"A": "1", "B": "two"}

    def test_export_env_round_trips_through_eval(self, tmp_path: Path) -> None:
        env_file = tmp_path / "plain.env"
        env_file.write_text("MSG=hi there\n", encoding="utf-8")
        export = _run_module(["export", "--env-file", str(env_file)])
        assert export.returncode == 0
        evaled = subprocess.run(
            ["bash", "-c", 'set -a; eval "$1"; set +a; printf "%s" "$MSG"', "_", export.stdout],
            capture_output=True,
            text=True,
            check=True,
        )
        assert evaled.stdout == "hi there"

    def test_no_subcommand_errors(self) -> None:
        result = _run_module([])
        assert result.returncode != 0

    def test_no_cache_flag_fails_via_real_entrypoint(self, tmp_path: Path) -> None:
        """Design 9.4: --no-cache must be an unknown-flag error at the CLI level."""
        env_file = tmp_path / "plain.env"
        env_file.write_text("A=1\n", encoding="utf-8")
        result = _run_module(["export", "--env-file", str(env_file), "--no-cache"])
        assert result.returncode != 0
