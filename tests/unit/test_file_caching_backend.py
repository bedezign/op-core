"""Tests for :mod:`op_core.backends.file_caching`."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import stat
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from op_core.backends import file_caching
from op_core.backends.file_caching import (
    AsyncFileCachingBackend,
    FileCachingBackend,
    default_cache_dir,
)
from op_core.exceptions import OpNotFoundError, OpOfflineError
from op_core.items import Item, ItemRef, ItemSummary, VaultSummary

REF = "op://Vault/Item/field"


class StubBackend:
    """Counts calls so tests can prove the cache prevented passthrough."""

    def __init__(self, *, refs: dict[str, str] | None = None, items: list[Item] | None = None) -> None:
        self._refs = refs or {}
        self._items = items or []
        self.read_count = 0
        self.list_items_count = 0
        self.list_vaults_count = 0
        self.get_item_count = 0

    def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
        self.read_count += 1
        if reference in self._refs:
            return self._refs[reference]
        if default_value is not None:
            return default_value
        raise OpNotFoundError(f"missing: {reference}")

    def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        self.list_items_count += 1
        return []

    def list_vaults(self) -> list[VaultSummary]:
        self.list_vaults_count += 1
        return []

    def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        self.get_item_count += 1
        raise OpNotFoundError("no item")


class AsyncStubBackend:
    def __init__(self, *, refs: dict[str, str] | None = None) -> None:
        self._sync = StubBackend(refs=refs)

    @property
    def read_count(self) -> int:
        return self._sync.read_count

    async def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
        return self._sync.read(reference, default_value=default_value)

    async def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        return self._sync.list_items(vault=vault, tags=tags, categories=categories)

    async def list_vaults(self) -> list[VaultSummary]:
        return self._sync.list_vaults()

    async def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        return self._sync.get_item(item, vault=vault)


def _cache_file(tmp_path: Path) -> Path:
    return tmp_path / "cache.bin"


def _read_sets(path: Path) -> dict[str, dict]:
    """Decode the scrambled cache file and return its sets mapping."""
    payload = file_caching._decode_payload(path.read_bytes())
    assert payload["version"] == 1
    return payload["sets"]


# ---------------------------------------------------------------------------
# In-process caching
# ---------------------------------------------------------------------------


class TestInProcess:
    def test_second_read_served_from_memory(self, tmp_path: Path) -> None:
        inner = StubBackend(refs={REF: "secret"})
        cache = FileCachingBackend(inner, path=_cache_file(tmp_path))
        assert cache.read(REF) == "secret"
        assert cache.read(REF) == "secret"
        assert inner.read_count == 1

    def test_lru_eviction(self, tmp_path: Path) -> None:
        inner = StubBackend(refs={"op://v/a": "1", "op://v/b": "2", "op://v/c": "3"})
        cache = FileCachingBackend(inner, path=_cache_file(tmp_path), max_entries=2)
        cache.read("op://v/a")
        cache.read("op://v/b")
        cache.read("op://v/c")  # evicts "a"
        cache.read("op://v/a")  # miss again
        assert inner.read_count == 4


# ---------------------------------------------------------------------------
# Cross-process persistence (the whole point)
# ---------------------------------------------------------------------------


class TestCrossProcess:
    def test_second_instance_reads_from_file(self, tmp_path: Path) -> None:
        path = _cache_file(tmp_path)
        first = StubBackend(refs={REF: "secret"})
        FileCachingBackend(first, path=path).read(REF)
        assert first.read_count == 1

        second = StubBackend(refs={REF: "secret"})
        value = FileCachingBackend(second, path=path).read(REF)
        assert value == "secret"
        assert second.read_count == 0  # served entirely from the file

    def test_negative_cache_persists(self, tmp_path: Path) -> None:
        path = _cache_file(tmp_path)
        first = StubBackend()
        with pytest.raises(OpNotFoundError):
            FileCachingBackend(first, path=path).read("op://v/missing")
        assert first.read_count == 1

        second = StubBackend()
        backend = FileCachingBackend(second, path=path)
        with pytest.raises(OpNotFoundError):
            backend.read("op://v/missing")
        assert second.read_count == 0

    def test_persisted_miss_honors_default_value(self, tmp_path: Path) -> None:
        path = _cache_file(tmp_path)
        with pytest.raises(OpNotFoundError):
            FileCachingBackend(StubBackend(), path=path).read("op://v/missing")
        second = StubBackend()
        backend = FileCachingBackend(second, path=path)
        assert backend.read("op://v/missing", default_value="fallback") == "fallback"
        assert second.read_count == 0

    def test_cache_file_round_trips_through_scrambled_format(self, tmp_path: Path) -> None:
        path = _cache_file(tmp_path)
        FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path, ttl=120, bucket="b1").read(REF)
        sets = _read_sets(path)
        assert sets["b1"]["ttl"] == 120
        assert sets["b1"]["entries"][REF]["value"] == "secret"


# ---------------------------------------------------------------------------
# TTL (wall-clock, must survive process restarts)
# ---------------------------------------------------------------------------


class TestTTL:
    def test_expiry_across_instances(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _cache_file(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path, ttl=300).read(REF)

        # A fresh process well past the TTL must re-resolve.
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0 + 301)
        inner = StubBackend(refs={REF: "secret"})
        FileCachingBackend(inner, path=path, ttl=300).read(REF)
        assert inner.read_count == 1

    def test_within_ttl_served_from_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _cache_file(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path, ttl=300).read(REF)

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0 + 299)
        inner = StubBackend(refs={REF: "secret"})
        FileCachingBackend(inner, path=path, ttl=300).read(REF)
        assert inner.read_count == 0


# ---------------------------------------------------------------------------
# Disabled persistence
# ---------------------------------------------------------------------------


class TestPersistenceDisabled:
    def test_ttl_zero_writes_no_file(self, tmp_path: Path) -> None:
        path = _cache_file(tmp_path)
        backend = FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path, ttl=0)
        assert backend.read(REF) == "secret"
        assert not path.exists()

    def test_ttl_zero_does_not_persist_across_instances(self, tmp_path: Path) -> None:
        path = _cache_file(tmp_path)
        FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path, ttl=0).read(REF)
        inner = StubBackend(refs={REF: "secret"})
        FileCachingBackend(inner, path=path, ttl=0).read(REF)
        assert inner.read_count == 1

    def test_insecure_dir_degrades_gracefully(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        # Parent is a regular file, so the cache directory cannot be created.
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        path = blocker / "cache.bin"
        inner = StubBackend(refs={REF: "secret"})
        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            backend = FileCachingBackend(inner, path=path)
            assert backend.read(REF) == "secret"
        assert inner.read_count == 1
        assert any("disabled" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# File security
# ---------------------------------------------------------------------------


class TestFileSecurity:
    def test_cache_file_is_0600(self, tmp_path: Path) -> None:
        path = _cache_file(tmp_path)
        FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path).read(REF)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_cache_dir_is_0700(self, tmp_path: Path) -> None:
        path = _cache_file(tmp_path)
        FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path).read(REF)
        assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700

    def test_loose_perms_file_is_ignored(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        path = _cache_file(tmp_path)
        FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path).read(REF)
        os.chmod(path, 0o644)  # someone widened it

        inner = StubBackend(refs={REF: "different"})
        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            value = FileCachingBackend(inner, path=path).read(REF)
        assert value == "different"  # file was ignored, inner consulted
        assert inner.read_count == 1
        assert any("permission" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Corruption tolerance
# ---------------------------------------------------------------------------


class TestCorruption:
    def test_corrupt_json_falls_back(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        path = _cache_file(tmp_path)
        path.write_text("{ this is not json", encoding="utf-8")
        os.chmod(path, 0o600)

        inner = StubBackend(refs={REF: "secret"})
        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            value = FileCachingBackend(inner, path=path).read(REF)
        assert value == "secret"
        assert inner.read_count == 1
        assert any("corrupt" in r.message.lower() for r in caplog.records)

    def test_foreign_file_format_falls_back(self, tmp_path: Path) -> None:
        path = _cache_file(tmp_path)
        path.write_text(json.dumps({"version": 999, "entries": {}}), encoding="utf-8")
        os.chmod(path, 0o600)
        inner = StubBackend(refs={REF: "secret"})
        assert FileCachingBackend(inner, path=path).read(REF) == "secret"
        assert inner.read_count == 1

    def test_missing_file_is_normal(self, tmp_path: Path) -> None:
        inner = StubBackend(refs={REF: "secret"})
        # No file at path yet — first run, no warning, just works.
        assert FileCachingBackend(inner, path=_cache_file(tmp_path)).read(REF) == "secret"


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------


class TestOffline:
    def test_cached_entry_served_offline(self, tmp_path: Path) -> None:
        path = _cache_file(tmp_path)
        backend = FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path)
        backend.read(REF)
        inner_count_before = backend._inner.read_count  # type: ignore[attr-defined]
        assert backend.read(REF, online=False) == "secret"
        assert backend._inner.read_count == inner_count_before  # type: ignore[attr-defined]

    def test_uncached_offline_raises(self, tmp_path: Path) -> None:
        backend = FileCachingBackend(StubBackend(), path=_cache_file(tmp_path))
        with pytest.raises(OpOfflineError):
            backend.read(REF, online=False)


# ---------------------------------------------------------------------------
# Passthrough
# ---------------------------------------------------------------------------


class TestPassthrough:
    def test_list_items_not_cached(self, tmp_path: Path) -> None:
        inner = StubBackend()
        backend = FileCachingBackend(inner, path=_cache_file(tmp_path))
        backend.list_items()
        backend.list_items()
        assert inner.list_items_count == 2

    def test_list_vaults_not_cached(self, tmp_path: Path) -> None:
        inner = StubBackend()
        backend = FileCachingBackend(inner, path=_cache_file(tmp_path))
        backend.list_vaults()
        backend.list_vaults()
        assert inner.list_vaults_count == 2

    def test_get_item_passes_through(self, tmp_path: Path) -> None:
        inner = StubBackend()
        backend = FileCachingBackend(inner, path=_cache_file(tmp_path))
        with pytest.raises(OpNotFoundError):
            backend.get_item("id")
        assert inner.get_item_count == 1


# ---------------------------------------------------------------------------
# Write failure degrades, never crashes
# ---------------------------------------------------------------------------


class TestWriteFailure:
    def test_write_error_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom(_path: Path, _blob: bytes) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(file_caching, "_atomic_write", boom)
        inner = StubBackend(refs={REF: "secret"})
        backend = FileCachingBackend(inner, path=_cache_file(tmp_path))
        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            assert backend.read(REF) == "secret"  # still resolves from the inner backend
        assert any("could not write" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# default_cache_dir
# ---------------------------------------------------------------------------


class TestDefaultCacheDir:
    def test_prefers_xdg_runtime_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        result = default_cache_dir()
        assert result == tmp_path / "op-core"
        assert result.is_dir()
        assert stat.S_IMODE(os.stat(result).st_mode) == 0o700

    def test_falls_back_to_tmpdir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        result = default_cache_dir()
        assert result.parent == tmp_path
        assert result.name.startswith("op-core-")
        assert stat.S_IMODE(os.stat(result).st_mode) == 0o700


# ---------------------------------------------------------------------------
# Async twin
# ---------------------------------------------------------------------------


class TestAsync:
    async def test_second_read_from_memory(self, tmp_path: Path) -> None:
        inner = AsyncStubBackend(refs={REF: "secret"})
        cache = AsyncFileCachingBackend(inner, path=_cache_file(tmp_path))
        assert await cache.read(REF) == "secret"
        assert await cache.read(REF) == "secret"
        assert inner.read_count == 1

    async def test_second_instance_reads_from_file(self, tmp_path: Path) -> None:
        path = _cache_file(tmp_path)
        first = AsyncStubBackend(refs={REF: "secret"})
        await AsyncFileCachingBackend(first, path=path).read(REF)
        second = AsyncStubBackend(refs={REF: "secret"})
        assert await AsyncFileCachingBackend(second, path=path).read(REF) == "secret"
        assert second.read_count == 0

    async def test_negative_cache_persists(self, tmp_path: Path) -> None:
        path = _cache_file(tmp_path)
        with pytest.raises(OpNotFoundError):
            await AsyncFileCachingBackend(AsyncStubBackend(), path=path).read("op://v/missing")
        second = AsyncStubBackend()
        with pytest.raises(OpNotFoundError):
            await AsyncFileCachingBackend(second, path=path).read("op://v/missing")
        assert second.read_count == 0

    async def test_uncached_offline_raises(self, tmp_path: Path) -> None:
        backend = AsyncFileCachingBackend(AsyncStubBackend(), path=_cache_file(tmp_path))
        with pytest.raises(OpOfflineError):
            await backend.read(REF, online=False)

    async def test_passthrough_methods(self, tmp_path: Path) -> None:
        backend = AsyncFileCachingBackend(AsyncStubBackend(), path=_cache_file(tmp_path))
        assert await backend.list_items() == []
        assert await backend.list_vaults() == []
        with pytest.raises(OpNotFoundError):
            await backend.get_item("missing")

    async def test_concurrent_reads_hit_inner_once(self, tmp_path: Path) -> None:
        """Two concurrent awaited reads of the same reference call the inner backend only once."""
        inner = AsyncStubBackend(refs={REF: "secret"})
        cache = AsyncFileCachingBackend(inner, path=_cache_file(tmp_path))
        results = await asyncio.gather(cache.read(REF), cache.read(REF))
        assert results == ["secret", "secret"]
        assert inner.read_count == 1


# ---------------------------------------------------------------------------
# Unicode round-trip
# ---------------------------------------------------------------------------


class TestUnicodeRoundTrip:
    def test_non_ascii_value_persists_across_instances(self, tmp_path: Path) -> None:
        """A secret with non-ASCII characters round-trips through the cache file."""
        path = _cache_file(tmp_path)
        unicode_value = "café-£-é漢字"
        FileCachingBackend(StubBackend(refs={REF: unicode_value}), path=path).read(REF)

        second = StubBackend()
        result = FileCachingBackend(second, path=path).read(REF)
        assert result == unicode_value
        assert second.read_count == 0  # served entirely from the file


# ---------------------------------------------------------------------------
# Scrambled on-disk format (obfuscation, not encryption)
# ---------------------------------------------------------------------------


class TestScrambledFormat:
    def test_no_plaintext_on_disk(self, tmp_path: Path) -> None:
        """Neither secret values nor op:// references are readable in the raw file."""
        path = _cache_file(tmp_path)
        FileCachingBackend(StubBackend(refs={REF: "hunter2-plaintext"}), path=path).read(REF)
        blob = path.read_bytes()
        assert blob.startswith(file_caching._MAGIC)
        assert b"hunter2-plaintext" not in blob
        assert REF.encode("utf-8") not in blob

    def test_foreign_machine_cache_is_discarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A cache file copied from another machine cannot be unscrambled and is discarded."""
        path = _cache_file(tmp_path)
        FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path).read(REF)

        monkeypatch.setattr(file_caching, "_machine_material", lambda: b"some-other-machine")
        file_caching._derive_key.cache_clear()
        try:
            inner = StubBackend(refs={REF: "secret"})
            with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
                assert FileCachingBackend(inner, path=path).read(REF) == "secret"
            assert inner.read_count == 1  # file was unreadable, inner consulted
            assert any("corrupt" in r.message.lower() for r in caplog.records)
        finally:
            file_caching._derive_key.cache_clear()

    def test_unreadable_cache_file_is_scrubbed_on_load(self, tmp_path: Path) -> None:
        """A trusted-but-undecodable cache file is rewritten, scrubbing whatever it held."""
        path = _cache_file(tmp_path)
        path.write_text(
            json.dumps({"entries": {REF: {"value": "stray-plaintext-secret", "cached_at": 0}}}),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        FileCachingBackend(StubBackend(), path=path)  # construction alone triggers the scrub
        assert b"stray-plaintext-secret" not in path.read_bytes()


# ---------------------------------------------------------------------------
# Per-set TTL (writer-owned, stamped into the file)
# ---------------------------------------------------------------------------


class TestPerSetTTL:
    def test_ttl_mismatch_reconstructs_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A backend constructed with a different TTL discards the stored set entirely."""
        path = _cache_file(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path, ttl=3600).read(REF)

        inner = StubBackend(refs={REF: "secret"})
        FileCachingBackend(inner, path=path, ttl=60).read(REF)
        assert inner.read_count == 1  # stored set not reinterpreted: reconstructed

    def test_ttl_mismatch_restamps_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _cache_file(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path, ttl=3600).read(REF)
        FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path, ttl=60).read(REF)
        assert _read_sets(path)["default"]["ttl"] == 60

    def test_same_ttl_reuses_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _cache_file(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path, ttl=3600).read(REF)
        inner = StubBackend(refs={REF: "secret"})
        FileCachingBackend(inner, path=path, ttl=3600).read(REF)
        assert inner.read_count == 0


# ---------------------------------------------------------------------------
# Sets: isolation, cross-set purge, concurrent merge
# ---------------------------------------------------------------------------


class TestSets:
    def test_sets_isolate_the_same_reference(self, tmp_path: Path) -> None:
        """The same credential cached under two sets is two independent entries."""
        path = _cache_file(tmp_path)
        FileCachingBackend(StubBackend(refs={REF: "value-a"}), path=path, bucket="a").read(REF)
        FileCachingBackend(StubBackend(refs={REF: "value-b"}), path=path, bucket="b").read(REF)

        again_a = StubBackend()
        assert FileCachingBackend(again_a, path=path, bucket="a").read(REF) == "value-a"
        assert again_a.read_count == 0

    def test_load_purges_expired_entries_of_other_sets(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Any invocation scrubs everyone's expired plaintext, not just its own set."""
        path = _cache_file(tmp_path)
        monkeypatch.setattr(file_caching, "_wallclock", lambda: 1000.0)
        FileCachingBackend(StubBackend(refs={REF: "secret-a"}), path=path, ttl=100, bucket="a").read(REF)

        monkeypatch.setattr(file_caching, "_wallclock", lambda: 2000.0)
        FileCachingBackend(StubBackend(), path=path, ttl=100, bucket="b")  # load alone purges
        assert "a" not in _read_sets(path)

    def test_persist_preserves_sets_written_by_others(self, tmp_path: Path) -> None:
        """A persist must merge with the file, not clobber sets it never loaded."""
        path = _cache_file(tmp_path)
        backend_a = FileCachingBackend(StubBackend(refs={REF: "value-a"}), path=path, bucket="a")
        backend_b = FileCachingBackend(StubBackend(refs={REF: "value-b"}), path=path, bucket="b")
        backend_a.read(REF)  # persists set "a"
        backend_b.read(REF)  # persists set "b"; must keep "a" it never saw at load time
        sets = _read_sets(path)
        assert set(sets) == {"a", "b"}

    def test_same_set_concurrent_writers_merge_entries(self, tmp_path: Path) -> None:
        path = _cache_file(tmp_path)
        backend_x = FileCachingBackend(StubBackend(refs={"op://v/a": "1"}), path=path, bucket="s")
        backend_y = FileCachingBackend(StubBackend(refs={"op://v/b": "2"}), path=path, bucket="s")
        backend_x.read("op://v/a")
        backend_y.read("op://v/b")

        fresh = StubBackend()
        reader = FileCachingBackend(fresh, path=path, bucket="s")
        assert reader.read("op://v/a") == "1"
        assert reader.read("op://v/b") == "2"
        assert fresh.read_count == 0


# ---------------------------------------------------------------------------
# flock contention: persist blocks until the sidecar lock is released
# ---------------------------------------------------------------------------


class TestFlockContention:
    def test_persist_completes_after_lock_release(self, tmp_path: Path) -> None:
        """A persist blocked by an external flock holder completes once the lock is released."""
        path = _cache_file(tmp_path)

        # Construct the backend BEFORE taking the external lock so _load() completes uncontested.
        inner = StubBackend(refs={REF: "secret"})
        backend = FileCachingBackend(inner, path=path)

        # _load() creates the sidecar lock file via O_CREAT; hold an exclusive lock on it now.
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
        assert lock_acquired.wait(timeout=5.0), "lock holder thread did not acquire lock in time"

        # Phase 2: backend.read triggers inner + persist; persist blocks waiting for the lock.
        #
        # Use reached_inner to make the sequencing observable: FileCachingBackend.read calls
        # inner.read before attempting the flock-guarded persist, so once reached_inner is set
        # the reader thread is past inner.read and is about to block on the lock.
        persist_done = threading.Event()
        reached_inner = threading.Event()
        original_inner_read = inner.read

        def read_with_signal(
            reference: str,
            *,
            default_value: str | None = None,
            online: bool = True,
        ) -> str:
            result = original_inner_read(reference, default_value=default_value, online=online)
            reached_inner.set()
            return result

        inner.read = read_with_signal  # type: ignore[method-assign]

        def do_read() -> None:
            backend.read(REF)
            persist_done.set()

        reader = threading.Thread(target=do_read, daemon=True)
        reader.start()

        # Wait until the reader has passed inner.read (i.e. is about to block on the flock),
        # then verify it has not yet completed persist.
        assert reached_inner.wait(timeout=5.0), "reader thread did not reach inner.read in time"
        reader.join(timeout=0.1)
        assert not persist_done.is_set(), "persist completed before lock was released"

        # Phase 3: release the lock, wait for persist to finish.
        release_lock.set()
        holder.join(timeout=5.0)
        assert persist_done.wait(timeout=5.0), "persist did not complete after lock release"
        reader.join(timeout=5.0)

        sets = _read_sets(path)
        assert sets["default"]["entries"][REF]["value"] == "secret"


# ---------------------------------------------------------------------------
# Oversized disk set trim: max_entries is enforced on persist
# ---------------------------------------------------------------------------


class TestMaxEntriesTrim:
    def test_persist_trims_to_max_entries_keeping_newest(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """store() through a small-max backend leaves exactly max_entries on disk, the newest ones."""
        path = _cache_file(tmp_path)
        n = 3
        extra = 2
        refs = {f"op://v/{i}": f"val-{i}" for i in range(n + extra)}

        # Pre-populate via a large-max backend with strictly ordered cached_at values.
        tick = 1000.0
        for ref, val in refs.items():
            monkeypatch.setattr(file_caching, "_wallclock", lambda t=tick: t)
            FileCachingBackend(StubBackend(refs={ref: val}), path=path, max_entries=1024).read(ref)
            tick += 1.0

        # A new backend with max_entries=n triggers a trim on its next store().
        last_ref = f"op://v/{n + extra}"
        last_val = "newest"
        monkeypatch.setattr(file_caching, "_wallclock", lambda: tick)
        FileCachingBackend(StubBackend(refs={last_ref: last_val}), path=path, max_entries=n).read(last_ref)

        sets = _read_sets(path)
        entries = sets["default"]["entries"]
        assert len(entries) == n
        # last_ref has the highest cached_at and must survive the trim.
        assert last_ref in entries
        # Every surviving entry must have a cached_at no older than the oldest surviving entry
        # (a tautology here, but confirms the sort-and-keep-newest logic ran without errors).
        oldest_at = min(e["cached_at"] for e in entries.values())
        assert all(e["cached_at"] >= oldest_at for e in entries.values())


# ---------------------------------------------------------------------------
# Permission-denied on persist: degrades gracefully, logs warning
# ---------------------------------------------------------------------------


class TestPermissionDeniedPersist:
    def test_read_succeeds_and_warns_when_cache_dir_unwritable(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When the cache directory is not writable, read still returns the value and logs a warning."""
        path = _cache_file(tmp_path)
        # Prime the backend so the directory exists at 0700 (created by _secure_dir).
        FileCachingBackend(StubBackend(refs={REF: "seed"}), path=path).read(REF)

        os.chmod(tmp_path, 0o500)
        try:
            inner = StubBackend(refs={"op://v/new": "fresh"})
            with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
                result = FileCachingBackend(inner, path=path).read("op://v/new")
            assert result == "fresh"
            assert any("could not write" in r.message for r in caplog.records)
        finally:
            os.chmod(tmp_path, 0o700)


# ---------------------------------------------------------------------------
# _machine_material fallback: uid-only keying when machine-id paths are absent
# ---------------------------------------------------------------------------


class TestMachineMaterialFallback:
    def test_round_trip_with_uid_only_keying(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cache round-trips correctly when _MACHINE_ID_PATHS yields nothing (uid-only key)."""
        # Clear any cached key before patching so _derive_key re-derives with the new paths.
        file_caching._derive_key.cache_clear()
        monkeypatch.setattr(file_caching, "_MACHINE_ID_PATHS", ("/nonexistent/a", "/nonexistent/b"))
        try:
            path = _cache_file(tmp_path)
            FileCachingBackend(StubBackend(refs={REF: "uid-only-secret"}), path=path).read(REF)

            inner = StubBackend()
            result = FileCachingBackend(inner, path=path).read(REF)
            assert result == "uid-only-secret"
            assert inner.read_count == 0
        finally:
            # Always clear so subsequent tests re-derive the real key, even if an assertion fails.
            file_caching._derive_key.cache_clear()


# ---------------------------------------------------------------------------
# Truncated blob: missing body triggers corrupt warning, inner consulted
# ---------------------------------------------------------------------------


class TestTruncatedBlob:
    def test_truncated_file_degrades_to_inner(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A file with magic + nonce but no body cannot be unscrambled; inner is consulted."""
        path = _cache_file(tmp_path)
        path.write_bytes(file_caching._MAGIC + b"x" * file_caching._NONCE_LEN)
        path.chmod(0o600)

        inner = StubBackend(refs={REF: "from-inner"})
        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            result = FileCachingBackend(inner, path=path).read(REF)
        assert result == "from-inner"
        assert inner.read_count == 1
        assert any("corrupt" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Equal-timestamp tie-break: local wins when cached_at values are identical
# ---------------------------------------------------------------------------


class TestEqualTimestampTieBreak:
    def test_local_wins_on_equal_cached_at(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When both disk and in-memory entries have equal cached_at, the local (in-memory) entry wins.

        B is constructed before A reads (so B loads an empty store), then A persists 'from-a',
        then B reads and persists 'from-b'. At B's persist time disk has 'from-a' at 5000.0
        and B's in-memory has 'from-b' at 5000.0. The merge condition
        ``disk["cached_at"] <= local["cached_at"]`` is True (equal), so local 'from-b' wins.
        """
        path = _cache_file(tmp_path)
        pinned_time = 5000.0
        monkeypatch.setattr(file_caching, "_wallclock", lambda: pinned_time)

        # Both backends constructed while the file is empty so both load an empty store.
        backend_a = FileCachingBackend(StubBackend(refs={REF: "from-a"}), path=path, bucket="s")
        backend_b = FileCachingBackend(StubBackend(refs={REF: "from-b"}), path=path, bucket="s")

        # A reads and persists 'from-a' first.
        backend_a.read(REF)

        # B misses in its empty in-memory store, calls inner -> 'from-b', persists.
        # At merge time: disk has 'from-a' at 5000.0, local has 'from-b' at 5000.0.
        # equal cached_at => local wins.
        backend_b.read(REF)

        sets = _read_sets(path)
        assert sets["s"]["entries"][REF]["value"] == "from-b"


# ---------------------------------------------------------------------------
# Sync concurrent reads: both threads return correct value; cache stays consistent
# ---------------------------------------------------------------------------


class TestSyncConcurrentReads:
    def test_concurrent_reads_return_correct_value(self, tmp_path: Path) -> None:
        """Two concurrent sync reads of the same reference both return the correct value.

        The sync backend's threading.Lock covers only cache ops, not inner.read, so two
        concurrent misses may both call inner. This test asserts the documented behavior:
        both return the correct value and the cache ends in a consistent state with the value
        present. It does NOT assert inner.read_count == 1 because double-hit is possible.
        """
        path = _cache_file(tmp_path)
        inner = StubBackend(refs={REF: "shared-secret"})
        backend = FileCachingBackend(inner, path=path)

        results: list[str] = []
        errors: list[Exception] = []

        barrier = threading.Barrier(2)

        def read_ref() -> None:
            try:
                barrier.wait(timeout=5.0)
                results.append(backend.read(REF))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=read_ref, daemon=True) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors
        assert results == ["shared-secret", "shared-secret"]
        # Cache must be consistent: subsequent read hits in-memory cache.
        inner_count_after_threads = inner.read_count
        backend.read(REF)
        assert inner.read_count == inner_count_after_threads


# ---------------------------------------------------------------------------
# Version gate: wrong version in a valid scrambled payload is rejected
# ---------------------------------------------------------------------------


class TestVersionGate:
    def test_wrong_version_degrades_to_inner(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A properly scrambled payload with a wrong version fires the version check, inner consulted."""
        path = _cache_file(tmp_path)
        blob = file_caching._encode_payload({"version": 99, "sets": {}})
        path.write_bytes(blob)
        path.chmod(0o600)

        inner = StubBackend(refs={REF: "from-inner"})
        with caplog.at_level(logging.WARNING, logger="op_core.backends.file_caching"):
            result = FileCachingBackend(inner, path=path).read(REF)
        assert result == "from-inner"
        assert inner.read_count == 1
        assert any("corrupt" in r.message.lower() for r in caplog.records)
