"""Tests for FileReaderLayer, FileWriterLayer, and clear_cache_file.

Phase 4 of the resolver-stack redesign (design section 4 / plan Phase 4):
- FileReaderLayer  — pure observer, stored-TTL, no writes ever
- FileWriterLayer  — read-write, per-set TTL, purge-on-load
- clear_cache_file — whole-file hammer with FIX-1 (leave sidecar)
- FIX-2            — writer's _FileCache uses two-sided expiry bound
"""

from __future__ import annotations

import fcntl
import os
import threading
from pathlib import Path

import pytest

from op_core.backends import file_caching
from op_core.backends.base import Backend
from op_core.backends.caching import _NOT_FOUND
from op_core.backends.file_caching import FileReaderLayer, FileWriterLayer, clear_cache_file
from op_core.backends.stack import CacheLayer, WritableCacheLayer
from tests.unit.cache_helpers import read_sets

REF = "op://Vault/Item/field"


def _cache_path(tmp_path: Path) -> Path:
    return tmp_path / "cache.bin"


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_file_writer_layer_satisfies_writable_cache_layer(self) -> None:
        layer = FileWriterLayer(ttl=60)
        assert isinstance(layer, WritableCacheLayer)

    def test_file_reader_layer_satisfies_cache_layer(self, tmp_path: Path) -> None:
        layer = FileReaderLayer(path=_cache_path(tmp_path))
        assert isinstance(layer, CacheLayer)

    def test_file_reader_layer_does_not_satisfy_writable_cache_layer(self, tmp_path: Path) -> None:
        layer = FileReaderLayer(path=_cache_path(tmp_path))
        assert not isinstance(layer, WritableCacheLayer)

    def test_file_writer_layer_is_not_a_backend(self) -> None:
        layer = FileWriterLayer(ttl=60)
        assert not isinstance(layer, Backend)


# ---------------------------------------------------------------------------
# FileWriterLayer — basic store / lookup
# ---------------------------------------------------------------------------


class TestFileWriterLayerBasic:
    def test_store_then_lookup_returns_value(self, tmp_path: Path) -> None:
        layer = FileWriterLayer(ttl=300, path=_cache_path(tmp_path))
        layer.store(REF, "my-secret")
        entry = layer.lookup(REF)
        assert entry is not None
        assert entry.value == "my-secret"

    def test_lookup_miss_returns_none(self, tmp_path: Path) -> None:
        layer = FileWriterLayer(ttl=300, path=_cache_path(tmp_path))
        assert layer.lookup("op://v/absent") is None

    def test_not_found_sentinel_round_trips(self, tmp_path: Path) -> None:
        """Storing the _NOT_FOUND sentinel must be retrievable as-is."""
        layer = FileWriterLayer(ttl=300, path=_cache_path(tmp_path))
        layer.store(REF, _NOT_FOUND)
        entry = layer.lookup(REF)
        assert entry is not None
        assert entry.value is _NOT_FOUND

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        """A value stored by one FileWriterLayer is visible to a fresh one on the same path."""
        path = _cache_path(tmp_path)
        writer1 = FileWriterLayer(ttl=300, path=path)
        writer1.store(REF, "persisted-value")

        writer2 = FileWriterLayer(ttl=300, path=path)
        entry = writer2.lookup(REF)
        assert entry is not None
        assert entry.value == "persisted-value"

    def test_ttl_expiry_across_instances(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An entry stored before the TTL elapses is gone to a fresh instance after expiry."""
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        writer1 = FileWriterLayer(ttl=300, path=path)
        writer1.store(REF, "will-expire")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0 + 301)
        writer2 = FileWriterLayer(ttl=300, path=path)
        assert writer2.lookup(REF) is None

    def test_ttl_zero_writes_no_file_and_does_not_persist(self, tmp_path: Path) -> None:
        """ttl<=0 keeps the engine inert: no file is created, nothing persists."""
        path = _cache_path(tmp_path)
        layer = FileWriterLayer(ttl=0, path=path)
        layer.store(REF, "ephemeral")
        assert not path.exists()

        fresh = FileWriterLayer(ttl=0, path=path)
        assert fresh.lookup(REF) is None

    def test_clear_empties_memory_and_disk_set(self, tmp_path: Path) -> None:
        """clear() removes in-memory entries and the own bucket on disk."""
        path = _cache_path(tmp_path)
        layer = FileWriterLayer(ttl=300, path=path)
        layer.store(REF, "secret")
        layer.clear()

        # In-memory gone.
        assert layer.lookup(REF) is None

        # On-disk set gone too — merge on a new store() cannot resurrect it.
        fresh = FileWriterLayer(ttl=300, path=path)
        assert fresh.lookup(REF) is None

    def test_clear_misses_drops_misses_keeps_values(self, tmp_path: Path) -> None:
        """clear_misses() removes negative-cache records but preserves positive values."""
        path = _cache_path(tmp_path)
        layer = FileWriterLayer(ttl=300, path=path)
        layer.store(REF, "value")
        layer.store("op://v/missing", _NOT_FOUND)
        layer.clear_misses()

        assert layer.lookup(REF) is not None
        assert layer.lookup("op://v/missing") is None

    def test_clear_without_persistence_is_safe(self, tmp_path: Path) -> None:
        """clear() and clear_misses() on a ttl=0 (no-file) writer must not raise."""
        layer = FileWriterLayer(ttl=0, path=_cache_path(tmp_path))
        layer.store(REF, "ephemeral")
        layer.clear()  # must not raise
        layer.clear_misses()  # must not raise

    def test_per_set_ttl_mismatch_reconstructs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A writer with a different TTL from the stored set discards and rebuilds the set."""
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        writer1 = FileWriterLayer(ttl=3600, path=path)
        writer1.store(REF, "old-ttl-value")

        writer2 = FileWriterLayer(ttl=60, path=path)
        # The set was stamped with ttl=3600; the new writer discards it.
        assert writer2.lookup(REF) is None

    def test_bucket_isolation(self, tmp_path: Path) -> None:
        """Two writers on different buckets store independent entries."""
        path = _cache_path(tmp_path)
        writer_a = FileWriterLayer(ttl=300, path=path, bucket="a")
        writer_b = FileWriterLayer(ttl=300, path=path, bucket="b")
        writer_a.store(REF, "value-a")
        writer_b.store(REF, "value-b")

        # Reload from disk so we exercise the on-disk isolation.
        fresh_a = FileWriterLayer(ttl=300, path=path, bucket="a")
        fresh_b = FileWriterLayer(ttl=300, path=path, bucket="b")
        assert fresh_a.lookup(REF).value == "value-a"  # type: ignore[union-attr]
        assert fresh_b.lookup(REF).value == "value-b"  # type: ignore[union-attr]

    def test_max_entries_trim(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exceeding max_entries keeps only the newest on disk after the next store()."""
        path = _cache_path(tmp_path)
        tick = 1000.0
        for i in range(3):
            monkeypatch.setattr(file_caching, "_wallclock", lambda t=tick: t)
            layer = FileWriterLayer(ttl=3600, path=path, max_entries=1024)
            layer.store(f"op://v/{i}", f"val-{i}")
            tick += 1.0

        # New writer with max_entries=2; next store() trims to 2.
        monkeypatch.setattr(file_caching, "_wallclock", lambda: tick)
        trimmer = FileWriterLayer(ttl=3600, path=path, max_entries=2)
        trimmer.store("op://v/newest", "newest-val")

        sets = read_sets(path)
        entries = sets["default"]["entries"]
        assert len(entries) == 2
        assert "op://v/newest" in entries

    def test_ttl_required_missing_raises_typeerror(self) -> None:
        """FileWriterLayer() without ttl must raise TypeError."""
        with pytest.raises(TypeError):
            FileWriterLayer()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# FIX-2: future-dated entry not served by the writer
# ---------------------------------------------------------------------------


class TestFix2WriterFutureDated:
    def test_future_dated_entry_not_served_by_writer_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FIX-2: an entry stamped in the future is expired by the writer's lookup.

        The writer uses a two-sided bound (0 <= age <= ttl), so a future-stamped
        entry (age < 0) is not served — clock skew is not immortality.
        """
        path = _cache_path(tmp_path)
        # Store at T=5000 (future relative to the read-back clock).
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 5000.0)
        layer = FileWriterLayer(ttl=300, path=path)
        layer.store(REF, "future-value")

        # Read back at T=1000 — entry is in the future, should be treated as expired.
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        fresh = FileWriterLayer(ttl=300, path=path)
        assert fresh.lookup(REF) is None

    def test_future_dated_entry_dropped_on_purge(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """FIX-2: _FileCache._purge uses the two-sided bound, so future entries are dropped."""
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 5000.0)
        layer = FileWriterLayer(ttl=300, path=path)
        layer.store(REF, "skewed")

        # Re-load (construct a new writer) at T=1000 — purge fires on load.
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        FileWriterLayer(ttl=300, path=path)  # triggers _load -> _purge

        # File should now be empty (set dropped because all entries expired).

        raw_sets = read_sets(path)
        assert REF not in raw_sets.get("default", {}).get("entries", {})


# ---------------------------------------------------------------------------
# FileReaderLayer — serves cached values
# ---------------------------------------------------------------------------


class TestFileReaderLayerServes:
    def test_serves_value_written_by_writer(self, tmp_path: Path) -> None:
        """A FileReaderLayer returns a value stored by FileWriterLayer."""
        path = _cache_path(tmp_path)
        writer = FileWriterLayer(ttl=300, path=path)
        writer.store(REF, "cached-value")
        path.with_name(path.name + ".lock").unlink(missing_ok=True)

        reader = FileReaderLayer(path=path)
        entry = reader.lookup(REF)
        assert entry is not None
        assert entry.value == "cached-value"

    def test_serves_stored_miss_sentinel(self, tmp_path: Path) -> None:
        """A FileReaderLayer returns the _NOT_FOUND sentinel for a stored miss."""
        path = _cache_path(tmp_path)
        writer = FileWriterLayer(ttl=300, path=path)
        writer.store("op://v/missing", _NOT_FOUND)
        path.with_name(path.name + ".lock").unlink(missing_ok=True)

        reader = FileReaderLayer(path=path)
        entry = reader.lookup("op://v/missing")
        assert entry is not None
        assert entry.value is _NOT_FOUND

    def test_unknown_reference_returns_none(self, tmp_path: Path) -> None:
        """lookup for an absent key returns None."""
        path = _cache_path(tmp_path)
        writer = FileWriterLayer(ttl=300, path=path)
        writer.store(REF, "value")
        path.with_name(path.name + ".lock").unlink(missing_ok=True)

        reader = FileReaderLayer(path=path)
        assert reader.lookup("op://v/absent") is None

    def test_bucket_scoped_reader_cannot_see_other_bucket(self, tmp_path: Path) -> None:
        """A reader bound to bucket 'b' cannot see entries in bucket 'a'."""
        path = _cache_path(tmp_path)
        writer = FileWriterLayer(ttl=300, path=path, bucket="a")
        writer.store(REF, "value-a")
        path.with_name(path.name + ".lock").unlink(missing_ok=True)

        reader = FileReaderLayer(path=path, bucket="b")
        assert reader.lookup(REF) is None

    def test_stored_ttl_expiry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An entry expired by the set's stored TTL is not served."""
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        writer = FileWriterLayer(ttl=300, path=path)
        writer.store(REF, "stale")
        path.with_name(path.name + ".lock").unlink(missing_ok=True)

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0 + 301)
        reader = FileReaderLayer(path=path)
        assert reader.lookup(REF) is None

    def test_future_dated_entry_treated_as_expired(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clock skew: an entry stamped in the future is expired by the reader too."""
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 5000.0)
        writer = FileWriterLayer(ttl=300, path=path)
        writer.store(REF, "future-stamped")
        path.with_name(path.name + ".lock").unlink(missing_ok=True)

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        reader = FileReaderLayer(path=path)
        assert reader.lookup(REF) is None


# ---------------------------------------------------------------------------
# FileReaderLayer — purity: never writes, never creates side effects
# ---------------------------------------------------------------------------


class TestFileReaderLayerPurity:
    def test_file_bytes_unchanged_across_reads_and_misses(self, tmp_path: Path) -> None:
        """Reading values and missing keys must not alter the cache file bytes."""
        path = _cache_path(tmp_path)
        writer = FileWriterLayer(ttl=300, path=path)
        writer.store(REF, "value")
        path.with_name(path.name + ".lock").unlink(missing_ok=True)
        before = path.read_bytes()

        reader = FileReaderLayer(path=path)
        reader.lookup(REF)
        reader.lookup("op://v/absent")  # miss must not write
        assert path.read_bytes() == before

    def test_no_lock_sidecar_created(self, tmp_path: Path) -> None:
        """Constructing a FileReaderLayer must not create a .lock sidecar."""
        path = _cache_path(tmp_path)
        writer = FileWriterLayer(ttl=300, path=path)
        writer.store(REF, "value")
        path.with_name(path.name + ".lock").unlink(missing_ok=True)

        FileReaderLayer(path=path)
        assert not path.with_name(path.name + ".lock").exists()

    def test_missing_file_returns_empty_no_directory_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reader on a non-existent path degrades to no entries without creating the dir."""
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir(mode=0o700)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

        # The default cache path would be runtime_dir/op-core/cache.bin; op-core/ does not exist.
        reader = FileReaderLayer()  # uses default path
        assert reader.lookup(REF) is None
        assert not (runtime_dir / "op-core").exists()

    def test_corrupt_file_ignored_not_scrubbed(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A corrupt cache file is silently ignored (not rewritten) by the reader."""
        import json
        import logging

        path = _cache_path(tmp_path)
        path.write_text(json.dumps({"bad": "format"}), encoding="utf-8")
        os.chmod(path, 0o600)
        before = path.read_bytes()

        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            reader = FileReaderLayer(path=path)
        assert reader.lookup(REF) is None
        assert path.read_bytes() == before  # file untouched

    def test_loose_perms_file_ignored_not_scrubbed(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A file with loose permissions is ignored and not scrubbed by the reader."""
        import logging

        path = _cache_path(tmp_path)
        writer = FileWriterLayer(ttl=300, path=path)
        writer.store(REF, "value")
        path.with_name(path.name + ".lock").unlink(missing_ok=True)
        os.chmod(path, 0o644)
        before = path.read_bytes()

        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            reader = FileReaderLayer(path=path)
        assert reader.lookup(REF) is None
        assert path.read_bytes() == before  # not scrubbed


# ---------------------------------------------------------------------------
# clear_cache_file — the whole-file hammer
# ---------------------------------------------------------------------------


class TestClearCacheFile:
    def test_deletes_the_cache_file(self, tmp_path: Path) -> None:
        """clear_cache_file removes the cache file."""
        path = _cache_path(tmp_path)
        writer = FileWriterLayer(ttl=300, path=path)
        writer.store(REF, "value")
        assert path.exists()

        clear_cache_file(path)
        assert not path.exists()

    def test_fix1_leaves_lock_sidecar_in_place(self, tmp_path: Path) -> None:
        """FIX-1: clear_cache_file must NOT unlink the .lock sidecar.

        Unlinking the sidecar while holding its flock lets a concurrent writer
        open a fresh inode and bypass the lock. The sidecar stays; only the
        cache file is deleted.
        """
        path = _cache_path(tmp_path)
        writer = FileWriterLayer(ttl=300, path=path)
        writer.store(REF, "value")
        lock_path = path.with_name(path.name + ".lock")
        assert lock_path.exists()

        clear_cache_file(path)
        assert not path.exists()
        assert lock_path.exists()

    def test_missing_cache_file_is_noop(self, tmp_path: Path) -> None:
        """clear_cache_file on an absent cache file must not raise."""
        path = _cache_path(tmp_path)
        # Create the directory (and the lock sidecar) without the cache file.
        path.parent.mkdir(parents=True, exist_ok=True)
        clear_cache_file(path)  # must not raise

    def test_missing_directory_is_noop(self, tmp_path: Path) -> None:
        """clear_cache_file on a path whose directory doesn't exist must not raise."""
        clear_cache_file(tmp_path / "nonexistent" / "cache.bin")  # must not raise

    def test_mutual_exclusion_with_concurrent_writer(self, tmp_path: Path) -> None:
        """FIX-1 / concurrency: a writer that starts after clear finishes on an empty slate.

        A concurrent writer that acquires the flock AFTER clear cannot resurrect
        cleared entries — it starts from scratch on the empty path. We verify
        this by: (1) blocking the writer on the flock, (2) clear runs and
        completes, (3) writer runs and stores a fresh value.  After the round-
        trip, only the fresh value is present (the pre-clear value is gone).
        """
        path = _cache_path(tmp_path)
        writer = FileWriterLayer(ttl=300, path=path)
        writer.store(REF, "pre-clear-value")
        assert path.exists()

        lock_path = path.with_name(path.name + ".lock")
        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def hold_flock() -> None:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                lock_acquired.set()
                release_lock.wait(timeout=5.0)
            finally:
                os.close(fd)

        holder = threading.Thread(target=hold_flock, daemon=True)
        holder.start()
        assert lock_acquired.wait(timeout=5.0), "lock holder did not acquire in time"

        # clear_cache_file blocks until the holder releases.
        clear_done = threading.Event()

        def do_clear() -> None:
            clear_cache_file(path)
            clear_done.set()

        clearer = threading.Thread(target=do_clear, daemon=True)
        clearer.start()

        # Clear is blocked (holder has the lock). Release and wait.
        release_lock.set()
        holder.join(timeout=5.0)
        assert clear_done.wait(timeout=5.0), "clear did not complete after lock release"

        # File is gone after clear.
        assert not path.exists()

        # A fresh writer stores a new entry — starting from an empty file.
        fresh = FileWriterLayer(ttl=300, path=path)
        fresh.store(REF, "post-clear-value")

        sets = read_sets(path)
        assert sets["default"]["entries"][REF]["value"] == "post-clear-value"
