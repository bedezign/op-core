"""Persistent caching decorator backends.

:class:`FileCachingBackend` and :class:`AsyncFileCachingBackend` mirror
:class:`~op_core.backends.caching.CachingBackend`, but persist resolved
``read()`` results to a file so cache hits survive *across separate process
invocations*. The motivating case is a short-lived CLI launched repeatedly: the
in-process :class:`CachingBackend` does nothing for it (its store dies with the
process), so every run re-shells to ``op`` and, with desktop auth, re-triggers
the biometric prompt. A persisted reference->value map lets repeated runs
authenticate at most once per TTL window.

Key differences from the in-process backend:

* **Wall-clock TTL.** :class:`CachingBackend` stamps entries with
  :func:`time.monotonic`, which resets every process start and is meaningless
  across runs. This backend stamps with :func:`time.time` and expires against
  it.
* **Only ``read()`` is persisted.** ``get_item`` / ``list_items`` /
  ``list_vaults`` pass straight through — serializing item graphs is out of
  scope and not what the cache exists for.
* **The file holds resolved secret values in plaintext.** It is created
  ``0600`` inside a ``0700`` directory, defaults to a RAM-backed location, is
  written atomically, and is ignored on load if its ownership or permissions
  look tampered with. A corrupt or unreadable cache never crashes the caller —
  it degrades to the wrapped backend with a non-secret warning.

``ttl<=0`` disables persistence entirely (the backend becomes an effective
pass-through).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from op_core.backends.caching import _NOT_FOUND, CacheEntry, _Store
from op_core.exceptions import OpNotFoundError, OpOfflineError
from op_core.items import Item, ItemRef, ItemSummary, VaultSummary

if TYPE_CHECKING:
    from collections.abc import Sequence

    from op_core.backends.base import AsyncBackend, Backend

log = logging.getLogger(__name__)

_CACHE_VERSION = 1
_DEFAULT_FILENAME = "cache.json"


def _wallclock() -> float:
    """Indirection over :func:`time.time` so tests can pin the clock."""
    return time.time()


def default_cache_dir() -> Path:
    """Return the directory persistent caches live in, creating it ``0700``.

    Prefers ``$XDG_RUNTIME_DIR/op-core`` (a per-user, RAM-backed, ``0700``
    location that is cleared on logout). Falls back to
    ``$TMPDIR/op-core-<uid>`` (``/tmp`` when ``TMPDIR`` is unset).

    Raises :class:`OSError` if the directory cannot be created or secured. The
    CLI catches this and runs without a persistent cache rather than failing.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        target = Path(runtime) / "op-core"
    else:
        tmp = os.environ.get("TMPDIR") or "/tmp"  # 0700 per-uid subdir, secured below
        target = Path(tmp) / f"op-core-{_uid()}"
    _secure_dir(target)
    return target


def _uid() -> int:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else 0


def _secure_dir(directory: Path) -> None:
    """Create ``directory`` ``0700`` and verify it is a real, owned, private dir.

    Raises :class:`OSError` (or :class:`ValueError`) on anything suspicious: a
    symlink in place of the directory, ownership by another user, or perms that
    cannot be tightened. Guards the classic shared-``/tmp`` pre-creation attack.
    """
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(directory)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"cache directory is not a real directory: {directory}")
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid != getuid():
        raise ValueError(f"cache directory not owned by current user: {directory}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(directory, 0o700)


class _FileCache:
    """Shared persistence + expiry logic for the sync and async backends.

    Holds an LRU :class:`_Store` of wall-clock-stamped entries and mirrors it to
    a JSON file. Knows nothing about sync vs async — the backends own the lock
    and the inner-backend calls.
    """

    def __init__(self, *, ttl: float, max_entries: int, path: str | Path | None) -> None:
        self._ttl = ttl
        self._store = _Store(max_entries)
        self._path = self._resolve_path(path)
        if self._path is not None:
            self._load()

    # -- public, lock-free helpers (callers hold the lock) ------------------

    def lookup(self, key: str) -> CacheEntry | None:
        """Return a live entry for ``key`` or ``None`` (absent or expired)."""
        entry = self._store.get(key)
        if entry is None:
            return None
        if (_wallclock() - entry.cached_at) > self._ttl:
            self._store.delete(key)
            return None
        return entry

    def store(self, key: str, value: Any) -> None:
        """Store ``value`` (or the ``_NOT_FOUND`` sentinel) and persist."""
        self._store.put(key, CacheEntry(key=key, value=value, cached_at=_wallclock(), metadata={}))
        self._persist()

    @property
    def persistent(self) -> bool:
        return self._path is not None

    # -- path resolution ----------------------------------------------------

    def _resolve_path(self, path: str | Path | None) -> Path | None:
        if self._ttl <= 0:
            return None
        try:
            if path is not None:
                target = Path(path)
                _secure_dir(target.parent)
                return target
            return default_cache_dir() / _DEFAULT_FILENAME
        except (OSError, ValueError) as exc:
            log.warning("persistent cache disabled (cannot secure cache location): %s", exc)
            return None

    # -- (de)serialization --------------------------------------------------

    def _load(self) -> None:
        assert self._path is not None
        try:
            info = os.lstat(self._path)
        except OSError:
            return  # no cache yet — normal first run
        if not self._is_trustworthy(info):
            log.warning("ignoring cache file with unexpected ownership/permissions: %s", self._path)
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._ingest(data)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("ignoring unreadable/corrupt cache file %s: %s", self._path, exc)
            self._store.clear()

    def _is_trustworthy(self, info: os.stat_result) -> bool:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return False
        getuid = getattr(os, "getuid", None)
        if getuid is not None and info.st_uid != getuid():
            return False
        return not stat.S_IMODE(info.st_mode) & 0o077

    def _ingest(self, data: object) -> None:
        if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
            raise ValueError("unsupported cache format")
        entries = data["entries"]
        if not isinstance(entries, dict):
            raise TypeError("entries must be an object")
        for key, record in entries.items():
            cached_at = float(record["cached_at"])
            value: Any = _NOT_FOUND if record.get("miss") else record["value"]
            if value is not _NOT_FOUND and not isinstance(value, str):
                raise TypeError("cached value must be a string")
            self._store.put(key, CacheEntry(key=key, value=value, cached_at=cached_at, metadata={}))

    def _persist(self) -> None:
        if self._path is None:
            return
        payload = {"version": _CACHE_VERSION, "entries": self._dump_entries()}
        try:
            _atomic_write(self._path, json.dumps(payload))
        except OSError as exc:
            log.warning("could not write cache file %s: %s", self._path, exc)

    def _dump_entries(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key, entry in self._store.items():
            if entry.value is _NOT_FOUND:
                out[key] = {"miss": True, "cached_at": entry.cached_at}
            else:
                out[key] = {"value": entry.value, "cached_at": entry.cached_at}
        return out


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically with ``0600`` permissions."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cache-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _unpack_read(entry: CacheEntry, reference: str, default_value: str | None) -> str:
    if entry.value is _NOT_FOUND:
        if default_value is not None:
            return default_value
        raise OpNotFoundError(reference)
    return entry.value


class FileCachingBackend:
    """Sync persistent caching decorator. Wraps any :class:`Backend`."""

    def __init__(
        self,
        inner: Backend,
        *,
        ttl: float = 300.0,
        max_entries: int = 1024,
        path: str | Path | None = None,
    ) -> None:
        self._inner = inner
        self._cache = _FileCache(ttl=ttl, max_entries=max_entries, path=path)
        self._lock = threading.Lock()

    def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
        with self._lock:
            entry = self._cache.lookup(reference)
        if entry is not None:
            return _unpack_read(entry, reference, default_value)

        if not online:
            raise OpOfflineError(f"reference not cached: {reference}")

        try:
            value: Any = self._inner.read(reference)
        except OpNotFoundError:
            with self._lock:
                self._cache.store(reference, _NOT_FOUND)
            if default_value is not None:
                return default_value
            raise OpNotFoundError(reference) from None
        with self._lock:
            self._cache.store(reference, value)
        return value

    def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        return self._inner.list_items(vault=vault, tags=tags, categories=categories)

    def list_vaults(self) -> list[VaultSummary]:
        return self._inner.list_vaults()

    def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        return self._inner.get_item(item, vault=vault)


class AsyncFileCachingBackend:
    """Async persistent caching decorator. Wraps any :class:`AsyncBackend`."""

    def __init__(
        self,
        inner: AsyncBackend,
        *,
        ttl: float = 300.0,
        max_entries: int = 1024,
        path: str | Path | None = None,
    ) -> None:
        self._inner = inner
        self._cache = _FileCache(ttl=ttl, max_entries=max_entries, path=path)
        # asyncio.Lock — consistent with AsyncCachingBackend. The disk write
        # inside the lock is intentional: the file is RAM-backed and the caller
        # is a short-lived CLI, so the synchronous write is acceptable here.
        self._lock = asyncio.Lock()

    async def read(self, reference: str, *, default_value: str | None = None, online: bool = True) -> str:
        async with self._lock:
            entry = self._cache.lookup(reference)
        if entry is not None:
            return _unpack_read(entry, reference, default_value)

        if not online:
            raise OpOfflineError(f"reference not cached: {reference}")

        try:
            value: Any = await self._inner.read(reference)
        except OpNotFoundError:
            async with self._lock:
                self._cache.store(reference, _NOT_FOUND)
            if default_value is not None:
                return default_value
            raise OpNotFoundError(reference) from None
        async with self._lock:
            self._cache.store(reference, value)
        return value

    async def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        return await self._inner.list_items(vault=vault, tags=tags, categories=categories)

    async def list_vaults(self) -> list[VaultSummary]:
        return await self._inner.list_vaults()

    async def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        return await self._inner.get_item(item, vault=vault)
