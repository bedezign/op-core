# Contributing to op-core

Thanks for considering a contribution. This document covers the development setup, test layout, and conventions the project follows.

## Development setup

`op-core` uses [`uv`](https://docs.astral.sh/uv/) for environment management.

```bash
git clone git@github.com:bedezign/op-core.git
cd op-core
uv sync                       # install runtime + dev dependencies
uv sync --extra sdk           # add the optional SDK backend
```

Python 3.11+ is required. The repository pins a development Python via `.python-version`; `uv` will pick it up automatically.

## Running tests

```bash
uv run pytest                 # full suite (all unit tests)
uv run pytest -k <pattern>    # subset by name
uv run pytest --cov           # with coverage
```

The test suite has no external dependencies — no real 1Password account, no `op` CLI, no network. Backends that touch external systems are exercised through `subprocess.run` / `asyncio.create_subprocess_exec` mocks. `InMemoryBackend` is itself a real backend used as a test seam.

Integration-marked tests (`-m integration`) are reserved for tests that require `OP_SERVICE_ACCOUNT_TOKEN` and network access. None ship by default.

## Type checking

```bash
uv run --with pyright pyright src/op_core/
```

Strict pyright is the contract — every public surface ships type hints, and the `py.typed` marker ships with the package.

## Code style

- `ruff format` for formatting, `ruff check --fix` for lints. Both run on save in most editors and as a pre-commit step.
- 120-char line length.
- `from __future__ import annotations` at the top of every module.
- Modern Python: `str | None` over `Optional[str]`, `list[int]` over `List[int]`, structural pattern matching where it improves clarity.
- No `print()` — use `logging` if you need debug output.

## Project layout

```
src/op_core/
├── __init__.py        # flat public API re-exports
├── auth.py            # ServiceAccountAuth, DesktopAuth, detect_auth
├── client.py          # OnePassword and AsyncOnePassword facades
├── exceptions.py      # OpError hierarchy
├── field.py           # FieldValue, classify_type, resolve_chain
├── items.py           # Item, ItemField, ItemSection, ItemSummary
├── opref.py           # op:// URI grammar
├── strings.py         # expand_braces and other string helpers
└── backends/
    ├── base.py        # Backend / AsyncBackend protocols
    ├── cli.py         # subprocess-driven `op` binary backend
    ├── sdk.py         # official onepassword-sdk wrapper
    ├── memory.py      # in-process backend with fall-through
    ├── caching.py     # TTL/LRU decorator
    └── detect.py      # detect_backend / detect_async_backend

tests/
├── conftest.py
└── unit/              # tests live at the repository root, NOT under src/
```

All public names are re-exported from `op_core/__init__.py`. Adding a new public symbol means adding it to both the import block and `__all__`.

## Pull requests

- Keep changes focused — one logical change per PR.
- Tests for new functionality and bug fixes. The existing suite is the primary documentation of intended behavior; new code should match its testing rhythm.
- Update [`CHANGELOG.md`](CHANGELOG.md) under `[Unreleased]` — `Added`, `Fixed`, `Changed`, or `Removed`.
- Commit messages: imperative subject under 50 characters, focus on *why* not *what*. Body for context.

## Reporting issues

Open a GitHub issue with:

- What you tried (a minimal reproducer is gold).
- What you expected.
- What actually happened (full traceback if there is one).
- `op-core` version, Python version, OS, and which backend you were using.

Security issues should not be filed as public issues — see [`SECURITY.md`](SECURITY.md) (or email the maintainer listed in `pyproject.toml`).

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
