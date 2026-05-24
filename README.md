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
```

With `pip`:

```bash
pip install "op-core @ git+https://github.com/bedezign/op-core"
pip install "op-core[sdk] @ git+https://github.com/bedezign/op-core"
```

Pin to a tag for reproducibility:

```bash
uv add "op-core @ git+https://github.com/bedezign/op-core@v0.1.0"
```

Python 3.11+. Zero required dependencies for the base install. The CLI backend requires the `op` binary on `PATH`; the SDK extra installs `onepassword-sdk` from PyPI.

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
| `CachingBackend` / `AsyncCachingBackend` | Decorator over any backend | Inherits | TTL-bounded read caching with LRU cap and negative caching |

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

## What ships

- Full `op://` URI grammar — quoted segments, URL encoding, self-markers (`.`), `||` fallback chains.
- Canonical models (`Item`, `ItemField`, `ItemSection`, `ItemURL`, `ItemSummary`) — interchangeable across backends.
- `FieldValue` with sensitivity detection and `to_dict` / `from_dict` JSON persistence.
- Service-account auth first-class via `ServiceAccountAuth.from_env()` (`OP_SERVICE_ACCOUNT_TOKEN`).
- Type hints throughout (`py.typed` ships); strict pyright/mypy clean.
- Sync and async parity — every public class has an async twin.

## What it deliberately does not ship

- **No template engine.** Field values are references or literals with optional `||` chains — no `{{...}}` substitution, no `${VAR}` interpolation. Consumers that need richer interpolation do it in their own code before calling `op.read()` / `op.resolve()`.
- **No item CRUD (yet).** `create_item` / `edit_item` / `delete_item` are planned but not landed.
- **No `run_with_env` subprocess helper (yet).** Resolving `op://` references in environment variables before spawning a child process is on the roadmap.

See [`CHANGELOG.md`](CHANGELOG.md) for the full landed surface.

## Status

`v0.1.0` — alpha. The public API is stable enough to build against; minor breaking changes are possible before `v0.2`. Track [`CHANGELOG.md`](CHANGELOG.md) for what changes between releases.

## Documentation

- [`INTEGRATION.md`](INTEGRATION.md) — full guide: backend selection, composition, `||` chains, generate/wrap, async, gotchas.
- [`CHANGELOG.md`](CHANGELOG.md) — release notes.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development setup, test layout, contribution process.
- Source as documentation: `src/op_core/` is small and readable; module docstrings explain design choices.
- Tests as documentation: `tests/unit/` covers every public surface with named scenarios.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
