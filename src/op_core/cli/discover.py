"""Upward ``.env`` discovery for ``op-env --ascend``.

Given the explicit ``--env-file`` arguments (if any), the extra
``--env-file-name`` patterns, and the ``--ascend-until`` boundaries, this
module produces the ordered list of ``.env`` files to load — highest precedence
first — by walking *up* the directory tree from one or more anchor directories.

Design (settled with the user):

* **Anchors.** One per ``--env-file`` (its containing directory), de-duplicated
  by real path; if no ``--env-file`` is given, the single anchor is the current
  working directory.
* **Names.** The set of basenames of the ``--env-file`` arguments plus any
  explicit ``--env-file-name`` values. If that set is empty (a bare
  ``--ascend`` from cwd), it defaults to ``.env``.
* **Boundaries.** ``--ascend-until`` values stop the climb at the first matching
  ancestor (inclusive). A value with no ``/`` matches an ancestor by directory
  *name*; a value with a ``/`` is resolved to an absolute path and matched
  exactly. With no ``--ascend-until``, the default boundary is ``$HOME`` when
  the anchor is under it, otherwise the climb does not leave the anchor.
* **Security ceiling (always on).** The climb stops before entering a directory
  that is world-writable, not owned by the current user, or on a different
  filesystem (a mount-point crossing). Individual discovered files that are
  symlinks, world-writable, or not owned by the current user are skipped. This
  matters because the result feeds ``os.execvpe`` — an attacker who can plant a
  ``.env`` in an untrusted ancestor must not be able to inject environment into
  the child.

Explicit ``--env-file`` arguments are always loaded (the user named them on
purpose) and rank above any discovered file.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

Boundary = Callable[[Path], bool]


@dataclass(frozen=True)
class _DirMeta:
    dev: int
    uid: int
    mode: int


def _dir_meta(path: Path) -> _DirMeta:
    """Stat a directory. A monkeypatchable seam so the security rails are testable."""
    info = os.lstat(path)
    return _DirMeta(dev=info.st_dev, uid=info.st_uid, mode=info.st_mode)


def _current_uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def discover_env_files(
    *,
    env_files: list[str],
    names: list[str],
    ascend: bool,
    ascend_until: list[str],
    cwd: Path,
    home: Path | None,
) -> list[Path]:
    """Return the ordered list of ``.env`` files to load (highest precedence first)."""
    explicit = [Path(f) for f in env_files]
    result: list[Path] = list(explicit)  # explicit files win over discovered ones

    if ascend:
        name_set = _name_set(explicit, names)
        boundaries = _make_boundaries(ascend_until, cwd)
        uid = _current_uid()
        for anchor in _anchors(explicit, cwd):
            for directory in _climb(anchor, boundaries, home, uid):
                result.extend(_collect_dir(directory, name_set))

    return _dedup(result)


def _resolve(path: Path, cwd: Path) -> Path:
    return (path if path.is_absolute() else cwd / path).resolve()


def _anchors(explicit: list[Path], cwd: Path) -> list[Path]:
    if not explicit:
        return [cwd.resolve()]
    anchors: dict[str, Path] = {}
    for file in explicit:
        directory = _resolve(file, cwd).parent
        anchors.setdefault(str(directory), directory)
    return list(anchors.values())


def _name_set(explicit: list[Path], names: list[str]) -> list[str]:
    combined = {file.name for file in explicit} | set(names)
    return sorted(combined) if combined else [".env"]


def _make_boundaries(values: list[str], cwd: Path) -> list[Boundary] | None:
    if not values:
        return None
    matchers: list[Boundary] = []
    for value in values:
        if "/" in value:
            target = _resolve(Path(value), cwd)
            matchers.append(lambda directory, t=target: directory == t)
        else:
            matchers.append(lambda directory, name=value: directory.name == name)
    return matchers


def _ceiling_stop(meta: _DirMeta, prev_dev: int | None, uid: int | None) -> bool:
    """Return True when the security ceiling requires stopping *before* yielding this directory.

    Checks (in order): filesystem-boundary crossing, symlinked directory component,
    world-writable or unowned directory.  All three are security-load-bearing — see
    module docstring.
    """
    if prev_dev is not None and meta.dev != prev_dev:
        return True  # crossed a filesystem boundary
    if stat.S_ISLNK(meta.mode):
        return True  # symlinked directory component — stop the climb as defense-in-depth
    return not _dir_trusted(meta, uid)  # world-writable or someone else's directory


def _climb(
    anchor: Path,
    boundaries: list[Boundary] | None,
    home: Path | None,
    uid: int | None,
) -> Iterator[Path]:
    """Yield directories from ``anchor`` upward, honouring boundaries and the ceiling."""
    current = anchor
    prev_dev: int | None = None
    matched = False
    while True:
        try:
            meta = _dir_meta(current)
        except OSError:
            break
        if _ceiling_stop(meta, prev_dev, uid):
            break
        yield current

        if _at_boundary(current, boundaries, home):
            matched = matched or boundaries is not None
            break

        parent = current.parent
        if parent == current:
            break  # filesystem root
        prev_dev = meta.dev
        current = parent

    if boundaries is not None and not matched:
        log.warning("ascend boundary not found above %s; stopped at root or trust boundary", anchor)


def _at_boundary(current: Path, boundaries: list[Boundary] | None, home: Path | None) -> bool:
    if boundaries is None:
        # Default mode: stop at $HOME, and never leave the anchor if it is not under $HOME.
        if home is None:
            return True
        return current == home or not current.is_relative_to(home)
    return any(match(current) for match in boundaries)


def _dir_trusted(meta: _DirMeta, uid: int | None) -> bool:
    if meta.mode & stat.S_IWOTH:
        return False
    return not (uid is not None and meta.uid != uid)


def _collect_dir(directory: Path, names: list[str]) -> list[Path]:
    return [directory / name for name in names if _file_trusted(directory / name)]


def _file_trusted(path: Path) -> bool:
    try:
        if path.is_symlink():
            log.warning("skipping symlinked env file: %s", path)
            return False
        if not path.is_file():
            return False
        info = path.stat()
        if info.st_mode & stat.S_IWOTH:
            log.warning("skipping world-writable env file: %s", path)
            return False
        uid = _current_uid()
        if uid is not None and info.st_uid != uid:
            log.warning("skipping env file not owned by current user: %s", path)
            return False
        return True
    except OSError:
        return False


def _dedup(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out
