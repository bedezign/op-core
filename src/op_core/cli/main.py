"""The ``op-env`` console command.

Two subcommands share one resolution core (compose env -> resolve ``op://`` ->
do something with it):

* ``op-env exec [options] -- <command> [args...]`` — replace the current
  process (``os.execvpe``) with ``command`` running under the resolved
  environment. A true exec (no lingering parent) matters for stdio JSON-RPC
  pipes. **Never prints resolved secret values.**
* ``op-env export [options] [--format env|json]`` — print the resolved
  environment (only the keys the ``.env`` files introduced) for
  ``set -a; eval "$(...)"; set +a`` or an HTTP-headers helper. **Prints secret
  values by design** — only for ``eval``/headers consumption, never an
  interactive terminal or a log.

Repeated runs resolving the same secrets share a per-invocation
:class:`~op_core.backends.file_caching.FileCachingBackend` cache file, so they
authenticate to 1Password at most once per TTL window.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from op_core.backends.detect import detect_backend
from op_core.backends.file_caching import FileCachingBackend, default_cache_dir
from op_core.cli.compose import (
    cache_bucket,
    check_required,
    compose_env,
    is_op_reference,
    resolve_env,
)
from op_core.cli.discover import discover_env_files
from op_core.cli.emit import format_env, format_json
from op_core.cli.envfile import expand_variables, load_env_file
from op_core.cli.errors import OpEnvError
from op_core.client import OnePassword
from op_core.exceptions import OpError

if TYPE_CHECKING:
    from op_core.backends.base import Backend

log = logging.getLogger(__name__)

ExecFn = Callable[[str, Sequence[str], Mapping[str, str]], object]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="op-env",
        description="Resolve op:// references in an environment, then exec a command or print it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    exec_p = sub.add_parser("exec", help="resolve the environment and exec a command")
    _add_common_options(exec_p)

    export_p = sub.add_parser("export", help="resolve the environment and print it")
    _add_common_options(export_p)
    export_p.add_argument(
        "--format",
        choices=["env", "json"],
        default="env",
        dest="fmt",
        help="output format (default: env)",
    )

    return parser


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env-file",
        action="append",
        dest="env_files",
        metavar="PATH",
        help="load a .env file (repeatable; later files layer over earlier ones)",
    )
    parser.add_argument(
        "--inherit-env",
        action="store_true",
        help="take along the inherited environment as a base and interpolation source "
        "(default: use .env file content only)",
    )
    parser.add_argument(
        "--keep",
        action="append",
        dest="keep",
        metavar="KEY",
        help="with --inherit-env, keep only these inherited variables (allowlist; repeatable)",
    )
    parser.add_argument(
        "--drop",
        action="append",
        dest="drop",
        metavar="KEY",
        help="with --inherit-env, drop these inherited variables (denylist; repeatable)",
    )
    parser.add_argument(
        "--override",
        action="store_true",
        help="let later .env files override earlier ones (default: first/nearest wins)",
    )
    parser.add_argument(
        "--ascend",
        action="store_true",
        help="also collect .env files by walking up parent directories",
    )
    parser.add_argument(
        "--ascend-until",
        action="append",
        dest="ascend_until",
        metavar="PATH_OR_NAME",
        help="stop the upward walk at this directory (path, or a bare name matched against "
        "ancestor directory names); repeatable; default: $HOME",
    )
    parser.add_argument(
        "--env-file-name",
        action="append",
        dest="env_file_names",
        metavar="NAME",
        help="additional filename to look for while ascending (repeatable; default: .env, "
        "plus the basename of each --env-file)",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=300,
        metavar="SECONDS",
        help="cache resolved values for this long across runs (default: 300; 0 disables)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="do not read or write the persistent cache",
    )
    parser.add_argument(
        "--require",
        action="append",
        dest="require",
        metavar="KEY",
        help="fail unless KEY resolves to a non-empty value (repeatable)",
    )


def main(argv: Sequence[str] | None = None) -> int:
    return run(sys.argv[1:] if argv is None else argv)


def run(
    argv: Sequence[str],
    *,
    backend: Backend | None = None,
    environ: Mapping[str, str] | None = None,
    exec_fn: ExecFn | None = None,
) -> int:
    """Parse ``argv`` and run the requested subcommand. Returns a process exit code.

    ``backend`` / ``environ`` / ``exec_fn`` are injection seams for tests; in
    normal use they default to the auto-detected backend, ``os.environ``, and
    :func:`os.execvpe`.
    """
    left, child = _split_double_dash(list(argv))
    namespace = build_parser().parse_args(left)
    try:
        return _dispatch(namespace, child, backend=backend, environ=environ, exec_fn=exec_fn)
    except (OpEnvError, OpError) as exc:
        print(f"op-env: {exc}", file=sys.stderr)
        return 2


def _filter_inherited(env: dict[str, str], *, keep: list[str], drop: list[str]) -> dict[str, str]:
    """Apply the allowlist then the denylist to the inherited environment.

    ``keep`` restricts to the named variables (when given); ``drop`` then removes
    the named variables. Applied before the result is used as either the base or
    the interpolation source.
    """
    if keep:
        allow = set(keep)
        env = {key: value for key, value in env.items() if key in allow}
    if drop:
        deny = set(drop)
        env = {key: value for key, value in env.items() if key not in deny}
    return env


def _safe_home() -> Path | None:
    """Return the user's home as the default ascent ceiling, or ``None`` if unknown."""
    try:
        return Path.home().resolve()
    except (RuntimeError, OSError):
        return None


def _split_double_dash(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split ``argv`` at the first ``--`` into (op-env args, child argv)."""
    if "--" in argv:
        index = argv.index("--")
        return argv[:index], argv[index + 1 :]
    return argv, []


def _validate_flags(ns: argparse.Namespace, child: list[str]) -> None:
    """Raise :exc:`OpEnvError` for flag combinations that are invalid."""
    if ns.command == "export" and child:
        raise OpEnvError("export does not take a command; remove everything after '--'")
    if not ns.ascend and (ns.ascend_until or ns.env_file_names):
        raise OpEnvError("--ascend-until and --env-file-name require --ascend")
    if not ns.inherit_env and (ns.keep or ns.drop):
        raise OpEnvError("--keep and --drop require --inherit-env")


def _build_composed_env(
    ns: argparse.Namespace,
    environ: Mapping[str, str] | None,
) -> tuple[dict[str, str], set[str], dict[str, str]]:
    """Return ``(parent, introduced, composed)`` after loading and layering all ``.env`` files.

    ``parent`` is the (possibly filtered) inherited environment.
    ``introduced`` is the set of keys that came from ``.env`` files.
    ``composed`` is the fully layered and ``${VAR}``-expanded result ready for ``op://`` resolution.
    """
    # Default: ignore the inherited environment entirely — .env content only.  --inherit-env
    # opts it back in as the base (and as an interpolation source).  The keep/drop filter is
    # applied up front so a dropped variable is neither inherited nor available to
    # interpolate — closing any LEAK=${DROPPED} exfiltration path.
    if ns.inherit_env:  # noqa: SIM108 — Sonar S3358 requires the expanded form here
        raw_parent = dict(os.environ if environ is None else environ)
    else:
        raw_parent = {}
    parent = _filter_inherited(raw_parent, keep=ns.keep or [], drop=ns.drop or [])

    paths = discover_env_files(
        env_files=ns.env_files or [],
        names=ns.env_file_names or [],
        ascend=ns.ascend,
        ascend_until=ns.ascend_until or [],
        cwd=Path.cwd(),
        home=_safe_home(),
    )
    file_envs = [load_env_file(path) for path in paths]
    introduced = {key for file_env in file_envs for key in file_env}
    composed = compose_env(parent, file_envs, override=ns.override)
    # Expand ${VAR} in .env-introduced values BEFORE resolving op://: references resolve
    # against the parent (when --inherit-env) and earlier values only, the inherited
    # environment passes through untouched, and resolved secrets are never interpolated.
    composed = expand_variables(composed, introduced=introduced, source=parent)
    return parent, introduced, composed


def _dispatch(
    ns: argparse.Namespace,
    child: list[str],
    *,
    backend: Backend | None,
    environ: Mapping[str, str] | None,
    exec_fn: ExecFn | None,
) -> int:
    _validate_flags(ns, child)

    _parent, introduced, composed = _build_composed_env(ns, environ)
    resolved = _resolve(composed, ttl=ns.ttl, no_cache=ns.no_cache, backend=backend)
    check_required(resolved, ns.require or [])

    if ns.command == "exec":
        return _do_exec(child, resolved, exec_fn=exec_fn)
    return _do_export(resolved, introduced, fmt=ns.fmt)


def _resolve(
    composed: Mapping[str, str],
    *,
    ttl: int,
    no_cache: bool,
    backend: Backend | None,
) -> dict[str, str]:
    # No references means no backend (and no 1Password contact) is needed at all.
    if not any(is_op_reference(value) for value in composed.values()):
        return dict(composed)
    inner = backend if backend is not None else detect_backend()
    op = OnePassword(_wrap_cache(inner, composed, ttl=ttl, no_cache=no_cache))
    return resolve_env(composed, op)


def _wrap_cache(inner: Backend, composed: Mapping[str, str], *, ttl: int, no_cache: bool) -> Backend:
    if no_cache or ttl <= 0:
        return inner
    path = _cache_path(composed)
    if path is None:
        return inner
    return FileCachingBackend(inner, ttl=ttl, path=path)


def _cache_path(composed: Mapping[str, str]) -> str | None:
    try:
        return str(default_cache_dir() / f"env-{cache_bucket(composed)}.json")
    except OSError as exc:
        log.warning("persistent cache unavailable: %s", exc)
        return None


def _do_exec(child: list[str], env: Mapping[str, str], *, exec_fn: ExecFn | None) -> int:
    if not child:
        raise OpEnvError("exec requires a command after '--'")
    runner = os.execvpe if exec_fn is None else exec_fn
    try:
        runner(child[0], child, env)
    except OSError as exc:
        raise OpEnvError(f"cannot exec {child[0]!r}: {exc}") from exc
    return 0  # only reached when exec_fn is a test stub (real execvpe never returns)


def _do_export(resolved: Mapping[str, str], introduced: set[str], *, fmt: str) -> int:
    subset = {key: resolved[key] for key in introduced if key in resolved}
    print(format_json(subset) if fmt == "json" else format_env(subset))
    return 0
