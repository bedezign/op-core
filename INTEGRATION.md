# Integrating op-core

A practical guide for building against `op-core`: backend selection, composition patterns, async, persistence, and the rough edges to know about.

For a high-level overview, see [`README.md`](README.md). For release notes, see [`CHANGELOG.md`](CHANGELOG.md).

## Status

`v0.1.0` — alpha. The API is stable enough to build against, but minor breaking changes are possible before `v0.2`. Specifically:

- `run_with_env` (subprocess helper) and Item CRUD are not built. See the README's "What it deliberately does not ship" section.
- No template / variable-substitution engine, ever. Field values are full `op://` / `ops://` references or literals, with optional `||` fallback chains. If you need richer interpolation (shell variables, embedded templates, etc.), do it in your own code before calling `op.read()` / `op.resolve()`.

## Install

`op-core` is not on PyPI yet. Install directly from the GitHub repository.

### Direct dependency on GitHub

With [`uv`](https://docs.astral.sh/uv/):

```bash
uv add "op-core @ git+https://github.com/bedezign/op-core"
uv add "op-core[sdk] @ git+https://github.com/bedezign/op-core"   # + official SDK
```

With `pip`:

```bash
pip install "op-core @ git+https://github.com/bedezign/op-core"
pip install "op-core[sdk] @ git+https://github.com/bedezign/op-core"
```

Pin to a tag for reproducibility:

```bash
uv add "op-core @ git+https://github.com/bedezign/op-core@v0.1.0"
pip install "op-core @ git+https://github.com/bedezign/op-core@v0.1.0"
```

In `pyproject.toml`, the equivalent is:

```toml
[project]
dependencies = [
    "op-core @ git+https://github.com/bedezign/op-core@v0.1.0",
]
```

Or, with `uv`'s `[tool.uv.sources]` syntax:

```toml
[project]
dependencies = ["op-core"]

[tool.uv.sources]
op-core = { git = "https://github.com/bedezign/op-core", tag = "v0.1.0" }
```

Python 3.11+. The CLI backend requires the `op` binary on `PATH`. The SDK extra installs `onepassword-sdk` from PyPI.

### Local editable install

For iterating on op-core itself alongside a consumer:

```toml
[project]
dependencies = ["op-core"]

[tool.uv.sources]
op-core = { path = "/path/to/op-core/checkout", editable = true }
```

Adjust the path to wherever your op-core checkout lives. Then run `uv sync`. For the SDK extra: `dependencies = ["op-core[sdk]"]`.

## Public API

All public names are flat-importable from `op_core`. Submodule imports (`from op_core.client import OnePassword`) keep working but the flat form is the recommended shape:

```python
from op_core import (
    OnePassword, AsyncOnePassword,                              # facades
    CLIBackend, AsyncCLIBackend,                                # CLI backend
    SDKBackend, AsyncSDKBackend,                                # SDK backend
    InMemoryBackend, AsyncInMemoryBackend,                      # in-process backend
    CachingBackend, AsyncCachingBackend,                        # decorator
    Backend, AsyncBackend,                                      # protocols (for custom backends)
    detect_backend, detect_async_backend,                       # auto-detection
    Auth, ServiceAccountAuth, DesktopAuth, detect_auth,         # auth types
    Item, ItemField, ItemSection, ItemURL, ItemSummary, ItemRef, # canonical models
    FieldValue, OpRef,                                          # references and field values
    classify_type, is_sensitive, normalize_original,            # field helpers
    complete_field_refs,                                        # reference completion
    TEMPLATE_OPEN, TEMPLATE_CLOSE,                              # reserved markers (op-core does not ship a template engine)
    expand_braces,                                              # string helper
    OpError, OpAuthError, OpNotFoundError,                      # exceptions
    OpTimeoutError, OpOfflineError,
)
```

## Constructing a `OnePassword` client

### Auto-detect (simplest)

```python
from op_core import OnePassword

op = OnePassword()  # detect_backend() picks SDK if token+SDK installed, else CLI
token = op.read("op://Personal/GitHub/token")
```

`detect_backend()` reads `OP_SERVICE_ACCOUNT_TOKEN` and checks whether the `onepassword` SDK package is importable.

### Explicit backend

```python
from op_core import CLIBackend, OnePassword, ServiceAccountAuth

op = OnePassword(backend=CLIBackend(
    auth=ServiceAccountAuth.from_env(),
    binary="op",
    timeout=30,
))
```

### With caching

```python
from op_core import CachingBackend, CLIBackend, OnePassword

op = OnePassword(backend=CachingBackend(CLIBackend(), ttl=300))
```

`CachingBackend` caches `read` and `get_item` results with a TTL and LRU cap. Negative caching is on by default — a missing reference is remembered for the same TTL window so you don't hammer `op` on known misses.

## Core operations

### Reading a single reference

```python
value = op.read("op://Personal/GitHub/token")  # -> str | None
```

Returns `None` if the reference is not found. Raises `OpAuthError` / `OpTimeoutError` / `OpError` on transport failures.

### Listing vaults

```python
vaults = op.list_vaults()
# -> list[VaultSummary]  (each has id and name)

# Vault ids are stable; names are user-controlled. For per-vault scoping,
# resolve a name to an id once and reuse the id:
ssh_vault_id = next(v.id for v in vaults if v.name == "SSH Hosts")
items = op.list_items(vault=ssh_vault_id)
```

Per-vault scoping is significantly faster than the unscoped form on accounts with many vaults — especially with `SDKBackend`, where the unscoped path enumerates vaults and lists each.

### Listing items

```python
summaries = op.list_items(
    vault="Personal",
    tags=["SSH Host"],
    categories=["SSH_KEY"],
)
# -> list[ItemSummary]

ids = [s.id for s in summaries]
```

### Fetching a full item

```python
item = op.get_item("itm_abc123")  # -> Item
# item.fields is a flat tuple of ItemField; walk it directly instead of
# re-reading every field value via op.read()
```

### Union filtering across two queries

`list_items` composes `tags` and `categories` with AND semantics — there is no `match="any"`. For union semantics (e.g. "SSH_KEY category OR tagged `SSH Host`"), issue two queries and dedup by id:

```python
by_category = op.list_items(categories=["SSH_KEY"])
by_tag = op.list_items(tags=["SSH Host"])
summaries = list({s.id: s for s in (*by_category, *by_tag)}.values())
```

Dict-by-id makes the dedup order-preserving with "first hit wins".

### Resolving a fallback chain

```python
from op_core import FieldValue

fv = FieldValue.from_raw(
    "op://Vault/Item/primary||op://Vault/Item/backup||literal-default",
    "password",
)
value = op.resolve(fv)  # -> str | None
```

`resolve` walks the `||` segments left-to-right. Reference segments are read via the backend; literal segments (no `op://` / `ops://` prefix) are returned as-is. Returns the first non-empty hit.

## Generate / wrap pattern

If you're persisting resolved secrets to disk and doing lazy fetches at runtime, compose `InMemoryBackend` with a fallback:

```python
from op_core import (
    CLIBackend,
    FieldValue,
    InMemoryBackend,
    Item,
    OnePassword,
    OpOfflineError,
)

# === Generate phase ===
op_live = OnePassword()
items = [op_live.get_item(s.id) for s in op_live.list_items(tags=["production"])]
# Build FieldValue objects, eagerly resolve non-sensitive ones, serialize via
# FieldValue.to_dict() and persist as JSON.

# === Wrap / runtime phase ===
# Two shapes for the in-memory backend, depending on what you persisted:
#
#   (a) Serialized full Items → pass items=... and InMemoryBackend auto-indexes
#       every non-None, non-reference field value under both its label and id.
#       Reference values (op://..., ops://...) are skipped — they need backend
#       resolution and fall through to `fallback`.
#   (b) Just a flat dict of resolved refs → pass refs=... (typed in as keys).
#
# You can mix both. refs=... wins on collision with the auto-built item index.
known_items: list[Item] = load_persisted_items()       # your own function
known_refs: dict[str, str] = load_explicit_overrides() # optional

op = OnePassword(backend=InMemoryBackend(
    items=known_items,
    refs=known_refs,
    fallback=CLIBackend(),  # fall through to live op CLI for unknowns
))

# Indexed field → local hit, no subprocess
value = op.read("op://Vault/Item/hostname")

# Unknown ref → falls through to CLIBackend → live fetch
secret = op.read("op://Vault/Item/password")

# Safety rail: if a field should be known locally but isn't, fail loudly
# instead of silently going online.
try:
    value = op.read("op://Vault/Item/hostname", online=False)
except OpOfflineError:
    # Local cache is stale — handle gracefully.
    ...
```

### `online=False` semantics

| Backend | Behavior when `online=False` |
|---|---|
| `CLIBackend` / `SDKBackend` | Always raises `OpOfflineError` immediately — no subprocess, no SDK call. |
| `CachingBackend` | Returns a live cached entry if present. On miss or expired entry, raises `OpOfflineError`. Never delegates to the inner backend. |
| `InMemoryBackend` | Returns the value if known locally. On miss, delegates to `fallback` with `online=False` propagated. On terminal miss, raises `OpOfflineError`. |

`OpOfflineError` is distinct from `OpNotFoundError`. The facade's `OnePassword.read` catches `OpNotFoundError` → `None` but deliberately lets `OpOfflineError` propagate so the safety rail actually fires.

## `FieldValue` JSON persistence

```python
import json
from op_core import FieldValue

fv = FieldValue.from_raw("op://Vault/Item/password", "password").with_resolved("hunter2")

# Persist
with open("cache.json", "w") as f:
    json.dump(fv.to_dict(), f)
# {"original": "op://Vault/Item/password", "resolved": "hunter2", "sensitive": true}

# Reload
with open("cache.json") as f:
    restored = FieldValue.from_dict(json.load(f))
# restored == fv (field_type is re-derived via classify_type on load)
```

`from_dict` raises `ValueError` on missing or wrong-typed fields.

## Async

Every sync class has an `Async*` twin with identical semantics:

```python
from op_core import AsyncInMemoryBackend, AsyncOnePassword

op = AsyncOnePassword(backend=AsyncInMemoryBackend(refs={...}))
value = await op.read("op://v/i/f")
```

## Error model

| Exception | Raised when | Caught by `OnePassword.read` (returns `None`)? |
|---|---|---|
| `OpAuthError` | Auth failed (not signed in, expired session, invalid token) | No — propagates |
| `OpNotFoundError` | Reference is confirmed missing | Yes |
| `OpTimeoutError` | Backend exceeded its configured timeout | No — propagates |
| `OpOfflineError` | `online=False` and the request can't be satisfied locally | No — propagates (deliberate; this is a safety rail) |
| `OpError` | Other transport failure | No — propagates |

All five inherit from `OpError`, so a broad `except OpError` catches everything if you don't want to discriminate.

## Gotchas

- **`CachingBackend` does not forward `online=`** to its inner backend. Stacked `CachingBackend(CachingBackend(...))` is unsupported.
- **Category casing is upper-case canonical** (`"LOGIN"`, `"SECURE_NOTE"`). `SDKBackend` normalizes this at the SDK→canonical boundary so both backends produce interchangeable `Item` values.
- **`Item.fields` is a flat tuple.** Fields inside sections carry their `section_id` as a back-reference rather than nesting under a section object.
- **`SDKBackend` only supports service-account auth.** Desktop auth is CLI-only.
- **No `whoami` on the backend protocol.** If you need account identity info, look it up another way — the protocol stays narrow on purpose.
- **Reference-valued fields are skipped during `InMemoryBackend` auto-indexing.** A field whose value starts with `op://` or `ops://` (e.g. a self-reference like `op://././username`) is NOT indexed as a literal — it requires backend resolution and falls through to the configured `fallback`. URLs (`https://...`) are still indexed as ordinary literals.
- **`Item.urls` is exposed for inspection, not resolution.** The 1Password CLI rejects `op read op://vault/item/<url-label>` with a "not a field" error, so `InMemoryBackend` does not address URL labels via `read()`. Inspect `Item.urls` directly (or use `Item.url(label)` / `Item.primary_url()`) to distinguish a URL-label token from a missing field on the same item.
- **Every parsed `ItemURL` has a non-empty label.** When the source payload omits or empties the label, op-core fills in `"website"` — the same default 1Password's UI shows. This means a Login item's unlabeled URLs are findable as `item.url("website")` regardless of how the user did or did not name them, but it also means `item.url("website")` may match URLs that weren't *the* "website" — use `item.primary_url()` when you specifically want the primary URL.
- **`ItemURL.primary` is not populated by the SDK backend.** The SDK's `Website` type has no equivalent flag, so SDK-sourced URLs always carry `primary=False` and `Item.primary_url()` returns `None`. Use `CLIBackend` when the primary marker matters.

## Where to look next

- Source: [`src/op_core/`](src/op_core/) — small and readable; module docstrings explain design choices.
- Tests as documentation: [`tests/unit/`](tests/unit/) covers every public surface with named scenarios.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — running tests, code style, contribution process.
