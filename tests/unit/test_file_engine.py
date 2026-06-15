"""Engine, security, and concurrency tests for the file-cache layer types.

These tests were ported from the legacy decorator suites (test_file_caching_backend.py
and test_cache_file.py) and drive through FileWriterLayer / module-level functions
instead of the retired FileCachingBackend / AsyncFileCachingBackend / CacheFile classes.

Case-mapping checklist
======================

PORTED (old test -> new test name):
  TestScrambledFormat::test_no_plaintext_on_disk
    -> TestScrambledFormat::test_no_plaintext_on_disk
  TestScrambledFormat::test_foreign_machine_cache_is_discarded
    -> TestScrambledFormat::test_foreign_machine_cache_is_discarded
  TestScrambledFormat::test_unreadable_cache_file_is_scrubbed_on_load
    -> TestScrambledFormat::test_unreadable_cache_file_is_scrubbed_on_load
  TestFileSecurity::test_cache_file_is_0600
    -> TestFileSecurity::test_cache_file_is_0600
  TestFileSecurity::test_cache_dir_is_0700
    -> TestFileSecurity::test_cache_dir_is_0700
  TestFileSecurity::test_loose_perms_file_is_ignored
    -> TestFileSecurity::test_loose_perms_file_is_ignored_on_writer_load
  TestCorruption::test_corrupt_json_falls_back
    -> TestCorruption::test_corrupt_json_writer_load_returns_none
  TestVersionGate::test_wrong_version_degrades_to_inner
    -> TestCorruption::test_foreign_version_payload_writer_load_returns_none
  TestTruncatedBlob::test_truncated_file_degrades_to_inner
    -> TestCorruption::test_truncated_blob_writer_load_returns_none
  TestCorruption::test_missing_file_is_normal
    -> TestCorruption::test_missing_file_is_normal
  TestPersistenceDisabled::test_insecure_dir_degrades_gracefully
    -> TestPersistenceDisabled::test_insecure_dir_degrades_gracefully
  TestWriteFailure::test_write_error_does_not_crash
    -> TestWriteFailure::test_write_error_logs_and_does_not_crash
  TestPermissionDeniedPersist::test_read_succeeds_and_warns_when_cache_dir_unwritable
    -> TestPermissionDeniedPersist::test_store_logs_and_does_not_crash_when_dir_unwritable
  TestPerSetTTL::test_ttl_mismatch_restamps_set
    -> TestPerSetTTL::test_ttl_mismatch_restamps_set
  TestPerSetTTL::test_same_ttl_reuses_set
    -> TestPerSetTTL::test_same_ttl_reuses_set
  TestSets::test_load_purges_expired_entries_of_other_sets
    -> TestSets::test_load_purges_expired_entries_of_other_sets
  TestSets::test_persist_preserves_sets_written_by_others
    -> TestSets::test_persist_preserves_sets_written_by_others
  TestSets::test_same_set_concurrent_writers_merge_entries
    -> TestSets::test_same_set_concurrent_writers_merge_entries
  TestEqualTimestampTieBreak::test_local_wins_on_equal_cached_at
    -> TestEqualTimestampTieBreak::test_local_wins_on_equal_cached_at
  TestUnicodeRoundTrip::test_non_ascii_value_persists_across_instances
    -> TestUnicodeRoundTrip::test_unicode_value_round_trips
  TestMachineMaterialFallback::test_round_trip_with_uid_only_keying
    -> TestMachineMaterialFallback::test_round_trip_with_uid_only_keying
  TestFlockContention::test_persist_completes_after_lock_release
    -> TestFlockContention::test_persist_completes_after_lock_release
  TestDefaultCacheDir::test_prefers_xdg_runtime_dir
    -> TestDefaultCacheDir::test_prefers_xdg_runtime_dir
  TestDefaultCacheDir::test_falls_back_to_tmpdir
    -> TestDefaultCacheDir::test_falls_back_to_tmpdir
  TestCrossProcess::test_second_instance_reads_from_file (cross-instance persistence)
    -> TestCrossInstancePersistence::test_value_stored_by_writer_visible_to_fresh_writer

DROPPED (with justification):
  TestInProcess::test_second_read_served_from_memory
  TestInProcess::test_lru_eviction
    -> in-process LRU eviction is tested directly in test_caching_backend.py;
       FileWriterLayer max_entries (disk trim) is covered in test_file_layers.py::max_entries_trim.
  TestTTL::test_expiry_across_instances
  TestTTL::test_within_ttl_served_from_file
    -> TTL expiry is covered in test_file_layers.py::TestFileWriterLayerBasic::test_ttl_expiry_across_instances.
  TestPersistenceDisabled::test_ttl_zero_writes_no_file
  TestPersistenceDisabled::test_ttl_zero_does_not_persist_across_instances
    -> ttl=0 behavior is covered in test_file_layers.py::test_ttl_zero_writes_no_file_and_does_not_persist.
  TestOffline::* (online=False / default_value behavior)
    -> resolver semantics; covered in test_resolver_stack.py and test_async_resolver_stack.py.
       The file layer has no .read(), only .lookup().
  TestPassthrough::test_list_items_not_cached
  TestPassthrough::test_list_vaults_not_cached
  TestPassthrough::test_get_item_passes_through
    -> ResolverStack delegation; covered in test_resolver_stack.py.
  TestSyncConcurrentReads::test_concurrent_reads_return_correct_value
    -> the decorator's threading.Lock is removed with the class; the resolver owns locking.
       Covered by test_resolver_stack.py::TestLocking::test_simultaneous_misses_both_reach_source.
  TestMaxEntriesTrim::test_persist_trims_to_max_entries_keeping_newest
    -> covered in test_file_layers.py::TestFileWriterLayerBasic::test_max_entries_trim.
  TestPerSetTTL::test_ttl_mismatch_reconstructs_set
    -> covered in test_file_layers.py::test_per_set_ttl_mismatch_reconstructs.
  TestAsync::* (AsyncFileCachingBackend tests)
    -> AsyncFileCachingBackend is retired; async file persistence is now AsyncResolverStack +
       FileWriterLayer, tested in test_async_resolver_stack.py. One sanity test is kept here:
       TestCrossInstancePersistence::test_value_stored_by_writer_visible_to_fresh_writer.
  TestCacheFileSurface::* (CacheFile construction / factory tests)
  TestCacheFileClear::* / TestWriterClear::*
    -> CacheFile and FileCachingBackend.clear() are retired; clear_cache_file() and
       FileWriterLayer.clear() / clear_misses() are covered in test_file_layers.py.
  TestReaderServes::* / TestReaderExpiry::* / TestReaderNeverWrites::* / TestReaderPassthrough::*
  TestAsyncReader::* / TestAsyncWriterClear::*
    -> CacheFile.reader() and CacheFile.async_reader() are retired; the reader role is
       FileReaderLayer, covered in test_file_layers.py.
  TestSets::test_sets_isolate_the_same_reference
    -> covered in test_file_layers.py::TestFileWriterLayerBasic::test_bucket_isolation.
  TestCrossProcess::test_negative_cache_persists
  TestCrossProcess::test_persisted_miss_honors_default_value
  TestCrossProcess::test_cache_file_round_trips_through_scrambled_format
    -> negative-cache / default_value semantics live in the resolver stack;
       scrambled round-trip is covered by TestCrossInstancePersistence and the
       scrambled-format tests above.
"""

from __future__ import annotations

import fcntl
import logging
import os
import stat
import threading
from pathlib import Path

import pytest

from op_core.backends import file_caching
from op_core.backends.file_caching import FileWriterLayer, default_cache_dir
from tests.unit.cache_helpers import read_sets

REF = "op://Vault/Item/field"


def _cache_path(tmp_path: Path) -> Path:
    return tmp_path / "cache.bin"


# ---------------------------------------------------------------------------
# Scrambled on-disk format (obfuscation, not encryption)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def clear_derive_key_cache():  # type: ignore[return]  # yield fixture; return type is Generator
    """Clear the _derive_key lru_cache before and after every test that touches _machine_material.

    Without this, monkeypatching _machine_material would corrupt the cached key for
    subsequent tests even after the monkeypatch teardown restores the original function.
    """
    file_caching._derive_key.cache_clear()
    yield
    file_caching._derive_key.cache_clear()


class TestScrambledFormat:
    def test_no_plaintext_on_disk(self, tmp_path: Path) -> None:
        """Neither secret values nor op:// references appear in the raw file bytes."""
        path = _cache_path(tmp_path)
        layer = FileWriterLayer(ttl=300, path=path)
        layer.store(REF, "hunter2-plaintext")
        blob = path.read_bytes()
        assert blob.startswith(file_caching._MAGIC)
        assert b"hunter2-plaintext" not in blob
        assert REF.encode("utf-8") not in blob

    def test_foreign_machine_cache_is_discarded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        clear_derive_key_cache: None,
    ) -> None:
        """A cache file copied from another machine cannot be unscrambled and is discarded."""
        path = _cache_path(tmp_path)
        writer = FileWriterLayer(ttl=300, path=path)
        writer.store(REF, "secret")

        # Clear the cached key before patching so the fresh instance derives a new key.
        monkeypatch.setattr(file_caching, "_machine_material", lambda: b"some-other-machine")
        file_caching._derive_key.cache_clear()
        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            fresh = FileWriterLayer(ttl=300, path=path)
        assert fresh.lookup(REF) is None
        assert any("corrupt" in r.message.lower() for r in caplog.records)

    def test_unreadable_cache_file_is_scrubbed_on_load(self, tmp_path: Path) -> None:
        """A trusted-but-undecodable file (stray plaintext) is rewritten on writer load."""
        import json

        path = _cache_path(tmp_path)
        path.write_text(
            json.dumps({"entries": {REF: {"value": "stray-plaintext-secret", "cached_at": 0}}}),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        # Construction triggers _load -> _read_sets -> rewrite (scrub).
        FileWriterLayer(ttl=300, path=path)
        assert b"stray-plaintext-secret" not in path.read_bytes()


# ---------------------------------------------------------------------------
# File security
# ---------------------------------------------------------------------------


class TestFileSecurity:
    def test_cache_file_is_0600(self, tmp_path: Path) -> None:
        """The cache file is written with mode 0600."""
        path = _cache_path(tmp_path)
        FileWriterLayer(ttl=300, path=path).store(REF, "secret")
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_cache_dir_is_0700(self, tmp_path: Path) -> None:
        """The cache directory is created with mode 0700."""
        path = _cache_path(tmp_path)
        FileWriterLayer(ttl=300, path=path).store(REF, "secret")
        assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700

    def test_loose_perms_file_is_ignored_on_writer_load(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A file widened to 0644 is ignored when a new writer loads; lookup returns None."""
        path = _cache_path(tmp_path)
        FileWriterLayer(ttl=300, path=path).store(REF, "secret")
        os.chmod(path, 0o644)

        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            fresh = FileWriterLayer(ttl=300, path=path)
        assert fresh.lookup(REF) is None
        assert any("permission" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Corruption tolerance
# ---------------------------------------------------------------------------


class TestCorruption:
    def test_corrupt_json_writer_load_returns_none(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Stray plaintext (corrupt JSON) at 0600 causes writer load to log 'corrupt', lookup returns None."""
        path = _cache_path(tmp_path)
        path.write_text("{ this is not json", encoding="utf-8")
        os.chmod(path, 0o600)

        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            writer = FileWriterLayer(ttl=300, path=path)
        assert writer.lookup(REF) is None
        assert any("corrupt" in r.message.lower() for r in caplog.records)

    def test_foreign_version_payload_writer_load_returns_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A properly-scrambled payload with an unsupported version fires the version check."""
        path = _cache_path(tmp_path)
        blob = file_caching._encode_payload({"version": 99, "sets": {}})
        path.write_bytes(blob)
        path.chmod(0o600)

        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            writer = FileWriterLayer(ttl=300, path=path)
        assert writer.lookup(REF) is None
        assert any("corrupt" in r.message.lower() for r in caplog.records)

    def test_truncated_blob_writer_load_returns_none(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A file with magic + nonce but no body cannot be unscrambled; lookup returns None."""
        path = _cache_path(tmp_path)
        path.write_bytes(file_caching._MAGIC + b"x" * file_caching._NONCE_LEN)
        path.chmod(0o600)

        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            writer = FileWriterLayer(ttl=300, path=path)
        assert writer.lookup(REF) is None
        assert any("corrupt" in r.message.lower() for r in caplog.records)

    def test_missing_file_is_normal(self, tmp_path: Path) -> None:
        """No cache file yet: writer constructs successfully, lookup returns None, no warning."""
        writer = FileWriterLayer(ttl=300, path=_cache_path(tmp_path))
        assert writer.lookup(REF) is None


# ---------------------------------------------------------------------------
# Disabled persistence: insecure directory
# ---------------------------------------------------------------------------


class TestPersistenceDisabled:
    def test_insecure_dir_degrades_gracefully(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """When the parent is a regular file the writer logs 'disabled' and store does not crash."""
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        path = blocker / "cache.bin"

        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            writer = FileWriterLayer(ttl=300, path=path)
            writer.store(REF, "secret")
        assert not path.exists()
        assert any("disabled" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Write failure degrades gracefully
# ---------------------------------------------------------------------------


class TestWriteFailure:
    def test_write_error_logs_and_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When _atomic_write raises OSError, store() does not crash and logs 'could not write'."""

        def boom(_path: Path, _blob: bytes) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(file_caching, "_atomic_write", boom)
        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            writer = FileWriterLayer(ttl=300, path=_cache_path(tmp_path))
            writer.store(REF, "secret")
        assert any("could not write" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _persist partial-failure: in-memory vs on-disk consistency
# ---------------------------------------------------------------------------


class TestPersistPartialFailure:
    def test_store_updates_memory_when_disk_write_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When _atomic_write raises mid-persist, the in-memory _store.put() has already
        been called (store() calls _store.put then _persist). The contract:

        - store() is NOT atomic across memory and disk.
        - The same layer object sees the new value in memory (lookup returns updated).
        - A fresh layer constructed from disk does NOT see the update (disk unchanged).
        - store() logs a warning and does not raise.
        """
        path = _cache_path(tmp_path)
        # Prime with a known value so disk has "original" before we inject the fault.
        FileWriterLayer(ttl=300, path=path).store(REF, "original")

        real_atomic_write = file_caching._atomic_write

        def always_fail(_path: Path, _blob: bytes) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(file_caching, "_atomic_write", always_fail)

        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            # Construction: _load reads the existing file; no dirty write (no expiry).
            writer = FileWriterLayer(ttl=300, path=path)
            # store() puts "updated" in memory then calls _persist which calls
            # _atomic_write -- which raises; _persist catches and logs.
            writer.store(REF, "updated")

        # In-memory: the writer's own lookup sees the updated value.
        assert writer.lookup(REF) is not None
        assert writer.lookup(REF).value == "updated"  # type: ignore[union-attr]

        # On-disk: the atomic write never completed, so a fresh layer sees "original".
        monkeypatch.setattr(file_caching, "_atomic_write", real_atomic_write)
        fresh = FileWriterLayer(ttl=300, path=path)
        assert fresh.lookup(REF) is not None
        assert fresh.lookup(REF).value == "original"  # type: ignore[union-attr]

        assert any("could not write" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Permission-denied persist: degrades gracefully
# ---------------------------------------------------------------------------


class TestPermissionDeniedPersist:
    def test_store_logs_and_does_not_crash_when_dir_unwritable(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When the cache directory is not writable, store() still returns and logs a warning."""
        path = _cache_path(tmp_path)
        # Prime so the directory exists at 0700.
        FileWriterLayer(ttl=300, path=path).store(REF, "seed")

        os.chmod(tmp_path, 0o500)
        try:
            with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
                writer = FileWriterLayer(ttl=300, path=path)
                writer.store("op://v/new", "fresh")
            assert any("could not write" in r.message for r in caplog.records)
        finally:
            os.chmod(tmp_path, 0o700)


# ---------------------------------------------------------------------------
# Per-set TTL
# ---------------------------------------------------------------------------


class TestPerSetTTL:
    def test_ttl_mismatch_restamps_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A TTL-mismatched writer discards the old set and restamps with the new TTL."""
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        writer1 = FileWriterLayer(ttl=3600, path=path)
        writer1.store(REF, "old-ttl-value")

        writer2 = FileWriterLayer(ttl=60, path=path)
        writer2.store(REF, "new-ttl-value")
        assert read_sets(path)["default"]["ttl"] == 60

    def test_same_ttl_reuses_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A second writer with the same TTL finds the entry already present (no re-store needed)."""
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        writer1 = FileWriterLayer(ttl=3600, path=path)
        writer1.store(REF, "cached-value")

        writer2 = FileWriterLayer(ttl=3600, path=path)
        entry = writer2.lookup(REF)
        assert entry is not None
        assert entry.value == "cached-value"


# ---------------------------------------------------------------------------
# Sets: cross-set purge and concurrent merge
# ---------------------------------------------------------------------------


class TestSets:
    def test_load_purges_expired_entries_of_other_sets(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Any writer load walks all sets and purges expired entries — not just its own."""
        path = _cache_path(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        writer_a = FileWriterLayer(ttl=100, bucket="a", path=path)
        writer_a.store(REF, "secret-a")

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 2000.0)
        # Constructing a writer for bucket 'b' triggers purge of set 'a'.
        FileWriterLayer(ttl=100, bucket="b", path=path)
        assert "a" not in read_sets(path)

    def test_persist_preserves_sets_written_by_others(self, tmp_path: Path) -> None:
        """A persist merges with the file; it must not clobber sets from other buckets."""
        path = _cache_path(tmp_path)
        writer_a = FileWriterLayer(ttl=300, bucket="a", path=path)
        writer_b = FileWriterLayer(ttl=300, bucket="b", path=path)
        writer_a.store(REF, "value-a")
        writer_b.store(REF, "value-b")
        sets = read_sets(path)
        assert set(sets) == {"a", "b"}

    def test_same_set_concurrent_writers_merge_entries(self, tmp_path: Path) -> None:
        """Two writers on the same bucket each store a different ref; both are visible."""
        path = _cache_path(tmp_path)
        writer_x = FileWriterLayer(ttl=300, bucket="s", path=path)
        writer_y = FileWriterLayer(ttl=300, bucket="s", path=path)
        writer_x.store("op://v/a", "1")
        writer_y.store("op://v/b", "2")

        fresh = FileWriterLayer(ttl=300, bucket="s", path=path)
        assert fresh.lookup("op://v/a") is not None
        assert fresh.lookup("op://v/b") is not None
        assert fresh.lookup("op://v/a").value == "1"  # type: ignore[union-attr]
        assert fresh.lookup("op://v/b").value == "2"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Equal-timestamp tie-break
# ---------------------------------------------------------------------------


class TestEqualTimestampTieBreak:
    def test_local_wins_on_equal_cached_at(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When disk and local entries have equal cached_at, the local (B) entry wins.

        Both writers are constructed while the file is empty (both load an empty store).
        A stores then B stores the same ref at the same pinned time. At B's persist,
        the disk has 'from-a' at 5000.0 and B's in-memory has 'from-b' at 5000.0.
        The merge condition ``disk["cached_at"] <= local["cached_at"]`` is True (equal),
        so local 'from-b' wins.
        """
        path = _cache_path(tmp_path)
        pinned_time = 5000.0
        monkeypatch.setattr(file_caching, "_wallclock", lambda: pinned_time)

        writer_a = FileWriterLayer(ttl=300, bucket="s", path=path)
        writer_b = FileWriterLayer(ttl=300, bucket="s", path=path)

        writer_a.store(REF, "from-a")
        writer_b.store(REF, "from-b")

        assert read_sets(path)["s"]["entries"][REF]["value"] == "from-b"


# ---------------------------------------------------------------------------
# Unicode round-trip
# ---------------------------------------------------------------------------


class TestUnicodeRoundTrip:
    def test_unicode_value_round_trips(self, tmp_path: Path) -> None:
        """Non-ASCII characters are preserved through the scramble/unscramble cycle."""
        path = _cache_path(tmp_path)
        unicode_value = "cafe-£-é漢字"
        writer1 = FileWriterLayer(ttl=300, path=path)
        writer1.store(REF, unicode_value)

        writer2 = FileWriterLayer(ttl=300, path=path)
        entry = writer2.lookup(REF)
        assert entry is not None
        assert entry.value == unicode_value


# ---------------------------------------------------------------------------
# machine-material uid-only fallback
# ---------------------------------------------------------------------------


class TestMachineMaterialFallback:
    def test_round_trip_with_uid_only_keying(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clear_derive_key_cache: None
    ) -> None:
        """Cache round-trips correctly when _MACHINE_ID_PATHS yields nothing (uid-only key)."""
        monkeypatch.setattr(file_caching, "_MACHINE_ID_PATHS", ("/nonexistent/a", "/nonexistent/b"))
        path = _cache_path(tmp_path)
        FileWriterLayer(ttl=300, path=path).store(REF, "uid-only-secret")

        fresh = FileWriterLayer(ttl=300, path=path)
        entry = fresh.lookup(REF)
        assert entry is not None
        assert entry.value == "uid-only-secret"


# ---------------------------------------------------------------------------
# flock contention
# ---------------------------------------------------------------------------


class TestFlockContention:
    def test_persist_completes_after_lock_release(self, tmp_path: Path) -> None:
        """A store() blocked by an external flock holder completes once the lock is released.

        Unlike the old decorator (which called inner.read before persist), FileWriterLayer.store
        goes straight to the flock-guarded _persist. We therefore construct the writer BEFORE
        the external lock is taken (so _load completes uncontested), then the store() call is
        what blocks on the flock.
        """
        path = _cache_path(tmp_path)
        # Construct the writer first so _load() completes before we take the external lock.
        writer = FileWriterLayer(ttl=300, path=path)

        # _load() creates the sidecar; hold an exclusive lock on it.
        lock_path = path.with_name(path.name + ".lock")
        lock_path.touch(exist_ok=True)

        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def hold_lock() -> None:
            fd = os.open(str(lock_path), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                lock_acquired.set()
                release_lock.wait(timeout=5.0)
            finally:
                os.close(fd)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        assert lock_acquired.wait(timeout=5.0), "lock holder did not acquire lock in time"

        persist_done = threading.Event()

        def do_store() -> None:
            writer.store(REF, "secret")
            persist_done.set()

        storer = threading.Thread(target=do_store, daemon=True)
        storer.start()

        # Give the storer a moment to reach the flock; verify it hasn't finished yet.
        storer.join(timeout=0.15)
        assert not persist_done.is_set(), "store completed before lock was released"

        # Release and wait for completion.
        release_lock.set()
        holder.join(timeout=5.0)
        assert persist_done.wait(timeout=5.0), "store did not complete after lock release"
        storer.join(timeout=5.0)

        sets = read_sets(path)
        assert sets["default"]["entries"][REF]["value"] == "secret"


# ---------------------------------------------------------------------------
# default_cache_dir
# ---------------------------------------------------------------------------


class TestDefaultCacheDir:
    def test_prefers_xdg_runtime_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """default_cache_dir() returns $XDG_RUNTIME_DIR/op-core at mode 0700."""
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        result = default_cache_dir()
        assert result == tmp_path / "op-core"
        assert result.is_dir()
        assert stat.S_IMODE(os.stat(result).st_mode) == 0o700

    def test_falls_back_to_tmpdir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without XDG_RUNTIME_DIR, default_cache_dir() falls back to $TMPDIR/op-core-<uid>."""
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        result = default_cache_dir()
        assert result.parent == tmp_path
        assert result.name.startswith("op-core-")
        assert stat.S_IMODE(os.stat(result).st_mode) == 0o700


# ---------------------------------------------------------------------------
# Cross-instance persistence sanity (async replacement)
# ---------------------------------------------------------------------------


class TestCrossInstancePersistence:
    def test_value_stored_by_writer_visible_to_fresh_writer(self, tmp_path: Path) -> None:
        """A value stored by one FileWriterLayer is visible to a fresh instance on the same path."""
        path = _cache_path(tmp_path)
        FileWriterLayer(ttl=300, path=path).store(REF, "persisted-value")

        entry = FileWriterLayer(ttl=300, path=path).lookup(REF)
        assert entry is not None
        assert entry.value == "persisted-value"
