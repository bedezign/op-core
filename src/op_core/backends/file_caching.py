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

On-disk model. All processes share **one cache file**
holding multiple **sets**, keyed by a caller-chosen ``bucket`` id (the CLI uses
a hash of the resolved reference set). A set is the unit of caching intent: it
is stamped with the TTL its writer was constructed with, and every entry in it
expires against that stored TTL. The TTL is therefore writer-owned — a reader
can never stretch an entry's life beyond the writer's intention. A backend that
opens its own set and finds a *different* stored TTL discards the set and
rebuilds it ("a different TTL means the cache is reconstructed"); there is no
override path. The same credential cached under two sets is two independent
entries with independent TTLs — that duplication is deliberate TTL isolation.

Hygiene properties of the single file:

* **Purge-on-load.** Every load walks *all* sets and drops entries expired by
  their own set's TTL (and sets left empty), rewriting the file if anything was
  dropped. Any invocation anywhere scrubs everyone's stale plaintext.
* **Locked merge-on-persist.** Writes re-read the file under an exclusive
  ``flock``, replace the writer's own set (merging entries newest-wins when the
  TTL matches), purge the others, and write atomically — so concurrent
  processes can neither clobber each other's sets nor resurrect purged entries.

The file content is **scrambled, not encrypted**: the serialized payload is
zlib-compressed and XOR-ed with a SHA-256 keystream derived from machine-local
material (machine-id + uid) and a per-write random nonce. The threat model is
casual or offline reading — ``cat``/``grep``, secret scanners, backups, or the
file copied off the machine (where the key material is absent). It does *not*
protect against a same-user process that runs this code; nothing without an
external secret store can. Defense against other users is unchanged: the file
is created ``0600`` inside a ``0700`` directory, defaults to a RAM-backed
location, and is ignored on load if its ownership or permissions look tampered
with. A corrupt or unreadable cache never crashes the caller — it degrades to
the wrapped backend with a non-secret warning (and, when the file is ours, is
rewritten, scrubbing whatever stale content it held).

``ttl<=0`` disables persistence entirely (the backend becomes an effective
pass-through).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import functools
import hashlib
import json
import logging
import os
import stat
import tempfile
import threading
import time
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from op_core.backends.caching import _NOT_FOUND, CacheEntry, _Store
from op_core.exceptions import OpNotFoundError, OpOfflineError
from op_core.items import Item, ItemRef, ItemSummary, VaultSummary

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from op_core.backends.base import AsyncBackend, Backend

log = logging.getLogger(__name__)

_CACHE_VERSION = 1
_DEFAULT_FILENAME = "cache.bin"
_DEFAULT_BUCKET = "default"
_MAGIC = b"OPC1"
_NONCE_LEN = 16
_KEY_CONTEXT = b"op-core-cache"
_MACHINE_ID_PATHS = ("/etc/machine-id", "/var/lib/dbus/machine-id")

# A set as serialized: {"ttl": float, "entries": {key: {"value"|"miss", "cached_at"}}}
_Sets = dict[str, dict[str, Any]]


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


# -- payload scrambling (obfuscation, not encryption — see module docstring) --


def _machine_material() -> bytes:
    """Best-effort stable per-machine bytes for the scrambling key."""
    for candidate in _MACHINE_ID_PATHS:
        try:
            data = Path(candidate).read_bytes().strip()
        except OSError:
            continue
        if data:
            return data
    return b""  # key degrades to uid-only material


@functools.lru_cache(maxsize=1)
def _derive_key() -> bytes:
    material = b"\x00".join((_KEY_CONTEXT, _machine_material(), str(_uid()).encode("ascii")))
    return hashlib.sha256(material).digest()


def _keystream_xor(data: bytes, key: bytes, nonce: bytes) -> bytes:
    """XOR ``data`` with a SHA-256 counter keystream. Symmetric."""
    stream = bytearray()
    counter = 0
    while len(stream) < len(data):
        stream += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(data, stream[: len(data)], strict=True))


def _encode_payload(payload: dict[str, Any]) -> bytes:
    nonce = os.urandom(_NONCE_LEN)
    raw = zlib.compress(json.dumps(payload).encode("utf-8"))
    return _MAGIC + nonce + _keystream_xor(raw, _derive_key(), nonce)


def _decode_payload(blob: bytes) -> dict[str, Any]:
    if not blob.startswith(_MAGIC) or len(blob) < len(_MAGIC) + _NONCE_LEN:
        raise ValueError("not an op-core cache file")
    nonce = blob[len(_MAGIC) : len(_MAGIC) + _NONCE_LEN]
    body = blob[len(_MAGIC) + _NONCE_LEN :]
    try:
        raw = zlib.decompress(_keystream_xor(body, _derive_key(), nonce))
    except zlib.error as exc:
        raise ValueError(f"cannot unscramble cache payload: {exc}") from exc
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise TypeError("cache payload must be an object")
    return data


class _FileCache:
    """Shared persistence + expiry logic for the sync and async backends.

    Holds an LRU :class:`_Store` of wall-clock-stamped entries mirroring this
    backend's own set, and merges it into the shared multi-set file. Knows
    nothing about sync vs async — the backends own the lock and the
    inner-backend calls.
    """

    def __init__(
        self,
        *,
        ttl: float,
        max_entries: int,
        path: str | Path | None,
        bucket: str = _DEFAULT_BUCKET,
    ) -> None:
        self._ttl = ttl
        self._bucket = bucket
        self._max_entries = max_entries
        self._store = _Store(max_entries)
        self._path = self._resolve_path(path)
        if self._path is not None:
            self._load()

    # -- public, lock-free helpers (callers hold the backend lock) ----------

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

    # -- file access (always under the inter-process lock) -------------------

    def _require_path(self) -> Path:
        """Return ``self._path``, raising ``RuntimeError`` if persistence is disabled."""
        if self._path is None:
            raise RuntimeError("persistence disabled — no cache path")
        return self._path

    @contextlib.contextmanager
    def _locked(self) -> Generator[None]:
        """Hold an exclusive inter-process lock on the cache file's sidecar."""
        path = self._require_path()
        fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # also releases the flock

    def _load(self) -> None:
        self._require_path()
        try:
            with self._locked():
                sets, dirty = self._read_sets()
                own = sets.get(self._bucket)
                if own is not None and own["ttl"] != self._ttl:
                    # Different TTL: the set is reconstructed, never reinterpreted.
                    del sets[self._bucket]
                    own = None
                    dirty = True
                if own is not None:
                    for key, record in own["entries"].items():
                        value: Any = _NOT_FOUND if record.get("miss") else record["value"]
                        self._store.put(
                            key, CacheEntry(key=key, value=value, cached_at=record["cached_at"], metadata={})
                        )
                if dirty:
                    self._write_sets(sets)
        except OSError as exc:
            log.warning("could not read cache file %s: %s", self._path, exc)

    def _persist(self) -> None:
        if self._path is None:
            return
        try:
            with self._locked():
                sets, _ = self._read_sets()
                sets[self._bucket] = {"ttl": self._ttl, "entries": self._merged_own_entries(sets)}
                self._write_sets(sets)
        except OSError as exc:
            log.warning("could not write cache file %s: %s", self._path, exc)

    def _merged_own_entries(self, sets: _Sets) -> dict[str, dict[str, Any]]:
        """Merge this store's entries with the own set on disk, newest-wins.

        The disk copy may hold fresh entries written by a concurrent process
        after our load; clobbering them would only cost a re-auth, but merging
        is cheap. A disk set stamped with a different TTL is discarded — the
        set is being reconstructed under our TTL.
        """
        disk_own = sets.get(self._bucket)
        merged: dict[str, dict[str, Any]] = {}
        if disk_own is not None and disk_own["ttl"] == self._ttl:
            merged.update(disk_own["entries"])
        for key, entry in self._store.items():
            record = self._dump_entry(entry)
            current = merged.get(key)
            if current is None or current["cached_at"] <= record["cached_at"]:
                merged[key] = record
        if len(merged) > self._max_entries:
            by_age = sorted(merged.items(), key=lambda kv: kv[1]["cached_at"])
            merged = dict(by_age[-self._max_entries :])
        return merged

    # -- (de)serialization --------------------------------------------------

    def _read_sets(self) -> tuple[_Sets, bool]:
        """Read, validate, and purge the cache file. Returns ``(sets, dirty)``.

        ``dirty`` is True when the on-disk content should be rewritten: either
        the purge dropped expired entries, or the file is ours but unreadable
        (corrupt or foreign-keyed) — rewriting then scrubs whatever stale
        content it held.
        """
        path = self._require_path()
        try:
            info = os.lstat(path)
        except OSError:
            return {}, False  # no cache yet — normal first run
        if not self._is_trustworthy(info):
            log.warning("ignoring cache file with unexpected ownership/permissions: %s", path)
            return {}, False
        try:
            sets = self._validated_sets(_decode_payload(path.read_bytes()))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("discarding unreadable/corrupt cache file %s: %s", path, exc)
            return {}, True
        return sets, self._purge(sets)

    @staticmethod
    def _is_trustworthy(info: os.stat_result) -> bool:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return False
        getuid = getattr(os, "getuid", None)
        if getuid is not None and info.st_uid != getuid():
            return False
        return not stat.S_IMODE(info.st_mode) & 0o077

    @staticmethod
    def _validated_sets(data: dict[str, Any]) -> _Sets:
        if data.get("version") != _CACHE_VERSION:
            raise ValueError("unsupported cache format")
        sets = data["sets"]
        if not isinstance(sets, dict):
            raise TypeError("sets must be an object")
        for record in sets.values():
            ttl = record["ttl"]
            if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
                raise ValueError("set ttl must be a positive number")
            entries = record["entries"]
            if not isinstance(entries, dict):
                raise TypeError("set entries must be an object")
            for entry in entries.values():
                entry["cached_at"] = float(entry["cached_at"])
                if not entry.get("miss") and not isinstance(entry["value"], str):
                    raise TypeError("cached value must be a string")
        return sets

    @staticmethod
    def _purge(sets: _Sets) -> bool:
        """Drop entries expired by their own set's TTL; drop emptied sets."""
        now = _wallclock()
        dirty = False
        dead: list[str] = []
        for bucket, record in sets.items():
            entries = record["entries"]
            live = {k: e for k, e in entries.items() if (now - e["cached_at"]) <= record["ttl"]}
            if len(live) != len(entries):
                record["entries"] = live
                dirty = True
            if not live:
                dead.append(bucket)
        for bucket in dead:
            del sets[bucket]
            dirty = True
        return dirty

    @staticmethod
    def _dump_entry(entry: CacheEntry) -> dict[str, Any]:
        if entry.value is _NOT_FOUND:
            return {"miss": True, "cached_at": entry.cached_at}
        return {"value": entry.value, "cached_at": entry.cached_at}

    def _write_sets(self, sets: _Sets) -> None:
        path = self._require_path()
        _atomic_write(path, _encode_payload({"version": _CACHE_VERSION, "sets": sets}))


def _atomic_write(path: Path, blob: bytes) -> None:
    """Write ``blob`` to ``path`` atomically with ``0600`` permissions."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cache-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
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
        bucket: str = _DEFAULT_BUCKET,
    ) -> None:
        self._inner = inner
        self._cache = _FileCache(ttl=ttl, max_entries=max_entries, path=path, bucket=bucket)
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
        bucket: str = _DEFAULT_BUCKET,
    ) -> None:
        self._inner = inner
        self._cache = _FileCache(ttl=ttl, max_entries=max_entries, path=path, bucket=bucket)
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
