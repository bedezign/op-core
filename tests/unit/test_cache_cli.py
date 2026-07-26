"""Tests for the ``op-cache`` console command (design sections 7-8).

``op-cache`` is a standalone, stdlib-only CLI with four subcommands:

* ``clear``   — cold, no auth: delete the whole cache file.
* ``info``    — cold, no auth: print metadata only. **Never** prints secret
  values or ``op://`` reference strings (the redaction contract).
* ``refresh`` — warm, auth required: re-resolve one named set's live entries
  under its existing stored ttl. ``--bucket`` is mandatory; it extends a live
  set but cannot resurrect an expired one, and it owns no ttl of its own.
* ``warm``    — warm, auth required: rebuild one named set's stored references
  under a NEW, caller-owned ttl (capped at 3 hours). ``--bucket`` and ``--ttl``
  are both mandatory. Unlike ``refresh``, it can rebuild an already-expired
  set, since it replaces the window outright rather than restamping it.

``refresh``/``warm`` mechanics are tested against an :class:`InMemoryBackend`
so no 1Password prompt is triggered.
"""

from __future__ import annotations

import json
import time
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

    def test_clear_purges_tombstoned_grace_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A set that has aged into its grace window still holds a tombstone record
        (reference + cached_at, no secret value) on disk. ``clear`` must delete the
        whole file regardless -- grace entries are not a carve-out. Today ``clear``
        goes through ``clear_cache_file()``, which drops the file outright rather than
        inspecting set state, so this is expected to pass without further changes; it
        exists to pin that behavior so a future move to selective/per-bucket clearing
        can't silently exempt tombstones.
        """
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        writer = FileWriterLayer(ttl=300, grace=150, bucket="default", path=path)
        writer.store(REF, SECRET)

        # Age past ttl (300) but within ttl+grace (450) -- the entry survives as a tombstone.
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1350.0)
        FileWriterLayer(ttl=300, grace=150, bucket="default", path=path)  # loading triggers the purge rewrite
        sets = _inspect_sets(path)
        assert sets is not None
        assert sets["default"]["entries"][REF].get("tombstone") is True

        assert cache_cli.run(["clear", "--path", str(path)]) == 0
        assert not path.exists()


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

    def test_human_output_format_includes_recoverable_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Locks the current prose format, including the ``recoverable`` count added
        alongside ``values``/``misses`` for tombstoned-but-in-grace entries.

        Entry ages are pinned via ``_wallclock`` (store time) and ``cache_cli.time.time``
        (the "now" _do_info reads); file size/mtime come from the real file so the
        assertion doesn't depend on the on-disk encoding being byte-stable.
        """
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1_700_000_000.0)
        writer = FileWriterLayer(ttl=300, bucket="default", path=path)
        writer.store(REF, SECRET)
        writer.store("op://Vault/Item/gone", _NOT_FOUND)

        monkeypatch.setattr(cache_cli.time, "time", lambda: 1_700_000_050.0)

        stat = path.stat()
        expected_modified = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
        expected = (
            f"cache file: {path}\n"
            f"size: {stat.st_size} bytes\n"
            f"modified: {expected_modified}\n"
            f"sets: 1\n"
            f"  bucket default: 1 value(s), 1 miss(es), 0 recoverable, ttl 300s, "
            f"oldest 50s, newest 50s, next expiry in 250s\n"
        )

        assert cache_cli.run(["info", "--path", str(path)]) == 0
        assert capsys.readouterr().out == expected

    def test_info_never_leaks_secrets_in_human_or_json_mode(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Cross-mode redaction contract, with distinctive sentinels (not the shared REF/SECRET
        constants) so a coincidental substring match can't hide a real leak.

        This is the guard that must still hold once a later change adds expired-entry
        retention to the cache file: whatever gets rendered, secrets and op:// references
        never do, in either --json or the human default.
        """
        path = _cache_path(tmp_path)
        sentinel_secret = "SENTINEL-SECRET-9f3d7c21"
        sentinel_ref = "op://SentinelVault/SentinelItem/sentinelfield"
        writer = FileWriterLayer(ttl=300, bucket="leaktest", path=path)
        writer.store(sentinel_ref, sentinel_secret)
        writer.store("op://SentinelVault/SentinelItem/missing", _NOT_FOUND)

        assert cache_cli.run(["info", "--path", str(path)]) == 0
        human_out = capsys.readouterr().out

        assert cache_cli.run(["info", "--json", "--path", str(path)]) == 0
        json_out = capsys.readouterr().out

        for out in (human_out, json_out):
            assert sentinel_secret not in out
            assert sentinel_ref not in out
            assert "op://" not in out
            assert "SentinelVault" not in out

    def test_info_never_leaks_a_secret_aged_into_the_tombstone_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The highest-value case for this feature: an entry aged past ttl but still
        within grace is reported as ``recoverable``, never as a value -- and neither
        its secret nor its op:// reference ever appears in either output mode, even
        though the raw record may still carry the plaintext ``value`` on disk (no
        purge has run in this flow to convert it to a tombstone shape).
        """
        path = _cache_path(tmp_path)
        sentinel_secret = "SENTINEL-SECRET-aged-9f3d7c21"
        sentinel_ref = "op://SentinelVault/SentinelItem/agedfield"
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1_700_000_000.0)
        writer = FileWriterLayer(ttl=100, bucket="agingleak", path=path)  # default grace: 50s
        writer.store(sentinel_ref, sentinel_secret)

        monkeypatch.setattr(cache_cli.time, "time", lambda: 1_700_000_130.0)  # age 130: ttl 100 < 130 <= 150

        assert cache_cli.run(["info", "--path", str(path)]) == 0
        human_out = capsys.readouterr().out

        assert cache_cli.run(["info", "--json", "--path", str(path)]) == 0
        json_out = capsys.readouterr().out

        for out in (human_out, json_out):
            assert sentinel_secret not in out
            assert sentinel_ref not in out
            assert "op://" not in out
            assert "SentinelVault" not in out

        payload = json.loads(json_out)
        assert payload["sets"]["agingleak"]["recoverable"] == 1
        assert payload["sets"]["agingleak"]["values"] == 0


class TestInfoJson:
    """``op-cache info --json`` — structured metadata, same fields as the prose output.

    Field-name assumptions made by these tests (the contract this task is defining):
    top-level ``schema`` (int, currently ``1``), ``path`` (str), ``size`` (int, bytes),
    ``modified`` (float, epoch seconds matching ``stat.st_mtime``), and ``sets`` (dict
    keyed by bucket id). Each bucket entry carries ``values`` (int), ``misses`` (int),
    ``recoverable`` (int -- tombstoned entries still within their set's grace window),
    ``ttl`` (float, seconds), ``oldest_age`` (float, seconds), ``newest_age`` (float,
    seconds), ``seconds_until_expiry`` (float, negative when overdue), and ``overdue``
    (bool). ``oldest_age``/``newest_age`` are durations, not timestamps, hence the
    ``_age`` suffix; ``schema`` lets a consumer detect a future shape change (e.g. a
    per-bucket ``state`` field) rather than infer one. ``recoverable`` was part of the
    schema from its first release (``schema == 1``); there was never a shipped shape
    that lacked it.
    """

    def test_info_json_emits_parseable_json_with_every_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1_700_000_000.0)
        writer = FileWriterLayer(ttl=300, bucket="b1", path=path)
        writer.store(REF, SECRET)
        writer.store("op://Vault/Item/gone", _NOT_FOUND)

        monkeypatch.setattr(cache_cli.time, "time", lambda: 1_700_000_050.0)

        assert cache_cli.run(["info", "--json", "--path", str(path)]) == 0
        out = capsys.readouterr().out
        payload = json.loads(out)  # must be parseable, full stop

        stat = path.stat()
        assert payload["schema"] == 1
        assert payload["path"] == str(path)
        assert payload["size"] == stat.st_size
        assert payload["modified"] == pytest.approx(stat.st_mtime)

        bucket = payload["sets"]["b1"]
        assert bucket["values"] == 1
        assert bucket["misses"] == 1
        assert bucket["recoverable"] == 0
        assert bucket["ttl"] == pytest.approx(300.0)
        assert bucket["oldest_age"] == pytest.approx(50.0)
        assert bucket["newest_age"] == pytest.approx(50.0)
        assert bucket["seconds_until_expiry"] == pytest.approx(250.0)
        assert bucket["overdue"] is False

    def test_info_json_overdue_set_reports_negative_seconds_and_overdue_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1_700_000_000.0)
        writer = FileWriterLayer(ttl=10, bucket="stale", path=path)
        writer.store(REF, SECRET)

        monkeypatch.setattr(cache_cli.time, "time", lambda: 1_700_000_100.0)  # ttl 10s, 100s later -> overdue by 90s

        assert cache_cli.run(["info", "--json", "--path", str(path)]) == 0
        payload = json.loads(capsys.readouterr().out)

        bucket = payload["sets"]["stale"]
        assert bucket["seconds_until_expiry"] == pytest.approx(-90.0)
        assert bucket["overdue"] is True

    def test_info_json_omits_a_bucket_with_zero_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty bucket must never be reported as if it carried a full TTL of runway.

        Normal writer traffic drops emptied sets on purge, so an empty bucket
        should not exist on disk in practice -- but if one somehow does, it
        must be absent from ``sets`` rather than reporting misleading ages.
        """
        path = _cache_path(tmp_path)
        writer = FileWriterLayer(ttl=300, bucket="populated", path=path)
        writer.store(REF, SECRET)

        # Inject an empty bucket directly -- normal writer traffic never
        # leaves one behind (purge-on-load drops emptied sets).
        raw = _inspect_sets(path)
        assert raw is not None
        raw["empty"] = {"ttl": 300.0, "grace": 150.0, "entries": {}}
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1_700_000_000.0)
        # Test seam: injecting a state normal writer traffic never produces.
        file_caching._atomic_write(
            path, file_caching._encode_payload({"version": file_caching._CACHE_VERSION, "sets": raw})
        )

        assert cache_cli.run(["info", "--json", "--path", str(path)]) == 0
        payload = json.loads(capsys.readouterr().out)

        assert "empty" not in payload["sets"]
        assert "populated" in payload["sets"]

    def test_info_json_on_absent_file_is_graceful(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert cache_cli.run(["info", "--json", "--path", str(_cache_path(tmp_path))]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema"] == 1
        assert payload["sets"] == {}

    def test_info_json_reports_recoverable_count_for_tombstoned_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A tombstoned-but-in-grace entry is counted as ``recoverable``, distinct from
        ``values``/``misses``, and its state is recomputed at display time rather than
        trusted from the raw ``tombstone`` flag left on disk by the last purge.
        """
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1_700_000_000.0)
        writer = FileWriterLayer(ttl=100, bucket="aging", path=path)  # default grace: 50s
        writer.store(REF, SECRET)

        # Age the entry past ttl but within grace -- still present with its raw "value"
        # (no purge has run), so a naive read of a "tombstone" flag would miss it.
        monkeypatch.setattr(cache_cli.time, "time", lambda: 1_700_000_130.0)  # age 130: ttl 100 < 130 <= 150

        assert cache_cli.run(["info", "--json", "--path", str(path)]) == 0
        payload = json.loads(capsys.readouterr().out)

        bucket = payload["sets"]["aging"]
        assert bucket["values"] == 0
        assert bucket["misses"] == 0
        assert bucket["recoverable"] == 1


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

    def test_refresh_cannot_resurrect_a_set_beyond_its_grace_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")  # default grace: 150s

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 2000.0)  # age 1000, past ttl+grace (450) -> dead
        source = StubBackend(refs={REF: "new-value"})
        rc = cache_cli.run(["refresh", "--bucket", "b", "--path", str(path)], backend=source)
        assert rc == 0
        assert source.read_count == 0  # no live or recoverable keys -> the source is never consulted
        _ttl, entries = _load_reader_state(path, "b")
        assert entries == {}  # nothing resurrected

    def test_refresh_resurrects_an_entry_within_the_grace_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An entry aged past ttl but still within grace IS resurrected: the source
        backend is consulted and the entry becomes live again under the stored ttl.
        """
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")  # default grace: 150s

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1350.0)  # age 350: past ttl(300), within grace (450)
        source = StubBackend(refs={REF: "resurrected-value"})
        rc = cache_cli.run(["refresh", "--bucket", "b", "--path", str(path)], backend=source)
        assert rc == 0
        assert source.read_count == 1  # the source was consulted to resurrect it

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1360.0)  # fresh cached_at (1350) is now live again
        _ttl, entries = _load_reader_state(path, "b")
        assert entries[REF].value == "resurrected-value"

    def test_refresh_unknown_bucket_reports_error(self, tmp_path: Path) -> None:
        path = _cache_path(tmp_path)
        _prime(path, {REF: "v"}, bucket="b")
        rc = cache_cli.run(["refresh", "--bucket", "nonexistent", "--path", str(path)], backend=InMemoryBackend())
        assert rc == 1

    def test_refresh_has_no_ttl_option(self, tmp_path: Path) -> None:
        """Regression guard: refresh owns no TTL -- this is the invariant the warm/refresh split turns on."""
        path = _cache_path(tmp_path)
        _prime(path, {REF: "v"}, ttl=300, bucket="b")
        with pytest.raises(SystemExit):
            cache_cli.run(["refresh", "--bucket", "b", "--ttl", "600", "--path", str(path)], backend=InMemoryBackend())

    def test_refresh_restamps_against_the_stored_ttl_not_a_new_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1100.0)
        rc = cache_cli.run(
            ["refresh", "--bucket", "b", "--path", str(path)], backend=InMemoryBackend(refs={REF: "new-value"})
        )
        assert rc == 0
        ttl, _entries = _load_reader_state(path, "b")
        assert ttl == pytest.approx(300.0)  # unchanged -- refresh never owns a new ttl

    def test_refresh_preserves_the_stored_grace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mirrors the ttl-preservation test above: refresh owns no new grace either."""
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        writer = FileWriterLayer(ttl=300, grace=222, bucket="b", path=path)
        writer.store(REF, "old-value")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1100.0)
        rc = cache_cli.run(
            ["refresh", "--bucket", "b", "--path", str(path)], backend=InMemoryBackend(refs={REF: "new-value"})
        )
        assert rc == 0
        raw = _inspect_sets(path)
        assert raw is not None
        assert raw["b"]["grace"] == pytest.approx(222.0)  # unchanged -- refresh never owns a new grace


# ---------------------------------------------------------------------------
# warm — owns a capped ttl and discards+rebuilds the set under it
# ---------------------------------------------------------------------------


class TestWarm:
    def test_warm_requires_bucket(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            cache_cli.run(["warm", "--ttl", "600", "--path", str(_cache_path(tmp_path))])

    def test_warm_requires_ttl(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            cache_cli.run(["warm", "--bucket", "b", "--path", str(_cache_path(tmp_path))])

    def test_warm_stores_the_new_ttl(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1100.0)
        rc = cache_cli.run(
            ["warm", "--bucket", "b", "--ttl", "600", "--path", str(path)],
            backend=InMemoryBackend(refs={REF: "new-value"}),
        )
        assert rc == 0
        ttl, entries = _load_reader_state(path, "b")
        assert ttl == pytest.approx(600.0)
        assert entries[REF].value == "new-value"

    def test_warm_with_a_different_ttl_discards_and_rebuilds_rather_than_mutating(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")
        raw_before = _inspect_sets(path)
        assert raw_before is not None
        assert raw_before["b"]["ttl"] == pytest.approx(300.0)

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1050.0)
        source = InMemoryBackend(refs={REF: "rewarmed-value"})
        rc = cache_cli.run(["warm", "--bucket", "b", "--ttl", "900", "--path", str(path)], backend=source)
        assert rc == 0

        raw_after = _inspect_sets(path)
        assert raw_after is not None
        assert raw_after["b"]["ttl"] == pytest.approx(900.0)  # rebuilt under the new ttl, not mutated in place
        assert raw_after["b"]["entries"][REF]["value"] == "rewarmed-value"  # re-resolved, not carried over verbatim

    def test_warm_rejects_ttl_above_the_three_hour_cap(self, tmp_path: Path) -> None:
        path = _cache_path(tmp_path)
        _prime(path, {REF: "v"}, ttl=300, bucket="b")
        with pytest.raises(SystemExit) as excinfo:
            cache_cli.run(["warm", "--bucket", "b", "--ttl", "10801", "--path", str(path)])
        assert excinfo.value.code == 2

    def test_warm_accepts_ttl_exactly_at_the_three_hour_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1050.0)
        rc = cache_cli.run(
            ["warm", "--bucket", "b", "--ttl", "10800", "--path", str(path)],
            backend=InMemoryBackend(refs={REF: "new-value"}),
        )
        assert rc == 0
        ttl, _entries = _load_reader_state(path, "b")
        assert ttl == pytest.approx(10800.0)

    def test_warm_rejects_zero_ttl(self, tmp_path: Path) -> None:
        path = _cache_path(tmp_path)
        _prime(path, {REF: "v"}, ttl=300, bucket="b")
        with pytest.raises(SystemExit) as excinfo:
            cache_cli.run(["warm", "--bucket", "b", "--ttl", "0", "--path", str(path)])
        assert excinfo.value.code == 2

    def test_warm_rejects_negative_ttl(self, tmp_path: Path) -> None:
        path = _cache_path(tmp_path)
        _prime(path, {REF: "v"}, ttl=300, bucket="b")
        with pytest.raises(SystemExit) as excinfo:
            cache_cli.run(["warm", "--bucket", "b", "--ttl", "-1", "--path", str(path)])
        assert excinfo.value.code == 2

    def test_warm_unknown_bucket_reports_error(self, tmp_path: Path) -> None:
        path = _cache_path(tmp_path)
        _prime(path, {REF: "v"}, bucket="b")
        rc = cache_cli.run(
            ["warm", "--bucket", "nonexistent", "--ttl", "600", "--path", str(path)], backend=InMemoryBackend()
        )
        assert rc == 1

    def test_warm_on_absent_cache_file_reports_error(self, tmp_path: Path) -> None:
        path = _cache_path(tmp_path)
        rc = cache_cli.run(["warm", "--bucket", "b", "--ttl", "600", "--path", str(path)], backend=InMemoryBackend())
        assert rc == 1

    def test_warm_rechecks_a_stored_miss_that_now_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Doom-path: a bucket whose stored entries are all miss records still warms cleanly."""
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        writer = FileWriterLayer(ttl=300, bucket="b", path=path)
        writer.store(REF, _NOT_FOUND)

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1100.0)
        rc = cache_cli.run(
            ["warm", "--bucket", "b", "--ttl", "600", "--path", str(path)],
            backend=InMemoryBackend(refs={REF: "now-present"}),
        )
        assert rc == 0
        _ttl, entries = _load_reader_state(path, "b")
        assert entries[REF].value == "now-present"

    def test_warm_can_rebuild_a_set_whose_entries_are_within_grace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unlike refresh, warm rebuilds under a new ttl -- a set expired-but-in-grace still warms."""
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")  # default grace: 150s

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1350.0)  # age 350: past ttl(300), within grace (450)
        rc = cache_cli.run(
            ["warm", "--bucket", "b", "--ttl", "600", "--path", str(path)],
            backend=InMemoryBackend(refs={REF: "revived-value"}),
        )
        assert rc == 0
        _ttl, entries = _load_reader_state(path, "b")
        assert entries[REF].value == "revived-value"

    def test_warm_does_not_resurrect_a_reference_beyond_grace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reference aged past ttl+grace is gone -- warm no longer treats every stale
        reference on disk as fair game, only live and in-grace ones.
        """
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")  # default grace: 150s

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 2000.0)  # age 1000: past ttl+grace (450) -> dead
        source = StubBackend(refs={REF: "should-not-be-fetched"})
        rc = cache_cli.run(["warm", "--bucket", "b", "--ttl", "600", "--path", str(path)], backend=source)
        assert rc == 0
        assert source.read_count == 0
        _ttl, entries = _load_reader_state(path, "b")
        assert entries == {}

    def test_warm_accepts_an_explicit_grace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1050.0)
        rc = cache_cli.run(
            ["warm", "--bucket", "b", "--ttl", "600", "--grace", "900", "--path", str(path)],
            backend=InMemoryBackend(refs={REF: "new-value"}),
        )
        assert rc == 0
        raw = _inspect_sets(path)
        assert raw is not None
        assert raw["b"]["grace"] == pytest.approx(900.0)

    def test_warm_accepts_zero_grace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """grace=0 is legal -- it disables the tombstone window entirely, unlike --ttl."""
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1050.0)
        rc = cache_cli.run(
            ["warm", "--bucket", "b", "--ttl", "600", "--grace", "0", "--path", str(path)],
            backend=InMemoryBackend(refs={REF: "new-value"}),
        )
        assert rc == 0
        raw = _inspect_sets(path)
        assert raw is not None
        assert raw["b"]["grace"] == pytest.approx(0.0)

    def test_warm_rejects_negative_grace(self, tmp_path: Path) -> None:
        path = _cache_path(tmp_path)
        _prime(path, {REF: "v"}, ttl=300, bucket="b")
        with pytest.raises(SystemExit) as excinfo:
            cache_cli.run(["warm", "--bucket", "b", "--ttl", "600", "--grace", "-1", "--path", str(path)])
        assert excinfo.value.code == 2

    def test_warm_defaults_grace_when_omitted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitting --grace lets the engine apply its own default fraction of ttl."""
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1050.0)
        rc = cache_cli.run(
            ["warm", "--bucket", "b", "--ttl", "600", "--path", str(path)],
            backend=InMemoryBackend(refs={REF: "new-value"}),
        )
        assert rc == 0
        raw = _inspect_sets(path)
        assert raw is not None
        assert raw["b"]["grace"] == pytest.approx(300.0)  # engine default: ttl * 0.5

    def test_warm_on_bucket_with_zero_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A bucket present in the file but holding zero entries still warms cleanly to empty.

        Injected directly, mirroring ``test_info_json_omits_a_bucket_with_zero_entries`` --
        normal writer traffic never leaves an empty bucket behind (purge-on-load drops it).
        """
        path = _cache_path(tmp_path)
        _prime(path, {REF: "v"}, ttl=300, bucket="populated")
        raw = _inspect_sets(path)
        assert raw is not None
        raw["empty"] = {"ttl": 300.0, "grace": 150.0, "entries": {}}
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1_700_000_000.0)
        file_caching._atomic_write(
            path, file_caching._encode_payload({"version": file_caching._CACHE_VERSION, "sets": raw})
        )

        source = StubBackend(refs={})
        rc = cache_cli.run(["warm", "--bucket", "empty", "--ttl", "600", "--path", str(path)], backend=source)
        assert rc == 0
        assert source.read_count == 0

    def test_warm_is_idempotent_across_identical_back_to_back_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, {REF: "old-value"}, ttl=300, bucket="b")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1050.0)
        source = InMemoryBackend(refs={REF: "warmed-value"})
        rc1 = cache_cli.run(
            ["warm", "--bucket", "b", "--ttl", "600", "--grace", "120", "--path", str(path)], backend=source
        )
        assert rc1 == 0
        state_1 = _inspect_sets(path)

        rc2 = cache_cli.run(
            ["warm", "--bucket", "b", "--ttl", "600", "--grace", "120", "--path", str(path)], backend=source
        )
        assert rc2 == 0
        state_2 = _inspect_sets(path)

        assert state_1 is not None
        assert state_2 is not None
        assert state_1["b"]["ttl"] == state_2["b"]["ttl"]
        assert state_1["b"]["grace"] == state_2["b"]["grace"]
        assert state_1["b"]["entries"].keys() == state_2["b"]["entries"].keys()


class TestTtlAndGraceTypeValidation:
    """``_ttl_type``/``_grace_type`` are ``argparse`` ``type=`` callables: invalid non-numeric
    input must be a usage error (exit code 2), the same as any other malformed argument.
    """

    def test_ttl_type_rejects_empty_string(self, tmp_path: Path) -> None:
        path = _cache_path(tmp_path)
        _prime(path, {REF: "v"}, ttl=300, bucket="b")
        with pytest.raises(SystemExit) as excinfo:
            cache_cli.run(["warm", "--bucket", "b", "--ttl", "", "--path", str(path)])
        assert excinfo.value.code == 2

    def test_ttl_type_rejects_non_numeric_string(self, tmp_path: Path) -> None:
        path = _cache_path(tmp_path)
        _prime(path, {REF: "v"}, ttl=300, bucket="b")
        with pytest.raises(SystemExit) as excinfo:
            cache_cli.run(["warm", "--bucket", "b", "--ttl", "not-a-number", "--path", str(path)])
        assert excinfo.value.code == 2

    def test_grace_type_rejects_empty_string(self, tmp_path: Path) -> None:
        path = _cache_path(tmp_path)
        _prime(path, {REF: "v"}, ttl=300, bucket="b")
        with pytest.raises(SystemExit) as excinfo:
            cache_cli.run(["warm", "--bucket", "b", "--ttl", "600", "--grace", "", "--path", str(path)])
        assert excinfo.value.code == 2

    def test_grace_type_rejects_non_numeric_string(self, tmp_path: Path) -> None:
        path = _cache_path(tmp_path)
        _prime(path, {REF: "v"}, ttl=300, bucket="b")
        with pytest.raises(SystemExit) as excinfo:
            cache_cli.run(["warm", "--bucket", "b", "--ttl", "600", "--grace", "not-a-number", "--path", str(path)])
        assert excinfo.value.code == 2


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


# ---------------------------------------------------------------------------
# warm: resolve-before-destroy ordering, and the ttl cap as a domain invariant
# ---------------------------------------------------------------------------


class _FailAfterNBackend:
    """Backend double: resolves the first ``fail_after`` references, then raises on the next."""

    def __init__(self, *, refs: dict[str, str], fail_after: int, error: Exception) -> None:
        self._refs = refs
        self._fail_after = fail_after
        self._error = error
        self.read_count = 0

    def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
        self.read_count += 1
        if self.read_count > self._fail_after:
            raise self._error
        return self._refs[reference]

    def list_items(self, *, vault=None, tags=None, categories=None):  # type: ignore[override]
        return []

    def list_vaults(self):  # type: ignore[override]
        return []

    def get_item(self, item, *, vault=None):  # type: ignore[override]
        from op_core.exceptions import OpNotFoundError

        raise OpNotFoundError("no item")


class TestWarmDestructiveOrdering:
    def test_mid_loop_backend_failure_leaves_prior_state_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A backend failure partway through resolving must not have already discarded the
        bucket's prior entries -- the writer that owns the new (ttl, grace) is constructed
        only once every reference has resolved, so a failure here leaves the original set
        untouched and re-resolvable.
        """
        path = _cache_path(tmp_path)
        refs = {
            "op://Vault/Item/a": "a-value",
            "op://Vault/Item/b": "b-value",
            "op://Vault/Item/c": "c-value",
        }
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        _prime(path, refs, ttl=300, bucket="b")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1050.0)
        backend = _FailAfterNBackend(refs=refs, fail_after=1, error=OpAuthError("session expired"))
        rc = cache_cli.run(["warm", "--bucket", "b", "--ttl", "600", "--path", str(path)], backend=backend)
        assert rc == 2  # OpAuthError is an OpError, caught by run()'s top-level handler

        raw_after = _inspect_sets(path)
        assert raw_after is not None
        assert raw_after["b"]["ttl"] == pytest.approx(300.0)  # the original stored ttl, not the new one
        assert set(raw_after["b"]["entries"].keys()) == set(refs.keys())  # nothing destroyed


class TestWarmTtlCapEnforcement:
    def test_do_warm_rejects_ttl_above_cap_even_when_called_directly(self, tmp_path: Path) -> None:
        """Defense in depth: the cap is a domain invariant, enforced in `_do_warm` itself,
        not only in `_ttl_type`'s argparse callback -- a caller reaching `_do_warm` by
        another path (bypassing the CLI parser) must not be able to exceed it either.
        """
        path = _cache_path(tmp_path)
        _prime(path, {REF: "v"}, ttl=300, bucket="b")
        with pytest.raises(ValueError, match="exceeds"):
            cache_cli._do_warm(path, "b", cache_cli._WARM_MAX_TTL_SECONDS + 1, None, backend=InMemoryBackend())
