"""Tests for :mod:`op_core.backends.file_caching`."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
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
    return tmp_path / "cache.json"


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

    def test_cache_file_is_valid_json(self, tmp_path: Path) -> None:
        path = _cache_file(tmp_path)
        FileCachingBackend(StubBackend(refs={REF: "secret"}), path=path).read(REF)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert REF in data["entries"]


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
        path = blocker / "cache.json"
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

    def test_wrong_version_ignored(self, tmp_path: Path) -> None:
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
        def boom(_path: Path, _text: str) -> None:
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
        """A secret with non-ASCII characters round-trips through the JSON cache file."""
        path = _cache_file(tmp_path)
        unicode_value = "café-£-é漢字"
        FileCachingBackend(StubBackend(refs={REF: unicode_value}), path=path).read(REF)

        second = StubBackend()
        result = FileCachingBackend(second, path=path).read(REF)
        assert result == unicode_value
        assert second.read_count == 0  # served entirely from the file
