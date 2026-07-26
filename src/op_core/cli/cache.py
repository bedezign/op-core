"""The ``op-cache`` console command: inspect and manage the op-core cache file.

Four subcommands, by "temperature" (design sections 7-8):

* ``op-cache clear`` — cold, no auth. Delete the whole cache file (every set).
* ``op-cache info`` — cold, no auth. Print metadata only: file path/size/mtime
  and, per set, the bucket id, value/miss/**recoverable** counts, stored TTL,
  and entry ages. A ``recoverable`` entry is tombstoned but still within its
  set's grace window — no secret survives, only the reference key and a
  timestamp, re-resolvable by ``refresh`` or ``warm``. It **never** prints
  secret values or ``op://`` reference strings — those are exactly the
  casual-reading exposure the on-disk scrambling exists to prevent.
* ``op-cache refresh --bucket ID`` — warm, **auth required**. Re-resolve one
  named set's *live and recoverable* (tombstoned-but-in-grace) entries through
  a source backend and re-store them, with the set's own stored TTL and grace
  (no override) — it restamps under the existing stored ``(ttl, grace)`` and
  owns neither of its own. A reference past its grace window is gone; refresh
  cannot bring it back.
* ``op-cache warm --bucket ID --ttl N [--grace N]`` — warm, **auth required**.
  Rebuild one named set's live-and-recoverable references under a **new**,
  caller-owned TTL (and, since ``warm`` is the verb that owns TTL, a caller-owned
  grace too — uncapped, unlike TTL, since a tombstone retains no secret) — it
  owns both, unlike ``refresh``. Because a writer constructed with a different
  ``(ttl, grace)`` than its stored set discards and rebuilds that set (the
  writer-owned-TTL invariant: a reader can never stretch an entry past its
  writer's intention, and nothing mutates a TTL in place), ``warm`` is a thin
  CLI surface over that existing discard-and-rebuild path — no engine change.
  The real difference from ``refresh`` is not which entries each can reach —
  both handle live and tombstoned-but-in-grace entries identically, and
  neither can recover anything past grace — it is that ``refresh`` restamps
  under the set's existing stored TTL and grace, while ``warm`` replaces them
  with new caller-owned ones, which is exactly why constructing its writer
  triggers the discard-and-rebuild path.

Both ``refresh`` and ``warm`` are **interactive by design**: they authenticate
to 1Password, which with desktop auth means an approval prompt (possibly
biometric). Do not bury either in non-interactive automation that cannot
satisfy the prompt — a stalled prompt looks like a hang.

Pure standard library, so it ships in the base install (``op-env`` keeps needing
the ``[cli]`` extra; this command does not).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from op_core.backends import file_caching
from op_core.backends.caching import _NOT_FOUND
from op_core.backends.detect import detect_backend
from op_core.backends.file_caching import (
    _STATE_DEAD,
    _STATE_TOMBSTONE,
    FileWriterLayer,
    _default_cache_path,
    _entry_state,
    _inspect_sets,
    clear_cache_file,
)
from op_core.exceptions import OpError, OpNotFoundError

if TYPE_CHECKING:
    import os
    from collections.abc import Sequence
    from typing import Any

    from op_core.backends.base import Backend


log = logging.getLogger(__name__)

# Bumped whenever the ``info --json`` payload shape changes, so a consumer can
# detect a shape change (e.g. a future per-bucket ``state`` field) rather than
# infer one from missing keys. 1: the initial shape, including the per-bucket
# ``recoverable`` count alongside ``values``/``misses``.
_INFO_JSON_SCHEMA = 1

# Caps how long a single `op-cache warm` approval can keep a set's credentials
# live without a fresh human approval. The writer-owned-TTL invariant exists to
# bound how long a set stays live without re-authentication; a TTL with no
# upper bound would let one biometric approval grant unlimited runway, which
# defeats that purpose. 3 hours (design section 8 follow-up).
_WARM_MAX_TTL_SECONDS = 3 * 60 * 60


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="op-cache", description="Inspect and manage the op-core persistent cache.")
    sub = parser.add_subparsers(dest="command", required=True)

    clear_p = sub.add_parser("clear", help="delete the cache file (every set, every bucket)")
    _add_path_option(clear_p)

    info_p = sub.add_parser("info", help="show cache metadata only (no secret values, no references)")
    info_p.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of prose")
    _add_path_option(info_p)

    refresh_p = sub.add_parser(
        "refresh",
        help=(
            "re-resolve one named set's live and recoverable (tombstoned-but-in-grace) entries under "
            "its existing stored ttl -- no override (see 'warm' to rebuild under a new ttl) "
            "(interactive: may prompt for 1Password auth)"
        ),
    )
    refresh_p.add_argument(
        "--bucket",
        required=True,
        metavar="ID",
        help="the set to refresh (list ids with 'op-cache info')",
    )
    _add_path_option(refresh_p)

    warm_p = sub.add_parser(
        "warm",
        help=(
            "rebuild one named set's stored references under a NEW, caller-owned ttl -- unlike "
            "'refresh', which restamps under the existing ttl only (interactive: may prompt for 1Password auth)"
        ),
    )
    warm_p.add_argument(
        "--bucket",
        required=True,
        metavar="ID",
        help="the set to warm (list ids with 'op-cache info')",
    )
    warm_p.add_argument(
        "--ttl",
        required=True,
        type=_ttl_type,
        metavar="SECONDS",
        help=f"new ttl this set will own, in seconds (capped at {_WARM_MAX_TTL_SECONDS}s / 3 hours)",
    )
    warm_p.add_argument(
        "--grace",
        type=_grace_type,
        default=None,
        metavar="SECONDS",
        help=(
            "tombstone window this set will own, in seconds after --ttl expires "
            "(default: half the ttl); 0 disables it. Unlike --ttl, uncapped -- a "
            "tombstone retains no secret, only the reference key and a timestamp, so a "
            "long grace costs no extra exposure."
        ),
    )
    _add_path_option(warm_p)
    return parser


def _add_path_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", metavar="PATH", help="cache file path (default: the standard location)")


def _ttl_type(value: str) -> float:
    """``argparse`` ``type=`` for ``--ttl``: a positive number of seconds, capped at 3 hours.

    Raising :class:`argparse.ArgumentTypeError` here makes an invalid ``--ttl`` a usage error
    (exit code 2), consistent with how argparse already reports other bad arguments.
    """
    try:
        ttl = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--ttl must be a number of seconds, got {value!r}") from exc
    if ttl <= 0:
        raise argparse.ArgumentTypeError(f"--ttl must be positive, got {ttl!r}")
    if ttl > _WARM_MAX_TTL_SECONDS:
        raise argparse.ArgumentTypeError(
            f"--ttl of {ttl:.0f}s exceeds the {_WARM_MAX_TTL_SECONDS}s (3 hour) cap for op-cache warm"
        )
    return ttl


def _grace_type(value: str) -> float:
    """``argparse`` ``type=`` for ``--grace``: a non-negative number of seconds, uncapped.

    Unlike ``--ttl``, ``grace=0`` is legal (it disables the tombstone window entirely),
    and there is no upper cap -- a tombstone retains no secret, only the reference key
    and a timestamp, so a long grace costs no extra exposure.
    """
    try:
        grace = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--grace must be a number of seconds, got {value!r}") from exc
    if grace < 0:
        raise argparse.ArgumentTypeError(f"--grace must be non-negative, got {grace!r}")
    return grace


def main(argv: Sequence[str] | None = None) -> int:
    return run(sys.argv[1:] if argv is None else argv)


def run(argv: Sequence[str], *, backend: Backend | None = None) -> int:
    """Parse ``argv`` and run the requested subcommand. ``backend`` is a test seam."""
    ns = build_parser().parse_args(list(argv))
    path = Path(ns.path) if ns.path else _default_cache_path()
    try:
        if ns.command == "clear":
            return _do_clear(path)
        if ns.command == "info":
            _do_info(path, as_json=ns.json)
            return 0
        if ns.command == "warm":
            return _do_warm(path, ns.bucket, ns.ttl, ns.grace, backend=backend)
        return _do_refresh(path, ns.bucket, backend=backend)
    except OpError as exc:
        log.error("op-cache: %s", exc)
        return 2


def _do_clear(path: Path) -> int:
    existed = path.exists()
    clear_cache_file(path)
    print(f"cleared the cache file: {path}" if existed else f"no cache file to clear: {path}")
    return 0


def _do_info(path: Path, *, as_json: bool = False) -> None:
    """Print cache metadata. Unlike the other ``_do_*`` handlers, this one has no
    failure path of its own -- an unreadable or missing cache file is not an error,
    it degrades to "no cache" output -- so it reports nothing back to ``run()``,
    which supplies the ``0`` exit code itself rather than have this function lie
    about a status it never varies.
    """
    sets = _inspect_sets(path)
    stat = _stat_or_none(path) if sets is not None else None
    if sets is None or stat is None:
        _print_empty_info(path, as_json=as_json)
        return
    now = time.time()
    if as_json:
        print(json.dumps(_build_info_payload(path, stat, sets, now)))
    else:
        _print_info_text(path, stat, sets, now)


def _stat_or_none(path: Path) -> os.stat_result | None:
    """``path.stat()``, or ``None`` on failure.

    TOCTOU: a concurrent `op-cache clear` can remove the file between
    `_inspect_sets` returning non-None and this stat -- treated as no cache.
    """
    try:
        return path.stat()
    except OSError:
        return None


def _print_empty_info(path: Path, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"schema": _INFO_JSON_SCHEMA, "path": str(path), "size": 0, "modified": 0.0, "sets": {}}))
    else:
        print(f"no readable cache at {path}")


def _print_info_text(path: Path, stat: os.stat_result, sets: dict[str, Any], now: float) -> None:
    print(f"cache file: {path}")
    print(f"size: {stat.st_size} bytes")
    print(f"modified: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))}")
    print(f"sets: {len(sets)}")
    for bucket, record in sets.items():
        print(_format_set(bucket, record, now))


class _SetCounts(NamedTuple):
    """Per-set entry counts, keyed by name rather than position at the two call sites."""

    values: int
    misses: int
    recoverable: int


def _classify_counts(entries: dict[str, Any], ttl: float, grace: float, now: float) -> _SetCounts:
    """Return per-set ``(values, misses, recoverable)`` counts.

    State is recomputed at display time via ``_entry_state`` rather than trusted from a
    raw ``tombstone`` flag -- ``_inspect_sets`` performs no purge, so an entry may still
    carry its raw ``value``/``miss`` key well past ``ttl`` (or even past ``ttl + grace``);
    a stale flag would misreport a dead entry as recoverable, telling a consuming process
    it has runway it does not have. Dead entries are excluded outright.
    """
    values = misses = recoverable = 0
    for entry in entries.values():
        state = _entry_state(now - entry["cached_at"], ttl, grace)
        if state == _STATE_DEAD:
            continue
        if state == _STATE_TOMBSTONE:
            recoverable += 1
            continue
        if entry.get("miss"):
            misses += 1
        else:
            values += 1
    return _SetCounts(values, misses, recoverable)


def _format_set(bucket: str, record: dict[str, Any], now: float) -> str:
    """Render one set's metadata. Prints counts/TTL/ages only — never keys or values."""
    entries = record["entries"]
    ttl = record["ttl"]
    grace = record["grace"]
    values, misses, recoverable = _classify_counts(entries, ttl, grace, now)
    line = f"  bucket {bucket}: {values} value(s), {misses} miss(es), {recoverable} recoverable, ttl {ttl:.0f}s"
    if entries:
        ages = [now - entry["cached_at"] for entry in entries.values()]
        oldest, newest = max(ages), min(ages)
        next_expiry = ttl - oldest
        when = f"next expiry in {next_expiry:.0f}s" if next_expiry >= 0 else f"{-next_expiry:.0f}s overdue"
        line += f", oldest {oldest:.0f}s, newest {newest:.0f}s, {when}"
    return line


def _build_info_payload(
    path: Path, stat: os.stat_result, sets: dict[str, dict[str, Any]], now: float
) -> dict[str, Any]:
    """Build the ``info --json`` payload. Counts/TTL/ages only — never keys or values."""
    return {
        "schema": _INFO_JSON_SCHEMA,
        "path": str(path),
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "sets": {
            bucket: _build_set_payload(record, now)
            for bucket, record in sets.items()
            if record["entries"]  # an empty bucket has no runway to report; omit rather than misreport it
        },
    }


def _build_set_payload(record: dict[str, Any], now: float) -> dict[str, Any]:
    """Build one bucket's JSON metadata. Mirrors ``_format_set`` but structured.

    Callers must not invoke this for a bucket with zero entries -- there is no
    meaningful age or runway to report, and inventing one (e.g. defaulting to
    a full TTL of "runway remaining") would misreport in the dangerous
    direction for a consumer deciding whether it has enough credentialed
    runway to schedule work. See ``_build_info_payload``, which filters empty
    buckets out before calling this.
    """
    entries = record["entries"]
    ttl = record["ttl"]
    grace = record["grace"]
    values, misses, recoverable = _classify_counts(entries, ttl, grace, now)
    ages = [now - entry["cached_at"] for entry in entries.values()]
    oldest_age, newest_age = max(ages), min(ages)
    seconds_until_expiry = ttl - oldest_age
    return {
        "values": values,
        "misses": misses,
        "recoverable": recoverable,
        "ttl": ttl,
        "oldest_age": oldest_age,
        "newest_age": newest_age,
        "seconds_until_expiry": seconds_until_expiry,
        "overdue": seconds_until_expiry < 0,
    }


def _refreshable_references(record: dict[str, Any], now: float) -> list[str]:
    """References worth refreshing: live or tombstoned-but-in-grace. Dead ones are gone."""
    ttl = record["ttl"]
    grace = record["grace"]
    return [
        key
        for key, entry in record["entries"].items()
        if _entry_state(now - entry["cached_at"], ttl, grace) != _STATE_DEAD
    ]


def _do_refresh(path: Path, bucket: str, *, backend: Backend | None) -> int:
    raw = _inspect_sets(path)
    if raw is None or bucket not in raw:
        print(f"op-cache: no set named {bucket!r} in the cache", file=sys.stderr)
        return 1
    record = raw[bucket]
    # Kept as a module-object access (`file_caching._wallclock()`), not a direct
    # import: tests reach it via `monkeypatch.setattr(file_caching, "_wallclock",
    # ...)`, which only intercepts callers that look it up through the module each
    # time -- a `from ... import _wallclock` binds the original function at import
    # time and would not see the patch.
    now = file_caching._wallclock()
    # The union of live and recoverable (tombstoned-but-in-grace) references -- a
    # tombstoned entry has no value or referrable content, but its reference key
    # survives, so it can be re-resolved through the source and come back live.
    refreshable = _refreshable_references(record, now)
    if not refreshable:
        print(f"set {bucket!r} has no live or recoverable entries to refresh (past its grace window)")
        return 0
    source = backend if backend is not None else detect_backend()
    # The set is rebuilt under its own stored (ttl, grace) — refresh acts as a writer
    # but owns no new policy of its own (design section 7). Passing the stored grace
    # (not just ttl) is required: a writer constructed with a mismatched (ttl, grace)
    # triggers the engine's discard-and-rebuild and would wipe the bucket before this
    # loop even ran. Re-resolving each key restamps a value and re-checks a stored miss,
    # and resurrects a tombstoned reference with a fresh cached_at under the same ttl.
    writer = FileWriterLayer(ttl=record["ttl"], grace=record["grace"], bucket=bucket, path=path)
    for reference in refreshable:
        try:
            writer.store(reference, source.read(reference))
        except OpNotFoundError:
            writer.store(reference, _NOT_FOUND)
    count = len(refreshable)
    print(f"refreshed {count} entr{'y' if count == 1 else 'ies'} in set {bucket!r}")
    return 0


def _do_warm(path: Path, bucket: str, ttl: float, grace: float | None, *, backend: Backend | None) -> int:
    # Defense in depth: `_ttl_type` already enforces this cap for the CLI entry
    # point, but the cap is a domain invariant (how long a set may stay live
    # without a fresh human approval), not a parsing concern -- a future caller
    # reaching `_do_warm` by another path must not be able to exceed it either.
    if ttl > _WARM_MAX_TTL_SECONDS:
        raise ValueError(f"--ttl of {ttl:.0f}s exceeds the {_WARM_MAX_TTL_SECONDS}s (3 hour) cap for op-cache warm")
    raw = _inspect_sets(path)
    if raw is None or bucket not in raw:
        print(f"op-cache: no set named {bucket!r} in the cache to warm", file=sys.stderr)
        return 1
    record = raw[bucket]
    now = file_caching._wallclock()
    # Re-resolve live and in-grace references only -- a reference past its grace
    # window is gone, explicitly, rather than an accident of whether a writer
    # happened to touch (and purge) the file recently.
    references = _refreshable_references(record, now)
    source = backend if backend is not None else detect_backend()

    # Resolve every reference BEFORE touching disk. Constructing the writer below
    # with a (ttl, grace) different from the stored one discards and rebuilds the
    # set (see file_caching._FileCache._load) -- if that construction happened
    # before all reads succeeded, a mid-loop failure (auth, timeout, offline) would
    # leave the bucket's prior entries and tombstones destroyed with no way to
    # re-resolve the references not yet read. So nothing destructive happens until
    # every read below has succeeded.
    resolved: dict[str, object] = {}
    for reference in references:
        try:
            resolved[reference] = source.read(reference)
        except OpNotFoundError:
            resolved[reference] = _NOT_FOUND

    # This is what distinguishes warm from refresh: warm owns a new ttl (and,
    # since it is the verb that owns ttl, a new grace too). A writer constructed
    # with a (ttl, grace) different from the stored one discards and rebuilds the
    # set -- existing engine behavior; warm is a thin CLI surface over it, not an
    # engine change. Only reached once every reference above has resolved.
    writer = FileWriterLayer(ttl=ttl, grace=grace, bucket=bucket, path=path)
    for reference, value in resolved.items():
        writer.store(reference, value)
    count = len(resolved)
    print(f"warmed {count} entr{'y' if count == 1 else 'ies'} in set {bucket!r} with a new {ttl:.0f}s ttl")
    return 0
