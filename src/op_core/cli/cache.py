"""The ``op-cache`` console command: inspect and manage the op-core cache file.

Three subcommands, by "temperature" (design section 7):

* ``op-cache clear`` — cold, no auth. Delete the whole cache file (every set).
* ``op-cache info`` — cold, no auth. Print metadata only: file path/size/mtime
  and, per set, the bucket id, value/miss counts, stored TTL, and entry ages.
  It **never** prints secret values or ``op://`` reference strings — those are
  exactly the casual-reading exposure the on-disk scrambling exists to prevent.
* ``op-cache refresh --bucket ID`` — warm, **auth required**. Re-resolve one
  named set's live entries through a source backend and re-store them, with the
  set's own stored TTL (no override).

``refresh`` is **interactive by design**: it authenticates to 1Password, which
with desktop auth means an approval prompt (possibly biometric). Do not bury it
in non-interactive automation that cannot satisfy the prompt — a stalled prompt
looks like a hang. It extends a *live* set before expiry; it cannot resurrect a
set after expiry (the file does not retain expired keys).

Pure standard library, so it ships in the base install (``op-env`` keeps needing
the ``[cli]`` extra; this command does not).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from op_core.backends.caching import _NOT_FOUND
from op_core.backends.detect import detect_backend
from op_core.backends.file_caching import (
    FileWriterLayer,
    _default_cache_path,
    _inspect_sets,
    _load_reader_state,
    clear_cache_file,
)
from op_core.exceptions import OpError, OpNotFoundError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    from op_core.backends.base import Backend


log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="op-cache", description="Inspect and manage the op-core persistent cache.")
    sub = parser.add_subparsers(dest="command", required=True)

    clear_p = sub.add_parser("clear", help="delete the cache file (every set, every bucket)")
    _add_path_option(clear_p)

    info_p = sub.add_parser("info", help="show cache metadata only (no secret values, no references)")
    _add_path_option(info_p)

    refresh_p = sub.add_parser(
        "refresh",
        help="re-resolve one named set's live entries (interactive: may prompt for 1Password auth)",
    )
    refresh_p.add_argument(
        "--bucket",
        required=True,
        metavar="ID",
        help="the set to refresh (list ids with 'op-cache info')",
    )
    _add_path_option(refresh_p)
    return parser


def _add_path_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", metavar="PATH", help="cache file path (default: the standard location)")


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
            return _do_info(path)
        return _do_refresh(path, ns.bucket, backend=backend)
    except OpError as exc:
        log.error("op-cache: %s", exc)
        return 2


def _do_clear(path: Path) -> int:
    existed = path.exists()
    clear_cache_file(path)
    print(f"cleared the cache file: {path}" if existed else f"no cache file to clear: {path}")
    return 0


def _do_info(path: Path) -> int:
    sets = _inspect_sets(path)
    if sets is not None:
        # TOCTOU: a concurrent `op-cache clear` can remove the file between
        # _inspect_sets returning non-None and path.stat(). Treat as no cache.
        try:
            stat = path.stat()
        except OSError:
            sets = None
        else:
            now = time.time()
            print(f"cache file: {path}")
            print(f"size: {stat.st_size} bytes")
            print(f"modified: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))}")
            print(f"sets: {len(sets)}")
            for bucket, record in sets.items():
                print(_format_set(bucket, record, now))
    if sets is None:
        print(f"no readable cache at {path}")
    return 0


def _format_set(bucket: str, record: dict[str, Any], now: float) -> str:
    """Render one set's metadata. Prints counts/TTL/ages only — never keys or values."""
    entries = record["entries"]
    ttl = record["ttl"]
    values = sum(1 for entry in entries.values() if not entry.get("miss"))
    misses = len(entries) - values
    line = f"  bucket {bucket}: {values} value(s), {misses} miss(es), ttl {ttl:.0f}s"
    if entries:
        ages = [now - entry["cached_at"] for entry in entries.values()]
        oldest, newest = max(ages), min(ages)
        next_expiry = ttl - oldest
        when = f"next expiry in {next_expiry:.0f}s" if next_expiry >= 0 else f"{-next_expiry:.0f}s overdue"
        line += f", oldest {oldest:.0f}s, newest {newest:.0f}s, {when}"
    return line


def _do_refresh(path: Path, bucket: str, *, backend: Backend | None) -> int:
    stored_ttl, live = _load_reader_state(path, bucket)
    if not live:
        # No live entries: distinguish "no such set" from "expired" for a clear message.
        raw = _inspect_sets(path)
        if raw is None or bucket not in raw:
            print(f"op-cache: no set named {bucket!r} in the cache", file=sys.stderr)
            return 1
        print(f"set {bucket!r} has no live entries to refresh (expired sets cannot be resurrected)")
        return 0
    source = backend if backend is not None else detect_backend()
    # The set is rebuilt under its own stored TTL — refresh acts as a writer but
    # owns no new TTL (design section 7). Re-resolving each live key restamps a
    # value and re-checks a stored miss.
    writer = FileWriterLayer(ttl=stored_ttl, bucket=bucket, path=path)
    for reference in live:
        try:
            writer.store(reference, source.read(reference))
        except OpNotFoundError:
            writer.store(reference, _NOT_FOUND)
    count = len(live)
    print(f"refreshed {count} entr{'y' if count == 1 else 'ies'} in set {bucket!r}")
    return 0
