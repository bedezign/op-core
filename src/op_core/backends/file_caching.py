"""File-backed cache engine and layers.

This module provides the persistent half of the resolver stack: a scrambled,
single-file, multi-set cache shared by all processes on the machine, plus the
two layer types that front it.

* :class:`FileReaderLayer` -- a read-only observer of one named set. It loads a
  consistent snapshot once at construction (lock-free; atomic writes make that
  safe), serves entries while they are live by the set's *stored* TTL, and never
  touches the filesystem afterwards: no entries added, no misses recorded, no
  purge rewrite, no lock sidecar, no corrupt-file scrub. It degrades to "no
  entries" when the file is missing, corrupt, untrusted, or holds no such set.
* :class:`FileWriterLayer` -- a read-write layer over one named set. ``ttl`` is
  required (persisting a secret to disk is an explicit choice). It performs the
  purge-on-load and locked merge-on-persist of the underlying engine.
* :func:`clear_cache_file` -- delete the whole cache file (every set).

Layers carry no locks; the resolver owns locking (see
:mod:`op_core.backends.stack`).

On-disk model. All processes share **one cache file** holding multiple
**sets**, keyed by a caller-chosen ``bucket`` id (``op-env`` uses a hash of the
resolved reference set). A set is the unit of caching intent: it is stamped with
the TTL its writer was constructed with, and every entry expires against that
stored TTL. The TTL is writer-owned -- a reader can never stretch an entry past
its writer's intention. A writer that opens its own set and finds a *different*
stored TTL discards and rebuilds it; there is no override path.

Hygiene (writer-side -- readers never write):

* **Purge-on-load.** Every writer load walks *all* sets and drops entries
  expired by their own set's TTL (and empties), rewriting the file if anything
  was dropped -- any writer invocation scrubs everyone's stale plaintext.
* **Locked merge-on-persist.** Writes re-read the file under an exclusive
  ``flock``, replace the writer's own set (merging newest-wins when the TTL
  matches), purge the others, and write atomically -- so concurrent processes
  neither clobber each other's sets nor resurrect purged entries.

The file content is **scrambled, not encrypted**: the serialized payload is
zlib-compressed and XOR-ed with a SHA-256 keystream derived from machine-local
material (machine-id + uid) and a per-write random nonce. The threat model is
casual or offline reading -- ``cat``/``grep``, secret scanners, backups, or the
file copied off the machine (where the key material is absent). It does *not*
protect against a same-user process. Defense against other users: the file is
``0600`` inside a ``0700`` directory, defaults to a RAM-backed location, and is
ignored on load if its ownership or permissions look tampered with. A corrupt or
unreadable cache never crashes the caller -- it degrades to empty (and, for a
writer whose file is ours, is rewritten, scrubbing whatever stale content it
held).

``ttl <= 0`` disables persistence (a :class:`FileWriterLayer` becomes inert).
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import json
import logging
import os
import stat
import tempfile
import time
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from op_core.backends.caching import _NOT_FOUND, CacheEntry, _Store

if TYPE_CHECKING:
    from collections.abc import Generator


log = logging.getLogger(__name__)

_CACHE_VERSION = 1
_DEFAULT_FILENAME = "cache.bin"
_DEFAULT_BUCKET = "default"
_MAGIC = b"OPC1"
_NONCE_LEN = 16
_KEY_CONTEXT = b"op-core-cache"
_MACHINE_ID_PATHS = ("/etc/machine-id", "/var/lib/dbus/machine-id")
_MSG_WRITE_FAILED = "could not write cache file %s: %s"
_MSG_UNTRUSTED_FILE = "ignoring cache file with unexpected ownership/permissions: %s"

# A set as serialized: {"ttl": float, "entries": {key: {"value"|"miss", "cached_at"}}}
_Sets = dict[str, dict[str, Any]]


def _wallclock() -> float:
    """Indirection over :func:`time.time` so tests can pin the clock."""
    return time.time()


def _default_cache_path() -> Path:
    """Compute the default cache file location without touching the filesystem.

    Prefers ``$XDG_RUNTIME_DIR/op-core`` (a per-user, RAM-backed, ``0700``
    location that is cleared on logout). Falls back to
    ``$TMPDIR/op-core-<uid>`` (``/tmp`` when ``TMPDIR`` is unset).
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "op-core" / _DEFAULT_FILENAME
    tmp = os.environ.get("TMPDIR") or "/tmp"  # 0700 per-uid subdir, secured by callers
    return Path(tmp) / f"op-core-{_uid()}" / _DEFAULT_FILENAME


def default_cache_dir() -> Path:
    """Return the directory persistent caches live in, creating it ``0700``.

    See :func:`_default_cache_path` for the location policy.

    Raises :class:`OSError` if the directory cannot be created or secured. The
    CLI catches this and runs without a persistent cache rather than failing.
    """
    target = _default_cache_path().parent
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
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool):
            raise TypeError("ttl must be a number of seconds; for read-only access use FileReaderLayer")
        self._ttl = ttl
        self._bucket = bucket
        self._max_entries = max_entries
        self._store = _Store(max_entries)
        self._path = self._resolve_path(path)
        if self._path is not None:
            self._load()

    # -- public, lock-free helpers (callers hold the backend lock) ----------

    def lookup(self, key: str) -> CacheEntry | None:
        """Return a live entry for ``key`` or ``None`` (absent or expired).

        Uses a two-sided bound ``0 <= age <= ttl`` so that future-dated entries
        (negative age, i.e. clock skew) are treated as expired rather than
        immortal (FIX-2).
        """
        entry = self._store.get(key)
        if entry is None:
            return None
        age = _wallclock() - entry.cached_at
        if not 0 <= age <= self._ttl:
            self._store.delete(key)
            return None
        return entry

    def store(self, key: str, value: Any) -> None:
        """Store ``value`` (or the ``_NOT_FOUND`` sentinel) and persist."""
        self._store.put(key, CacheEntry(key=key, value=value, cached_at=_wallclock(), metadata={}))
        self._persist()

    def clear(self) -> None:
        """Drop every entry: wipe the in-memory store and delete the own set on disk.

        Both halves are required — persist merges with the on-disk set
        newest-wins, so a memory-only clear would resurrect on the next store.
        """
        self._store.clear()
        if self._path is None:
            return
        try:
            with self._locked():
                sets, _ = self._read_sets()
                sets.pop(self._bucket, None)
                self._write_sets(sets)
        except OSError as exc:
            log.warning(_MSG_WRITE_FAILED, self._path, exc)

    def clear_misses(self) -> None:
        """Forget negative-cache records, in memory and in the own set on disk."""
        self._store.clear_misses()
        if self._path is None:
            return
        try:
            with self._locked():
                sets, _ = self._read_sets()
                own = sets.get(self._bucket)
                if own is not None:
                    own["entries"] = {k: e for k, e in own["entries"].items() if not e.get("miss")}
                    if not own["entries"]:
                        del sets[self._bucket]
                self._write_sets(sets)
        except OSError as exc:
            log.warning(_MSG_WRITE_FAILED, self._path, exc)

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
            log.warning(_MSG_WRITE_FAILED, self._path, exc)

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
            log.warning(_MSG_UNTRUSTED_FILE, path)
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
            live = {k: e for k, e in entries.items() if 0 <= (now - e["cached_at"]) <= record["ttl"]}
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


def _load_reader_state(path: Path, bucket: str) -> tuple[float, dict[str, CacheEntry]]:
    """Lock-free, write-free snapshot of one set: ``(stored_ttl, live entries)``.

    Returns an empty state when the file is missing, untrustworthy, corrupt, or
    holds no such set — the reader then degrades to a pass-through. Entries
    already expired by the set's stored TTL are not loaded; entries stamped in
    the future (clock skew) are treated as expired, not immortal.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return 0.0, {}  # no cache yet — normal
    if not _FileCache._is_trustworthy(info):
        log.warning(_MSG_UNTRUSTED_FILE, path)
        return 0.0, {}
    try:
        sets = _FileCache._validated_sets(_decode_payload(path.read_bytes()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("ignoring unreadable/corrupt cache file %s: %s", path, exc)
        return 0.0, {}
    own = sets.get(bucket)
    if own is None:
        return 0.0, {}
    ttl: float = own["ttl"]
    now = _wallclock()
    entries: dict[str, CacheEntry] = {}
    for key, record in own["entries"].items():
        if 0 <= (now - record["cached_at"]) <= ttl:
            value: Any = _NOT_FOUND if record.get("miss") else record["value"]
            entries[key] = CacheEntry(key=key, value=value, cached_at=record["cached_at"], metadata={})
    return ttl, entries


def _live_reader_entry(entries: dict[str, CacheEntry], key: str, ttl: float) -> CacheEntry | None:
    """Return a still-live entry from a reader snapshot, or None if absent or expired.

    Does not mutate ``entries`` -- FileReaderLayer is a pure observer of an
    immutable snapshot. Expired entries are simply not served; they age out
    logically without being deleted from the dict.
    """
    entry = entries.get(key)
    if entry is None:
        return None
    if not 0 <= (_wallclock() - entry.cached_at) <= ttl:
        return None
    return entry


def _inspect_sets(path: Path) -> _Sets | None:
    """Read-only decode of *every* set for inspection — no purge, no write, no live filter.

    Returns the raw validated sets exactly as they sit on disk (expired entries
    included), or ``None`` when the file is missing, untrustworthy, or
    unreadable. This is the cold-path companion to :func:`_load_reader_state`
    (which loads one bucket's *live* entries); it backs ``op-cache info``, which
    reports counts and ages without mutating the file.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not _FileCache._is_trustworthy(info):
        log.warning(_MSG_UNTRUSTED_FILE, path)
        return None
    try:
        return _FileCache._validated_sets(_decode_payload(path.read_bytes()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("ignoring unreadable/corrupt cache file %s: %s", path, exc)
        return None


class FileReaderLayer:
    """Read-only cache layer: one-time snapshot of one named bucket.

    A pure observer of the cache file. Loads its bucket once at construction
    (lock-free — atomic writes guarantee a consistent read), serves entries while
    they are live by the set's *stored* TTL, and never touches the filesystem
    afterwards: no entries added, no misses recorded, no purge rewrite, no lock
    sidecar, no corrupt-file scrub.

    ``path=None`` uses the standard location (``$XDG_RUNTIME_DIR/op-core/`` or
    ``$TMPDIR/op-core-<uid>/``). No directory is created — if the path does not
    exist the layer degrades to "no entries" and the resolver falls through.

    Satisfies :class:`~op_core.backends.stack.CacheLayer` only (not writable).
    The resolver owns locking; this layer carries none. An instance must not be
    shared between stacks.
    """

    def __init__(self, bucket: str = _DEFAULT_BUCKET, path: str | Path | None = None) -> None:
        resolved = Path(path) if path is not None else _default_cache_path()
        self._set_ttl, self._entries = _load_reader_state(resolved, bucket)

    def lookup(self, reference: str) -> CacheEntry | None:
        """Return a still-live entry for ``reference``, or ``None``."""
        return _live_reader_entry(self._entries, reference, self._set_ttl)


class FileWriterLayer:
    """Read-write cache layer backed by one named bucket in the shared cache file.

    Wraps :class:`_FileCache` and exposes the
    :class:`~op_core.backends.stack.WritableCacheLayer` interface. The
    resolver owns locking; this layer carries none.

    ``ttl`` is **required** with no default — persisting secrets to disk is an
    explicit caller decision (design section 6). ``ttl <= 0`` disables
    persistence: the layer behaves as an in-memory-only store that writes no
    file and survives only for the lifetime of this object.

    Construction performs purge-on-load: any writer invocation scrubs everyone's
    stale entries from the shared file (existing ``_FileCache`` behavior,
    unchanged).
    """

    def __init__(
        self,
        ttl: float,
        bucket: str = _DEFAULT_BUCKET,
        path: str | Path | None = None,
        max_entries: int = 1024,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._cache = _FileCache(ttl=ttl, max_entries=max_entries, path=path, bucket=bucket)

    def lookup(self, reference: str) -> CacheEntry | None:
        """Return a live entry for ``reference``, or ``None``."""
        return self._cache.lookup(reference)

    def store(self, reference: str, value: object) -> None:
        """Store ``value`` (a string or the miss sentinel) and persist to disk."""
        self._cache.store(reference, value)

    def clear(self) -> None:
        """Drop every entry in memory and delete the own set on disk.

        Both halves are cleared so a later ``store()`` cannot resurrect cleared
        entries via the merge-on-persist cycle.
        """
        self._cache.clear()

    def clear_misses(self) -> None:
        """Drop negative-cache records in memory and from the own disk set."""
        self._cache.clear_misses()


def clear_cache_file(path: str | Path | None = None) -> None:
    """Delete the cache file — every set, every bucket.

    ``path=None`` targets the standard location (``$XDG_RUNTIME_DIR/op-core/``
    or ``$TMPDIR/op-core-<uid>/``). The operation is a no-op when the file or
    its directory does not exist.

    **Locking and FIX-1.** The deletion is taken under the exclusive flock on
    the ``.lock`` sidecar so a concurrent writer's read-merge-write cycle is not
    torn. Only the cache file is unlinked; the sidecar is deliberately **left in
    place**. Unlinking the sidecar while still holding its flock would allow a
    concurrent writer to open a fresh inode for the same path and acquire what
    it believes is the same lock — bypassing mutual exclusion entirely. The
    sidecar is a zero-byte file; the locking path recreates it anyway, so
    leaving it costs nothing.
    """
    target = Path(path) if path is not None else _default_cache_path()
    lock_path = target.with_name(target.name + ".lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return  # directory does not exist — nothing to clear
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        # FIX-1: leave the sidecar in place; unlinking it under its own flock
        # would let a concurrent writer open a fresh inode and skip the lock.
    finally:
        os.close(fd)
