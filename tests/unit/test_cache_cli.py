"""Tests for the ``op-cache`` console command (design section 7).

``op-cache`` is a standalone, stdlib-only CLI with three subcommands:

* ``clear``   — cold, no auth: delete the whole cache file.
* ``info``    — cold, no auth: print metadata only. **Never** prints secret
  values or ``op://`` reference strings (the redaction contract).
* ``refresh`` — warm, auth required: re-resolve one named set's live entries.
  ``--bucket`` is mandatory; it extends a live set but cannot resurrect an
  expired one.

``refresh`` mechanics are tested against an :class:`InMemoryBackend` so no
1Password prompt is triggered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from op_core.backends import file_caching
from op_core.backends.caching import _NOT_FOUND
from op_core.backends.file_caching import FileWriterLayer, _inspect_sets, _load_reader_state
from op_core.backends.memory import InMemoryBackend
from op_core.cli import cache as cache_cli
from op_core.exceptions import OpAuthError
from tests.unit.cache_helpers import StubBackend

if TYPE_CHECKING:
    from pathlib import Path

REF = "op://Vault/Item/field"
SECRET = "hunter2-plaintext-secret"


def _cache_path(tmp_path: Path) -> Path:
    return tmp_path / "cache.bin"


def _prime(path: Path, refs: dict[str, str], *, ttl: float = 300.0, bucket: str = "default") -> None:
    writer = FileWriterLayer(ttl=ttl, bucket=bucket, path=path)
    for ref, value in refs.items():
        writer.store(ref, value)


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_deletes_the_cache_file(self, tmp_path: Path) -> None:
        path = _cache_path(tmp_path)
        _prime(path, {REF: SECRET})
        assert path.exists()
        assert cache_cli.run(["clear", "--path", str(path)]) == 0
        assert not path.exists()

    def test_clear_on_absent_file_exits_zero(self, tmp_path: Path) -> None:
        assert cache_cli.run(["clear", "--path", str(_cache_path(tmp_path))]) == 0


# ---------------------------------------------------------------------------
# info — metadata only, and the redaction contract
# ---------------------------------------------------------------------------


class TestInfo:
    def test_info_reports_metadata(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = _cache_path(tmp_path)
        writer = FileWriterLayer(ttl=300, bucket="default", path=path)
        writer.store(REF, SECRET)
        writer.store("op://Vault/Item/gone", _NOT_FOUND)

        assert cache_cli.run(["info", "--path", str(path)]) == 0
        out = capsys.readouterr().out
        assert "default" in out  # bucket id is printable
        assert "1 value" in out  # one positive entry
        assert "1 miss" in out  # one negative entry

    def test_info_redacts_secret_values_and_references(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The redaction contract: info output carries no secret values and no op:// strings."""
        path = _cache_path(tmp_path)
        writer = FileWriterLayer(ttl=300, bucket="default", path=path)
        writer.store(REF, SECRET)
        writer.store("op://Vault/Item/gone", _NOT_FOUND)

        cache_cli.run(["info", "--path", str(path)])
        out = capsys.readouterr().out
        assert SECRET not in out
        assert "op://" not in out
        assert "Vault" not in out  # no fragment of a reference leaks

    def test_info_on_absent_file_is_graceful(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert cache_cli.run(["info", "--path", str(_cache_path(tmp_path))]) == 0
        assert "no" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# refresh — mechanics against InMemoryBackend (no prompts)
# ---------------------------------------------------------------------------


class TestRefresh:
    def test_refresh_requires_bucket(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            cache_cli.run(["refresh", "--path", str(_cache_path(tmp_path))])

    def test_refresh_restamps_value_from_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1200.0)  # still live (age 200 < 300)
        rc = cache_cli.run(
            ["refresh", "--bucket", "b", "--path", str(path)], backend=InMemoryBackend(refs={REF: "new-value"})
        )
        assert rc == 0

        # Past the original expiry (1300) but within the restamped window (1200+300): the new value is live.
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1400.0)
        _ttl, entries = _load_reader_state(path, "b")
        assert entries[REF].value == "new-value"

    def test_refresh_rechecks_a_stored_miss_that_now_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        writer = FileWriterLayer(ttl=300, bucket="b", path=path)
        writer.store(REF, _NOT_FOUND)  # was confirmed absent

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1100.0)
        rc = cache_cli.run(
            ["refresh", "--bucket", "b", "--path", str(path)], backend=InMemoryBackend(refs={REF: "now-present"})
        )
        assert rc == 0
        _ttl, entries = _load_reader_state(path, "b")
        assert entries[REF].value == "now-present"  # miss became a value

    def test_refresh_cannot_resurrect_an_expired_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 2000.0)  # expired (age 1000 > 300)
        source = StubBackend(refs={REF: "new-value"})
        rc = cache_cli.run(["refresh", "--bucket", "b", "--path", str(path)], backend=source)
        assert rc == 0
        assert source.read_count == 0  # no live keys -> the source is never consulted
        _ttl, entries = _load_reader_state(path, "b")
        assert entries == {}  # nothing resurrected

    def test_refresh_unknown_bucket_reports_error(self, tmp_path: Path) -> None:
        path = _cache_path(tmp_path)
        _prime(path, {REF: "v"}, bucket="b")
        rc = cache_cli.run(["refresh", "--bucket", "nonexistent", "--path", str(path)], backend=InMemoryBackend())
        assert rc == 1


# ---------------------------------------------------------------------------
# _inspect_sets — read-only, no purge, degrades to None
# ---------------------------------------------------------------------------


class TestInspectSets:
    def test_returns_none_for_absent_file(self, tmp_path: Path) -> None:
        assert _inspect_sets(_cache_path(tmp_path)) is None

    def test_returns_raw_sets_without_purging(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "v"}, ttl=300, bucket="b")
        before = path.read_bytes()

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 2000.0)  # the entry is now expired
        sets = _inspect_sets(path)
        assert sets is not None
        assert "b" in sets  # expired set still present (no purge on inspect)
        assert path.read_bytes() == before  # file untouched


# ---------------------------------------------------------------------------
# refresh: error propagation from source.read() inside the loop
# ---------------------------------------------------------------------------


class _RaisingBackend:
    """Backend double that raises a caller-specified error on read()."""

    def __init__(self, *, error: Exception) -> None:
        self._error = error
        self.read_count = 0

    def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
        self.read_count += 1
        raise self._error

    def list_items(self, *, vault: str | None = None, tags=None, categories=None):  # type: ignore[override]
        return []

    def list_vaults(self):  # type: ignore[override]
        return []

    def get_item(self, item, *, vault=None):  # type: ignore[override]
        from op_core.exceptions import OpNotFoundError
        raise OpNotFoundError("no item")


class TestRefreshErrorPropagation:
    def test_non_op_error_during_refresh_loop_propagates_uncaught(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-OpError raised by source.read() inside _do_refresh is not caught by run().

        _do_refresh only catches OpNotFoundError; run() catches OpError. Any other
        exception (e.g. RuntimeError) escapes both and propagates to the caller.
        This test documents the actual behavior so a future change that swallows
        non-op errors would break it.
        """
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1100.0)  # still live
        backend = _RaisingBackend(error=RuntimeError("unexpected internal failure"))
        with pytest.raises(RuntimeError, match="unexpected internal failure"):
            cache_cli.run(["refresh", "--bucket", "b", "--path", str(path)], backend=backend)
        # The source was reached before the error propagated.
        assert backend.read_count >= 1

    def test_op_auth_error_during_refresh_surfaces_as_exit_code_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """OpAuthError (an OpError subclass) raised inside _do_refresh is caught by
        run()'s top-level 'except OpError' handler and returns exit code 2.
        The error is emitted via log.error (not print).
        """
        import logging

        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1100.0)  # still live
        backend = _RaisingBackend(error=OpAuthError("session expired"))
        with caplog.at_level(logging.ERROR, logger="op_core.cli.cache"):
            rc = cache_cli.run(["refresh", "--bucket", "b", "--path", str(path)], backend=backend)
        assert rc == 2
        assert any("session expired" in r.message for r in caplog.records)
