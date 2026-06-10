# op-core

**A backend-agnostic Python toolkit for 1Password.** One API for the `op` CLI, the official SDK, and an in-process backend that lets you test code without 1Password — or run offline against a pre-resolved cache.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#status)

## Why

Calling 1Password from Python is ergonomically rough today. The `op` CLI is great in shell pipelines but verbose to wrap in code. The official SDK is fast but service-account-only and has no notion of fallback or caching. Tests that need secrets either talk to a real vault or hand-roll a mock that drifts from reality.

`op-core` gives you a single `OnePassword` interface, four interchangeable backends, and sync-or-async parity, so you can:

- **Use the CLI in dev, the SDK in production** — same code, different transport.
- **Test without 1Password** — `InMemoryBackend` is a real backend, not a mock; your application code never knows the difference.
- **Cache aggressively** — `CachingBackend` decorator with TTL, LRU cap, and negative caching (missing refs are remembered too).
- **Resolve fallback chains** — `op://primary||op://backup||literal-default` walks left-to-right, returns the first hit.
- **Persist resolved secrets and run offline** — `online=False` is a hard safety rail (`OpOfflineError`, distinct from `OpNotFoundError`), not a hint.

```python
from op_core import OnePassword

op = OnePassword()  # auto-detects the backend best suited to the environment
token = op.read("op://Personal/GitHub/token")
```

## Install

`op-core` is not on PyPI yet. Install directly from GitHub.

With [`uv`](https://docs.astral.sh/uv/) (recommended):

```bash
uv add "op-core @ git+https://github.com/bedezign/op-core"
uv add "op-core[sdk] @ git+https://github.com/bedezign/op-core"   # + official 1Password SDK
uv add "op-core[cli] @ git+https://github.com/bedezign/op-core"   # + the op-env command
```

With `pip`:

```bash
pip install "op-core @ git+https://github.com/bedezign/op-core"
pip install "op-core[sdk] @ git+https://github.com/bedezign/op-core"
pip install "op-core[cli] @ git+https://github.com/bedezign/op-core"
```

Pin to a tag for reproducibility:

```bash
uv add "op-core @ git+https://github.com/bedezign/op-core@v0.4.0"
```

Python 3.11+. Zero required dependencies for the base install. The CLI backend requires the `op` binary on `PATH`; the `sdk` extra installs `onepassword-sdk` from PyPI; the `cli` extra installs `python-dotenv` and the `op-env` command.

## Quick tour

### Read a secret

```python
from op_core import OnePassword

op = OnePassword()
token = op.read("op://Personal/GitHub/token")  # str | None
```

`read` returns `None` on a confirmed miss; raises `OpAuthError`, `OpTimeoutError`, or `OpError` for transport failures.

### Resolve a fallback chain

```python
from op_core import FieldValue, OnePassword

op = OnePassword()
fv = FieldValue.from_raw(
    "op://Vault/Item/api_key||op://Vault/Backup/api_key||sk-default",
    "api_key",
)
value = op.resolve(fv)  # walks `||` segments, returns first hit
```

### Listing vaults

```python
op = OnePassword()
for vault in op.list_vaults():
    print(vault.id, vault.name)

# Per-vault scoping is dramatically faster on accounts with many vaults:
ssh_vault = next(v for v in op.list_vaults() if v.name == "SSH Hosts")
items = op.list_items(vault=ssh_vault.id)
```

### List and fetch items

```python
op = OnePassword()
servers = op.list_items(categories=["SSH_KEY"], tags=["production"])
for summary in servers:
    item = op.get_item(summary)
    # walk item.fields directly — no extra round-trip per field
    for f in item.fields:
        print(f.label, f.value)
```

### Async parity

Every sync class has an `Async*` twin with identical semantics:

```python
from op_core import AsyncOnePassword

op = AsyncOnePassword()
token = await op.read("op://Personal/GitHub/token")
```

### Tests don't need 1Password

```python
from op_core import InMemoryBackend, OnePassword

# Your application code:
def post_to_api(op: OnePassword, url: str) -> int:
    token = op.read("op://Personal/Service/token")
    return some_http_lib.post(url, headers={"Authorization": f"Bearer {token}"}).status_code

# Your test:
def test_post_to_api():
    op = OnePassword(InMemoryBackend(refs={"op://Personal/Service/token": "test-token"}))
    assert post_to_api(op, "https://example.com/api") == 200
```

## Backends

| Backend | Transport | Auth | When to use |
|---|---|---|---|
| `CLIBackend` / `AsyncCLIBackend` | Subprocess (`op` binary) | Desktop or service account | Workstations, CI runners with the CLI installed |
| `SDKBackend` / `AsyncSDKBackend` | Official `onepassword-sdk` | Service account only | Servers, containers, anywhere without the `op` binary |
| `InMemoryBackend` / `AsyncInMemoryBackend` | In-process dict + items | None | Tests, persistent local caches, generate/wrap workflows |
| `CachingBackend` / `AsyncCachingBackend` | Decorator over any backend | Inherits | In-process TTL read caching with LRU cap and negative caching |
| `FileCachingBackend` / `AsyncFileCachingBackend` | Decorator over any backend | Inherits | Persistent TTL read cache that survives across process runs; scrambled, RAM-backed, `0600` |

Backends compose. Cache live reads:

```python
from op_core import CachingBackend, CLIBackend, OnePassword

op = OnePassword(CachingBackend(CLIBackend(), ttl=300))
```

Or serve known refs from memory and fall through to live `op` for the rest:

```python
from op_core import CLIBackend, InMemoryBackend, OnePassword

op = OnePassword(InMemoryBackend(
    refs={"op://Vault/Item/host": "db.internal"},
    fallback=CLIBackend(),
))
```

## Generate once, run offline

For applications that resolve secrets up-front and run repeatedly without round-tripping to 1Password, persist `FieldValue` objects to disk and reload them into an `InMemoryBackend`:

```python
import json
from op_core import FieldValue, InMemoryBackend, OnePassword

# Generate phase — talk to 1Password, persist the resolved values:
op_live = OnePassword()
fv = FieldValue.from_raw("op://Vault/Item/secret", "secret").with_resolved(
    op_live.read("op://Vault/Item/secret"),
)
with open("cache.json", "w") as f:
    json.dump(fv.to_dict(), f)

# Run phase — no 1Password contact, hard offline rail:
with open("cache.json") as f:
    fv = FieldValue.from_dict(json.load(f))
op = OnePassword(InMemoryBackend(refs={fv.original: fv.resolved}))
value = op.read(fv.original, online=False)  # raises OpOfflineError if missing
```

`online=False` is propagated through every backend; `OpOfflineError` is distinct from `OpNotFoundError`, so the two failure modes are separable at catch sites.

See [`INTEGRATION.md`](INTEGRATION.md) for the full patterns, including item auto-indexing and async equivalents.

## Run a command with resolved secrets (`op-env`)

The `cli` extra ships the `op-env` command. It builds an environment from one or more `.env` files, resolves any `op://` references in it, and then either runs a child process under that environment or prints it. By default it does **not** inherit your shell's environment — the result is exactly your `.env` content, nothing ambient leaks in (opt back in with `--inherit-env`, below).

```bash
pip install "op-core[cli]"   # installs python-dotenv and the op-env command
```

Put `op://` **references** in your `.env` — not raw secrets — so the file is safe at rest:

```bash
# app.env
DATABASE_URL=op://Personal/App/database_url
API_TOKEN=op://Personal/App/api_token
LOG_LEVEL=info
```

**Run a tool** with the references resolved (a true `exec` — no lingering parent, which matters for stdio JSON-RPC pipes):

```bash
op-env exec --env-file app.env -- mytool --flag
```

**Emit the resolved environment** for `eval` or an HTTP-headers helper:

```bash
set -a; eval "$(op-env export --env-file app.env)"; set +a   # shell-safe KEY='value' lines
op-env export --env-file app.env --format json               # {"DATABASE_URL": "...", ...}
```

Multiple `--env-file`s layer in order — by default the **first** file to set a key wins; `--override` flips that so later files win. `--require KEY...` hard-fails if a named key is unresolved or empty.

### Inheriting the shell environment

By default the produced environment is your `.env` content only — a hermetic, fully-specified environment. Pass `--inherit-env` to take your existing environment along as the base (and as an interpolation source, see below). Loaded `.env` files always override inherited values, so this is how you *extend* one:

```bash
op-env exec --inherit-env --env-file app.env -- mytool
```

Because inheriting the whole environment can carry unrelated secrets into the child, you can narrow it — applied to the inherited set *before* it's used for anything, so a dropped variable is neither inherited nor available to interpolate:

```bash
op-env exec --inherit-env --keep PATH --keep HOME -- mytool     # allowlist: only these
op-env exec --inherit-env --drop AWS_SECRET_ACCESS_KEY -- mytool # denylist: everything but these
```

`--keep` and `--drop` are repeatable and require `--inherit-env`. If both are given, `--keep` restricts first, then `--drop` subtracts.

### Variable interpolation

Values may reference variables with `${VAR}` or `${VAR:-default}`. Interpolation is applied to the values your `.env` files introduced, once, **before** `op://` references are resolved:

- A `${VAR}` resolves against the inherited environment (only with `--inherit-env`) and any variable set **earlier in the merge order** — and *nothing else*. There is no implicit `os.environ` lookup, so without `--inherit-env`, `${PATH}` resolves to empty.
- Resolution reads a variable's prior value, so `PATH=${PATH}/extra` extends the inherited `PATH` rather than referring to itself.
- It is a **single forward pass** — a reference to a variable defined *later* in the merge order, or a cycle, resolves to an empty string. There is no recursive/fixpoint resolution.
- **Resolved `op://` secret values are never interpolated.** A secret whose value contains `${...}` or `$` is passed to the child verbatim. This is deliberate: it avoids corrupting secrets that contain `$`, and it stops a vault value from injecting environment into the child. Building a string from a resolved secret is your code's job after resolution — op-core is not a template engine.
- You *can* interpolate into a reference path (`op://${VAULT}/Item/field`), and the cache still keys on the concrete, post-interpolation reference.

**Collect `.env` files up the tree** with `--ascend` — handy for a shared parent `.env` plus a project-specific one:

```bash
op-env exec --ascend -- mytool          # walk up from the current directory, nearest .env wins
op-env exec --ascend --env-file app.env -- mytool   # walk up from app.env's directory
```

`--ascend` walks **up** from each `--env-file`'s directory (or the current directory if none is given), collecting `.env` files with nearest-directory-wins precedence. It looks for the basename of each `--env-file` plus any `--env-file-name NAME` (default `.env`). `--ascend-until PATH_OR_NAME` (repeatable) stops the walk at the first matching ancestor — a bare name matches an ancestor directory by name (`--ascend-until myproject`), anything with a `/` is an exact path; the default boundary is `$HOME`. A security ceiling is always enforced: the walk never enters a world-writable, not-owned, or different-filesystem directory, and symlinked or world-writable `.env` files are skipped — because the result feeds `exec`.

Repeated runs authenticate to 1Password **at most once per TTL window**. `op-env` wraps the auto-detected backend in a [`FileCachingBackend`](#backends) keyed on the set of `op://` references in the environment, so a tool launched over and over reuses the cached values (and, with desktop auth, skips re-triggering the biometric prompt) instead of shelling out to `op` every time. `--ttl SECONDS` (default 300) tunes the window; `--no-cache` disables it.

> **Security:** `op-env exec` never prints resolved secret values. `op-env export` prints them by design — use it only for `eval`/headers consumption, never an interactive terminal or a log. The cache file holds resolved secrets and is written `0600` in a RAM-backed `0700` directory, scrambled with a machine-local key so values never hit the filesystem as readable text; it is never logged.

Works with both `CLIBackend` (desktop/biometric) and `SDKBackend` (`OP_SERVICE_ACCOUNT_TOKEN`, no prompt) — the backend is auto-detected from the environment.

## What ships

- Full `op://` URI grammar — quoted segments, URL encoding, self-markers (`.`), `||` fallback chains.
- Canonical models (`Item`, `ItemField`, `ItemSection`, `ItemURL`, `ItemSummary`) — interchangeable across backends.
- `FieldValue` with sensitivity detection and `to_dict` / `from_dict` JSON persistence.
- Service-account auth first-class via `ServiceAccountAuth.from_env()` (`OP_SERVICE_ACCOUNT_TOKEN`).
- Type hints throughout (`py.typed` ships); strict pyright/mypy clean.
- Sync and async parity — every public class has an async twin.
- Persistent, cross-process secret caching (`FileCachingBackend`) plus the `op-env` command (`[cli]` extra) for running a child process or exporting an environment with `op://` references resolved.

## What it deliberately does not ship

- **No template engine.** Field values are references or literals with optional `||` chains — no `{{...}}` substitution, no `${VAR}` interpolation. Consumers that need richer interpolation do it in their own code before calling `op.read()` / `op.resolve()`.
- **No item CRUD (yet).** `create_item` / `edit_item` / `delete_item` are planned but not landed.

See [`CHANGELOG.md`](CHANGELOG.md) for the full landed surface.

## Status

`v0.5.0` — pre-1.0. The public API is stable enough to build against; minor breaking changes are possible before `v1.0`. Track [`CHANGELOG.md`](CHANGELOG.md) for what changes between releases.

## Documentation

- [`INTEGRATION.md`](INTEGRATION.md) — full guide: backend selection, composition, `||` chains, generate/wrap, async, gotchas.
- [`CHANGELOG.md`](CHANGELOG.md) — release notes.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development setup, test layout, contribution process.
- Source as documentation: `src/op_core/` is small and readable; module docstrings explain design choices.
- Tests as documentation: `tests/unit/` covers every public surface with named scenarios.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
