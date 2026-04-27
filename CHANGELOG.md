# Changelog

All notable changes to op-core will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-04-27

### Added

- **`list_vaults()`** on `OnePassword` / `AsyncOnePassword` and on every backend (CLI, SDK, in-memory, caching, sync + async). Returns `list[VaultSummary]` for per-vault scoping of subsequent `list_items()` calls — significantly faster on accounts with many vaults.
- **`VaultSummary(id, name)`** — frozen dataclass exported from `op_core`. The lightweight vault view returned by `list_vaults`.

## [0.1.0] — Initial release

### Added

#### Public API

Flat re-exports from `op_core`:

- **Facades** — `OnePassword`, `AsyncOnePassword` (compose any backend with `read`, `resolve`, `list_items`, `get_item`).
- **Backends** — `CLIBackend`, `SDKBackend`, `InMemoryBackend`, `CachingBackend`, plus an `Async*` twin of each. Backend protocols `Backend` / `AsyncBackend` are exported for third-party backends.
- **Auto-detection** — `detect_backend` / `detect_async_backend` pick a backend from the environment (`OP_SERVICE_ACCOUNT_TOKEN` plus SDK availability).
- **Auth** — `Auth`, `ServiceAccountAuth` (with `from_env`), `DesktopAuth`, `detect_auth`.
- **Models** — `Item`, `ItemField`, `ItemSection`, `ItemSummary`, `ItemRef` (canonical, interchangeable across backends).
- **References & field values** — `OpRef`, `FieldValue` (with `||` fallback chains, sensitivity detection, and `to_dict` / `from_dict` JSON persistence).
- **Helpers** — `classify_type`, `is_sensitive`, `normalize_original`, `complete_field_refs`, `expand_braces`, `TEMPLATE_OPEN`, `TEMPLATE_CLOSE`.
- **Exceptions** — `OpError` (base) plus `OpAuthError`, `OpNotFoundError`, `OpTimeoutError`, `OpOfflineError`.

#### Modules

- `op_core.opref` — full `op://` URI grammar with quoted segments, URL encoding, self-markers (`.`), and relative-reference resolution (`OpRef.as_absolute`).
- `op_core.field` — `FieldValue` dataclass with `||` fallback chains, sensitivity detection, `classify_type`, `resolve_chain`, `async_resolve_chain`, and 3-field-format JSON persistence (`field_type` is re-derived on load).
- `op_core.items` — canonical dataclasses; backends normalize into these.
- `op_core.exceptions` — exception hierarchy.
- `op_core.auth` — auth types and detection.
- `op_core.client` — `OnePassword` / `AsyncOnePassword` facades. `online=` propagates through `read` and `resolve` for wrap-phase safety rails.
- `op_core.strings.expand_braces` — brace expansion for comma lists (`host{a,b,c}`) and numeric ranges (`worker{1..8}`).

#### Backends

- `CLIBackend` / `AsyncCLIBackend` — shell out to the `op` binary with timeout and heuristic error mapping. Supports both `DesktopAuth` and `ServiceAccountAuth`.
- `SDKBackend` / `AsyncSDKBackend` — wrap the official `onepassword-sdk` Python package (install via `op-core[sdk]` extra). Service-account auth only. SDK category casing is normalized to upper-case to match `CLIBackend`.
- `InMemoryBackend` / `AsyncInMemoryBackend` — in-process `refs` dict and `items` list, with optional `fallback: Backend | None` chaining. Production-usable for generate / wrap workflows as well as tests. Passing `items=` auto-indexes every non-`None`, non-reference field value under both `op://<vault_id>/<item_id>/<label>` and `op://<vault_id>/<item_id>/<id>`. Reference values (`op://...`, `ops://...`) are skipped — they require backend resolution and fall through to `fallback`. Explicit `refs=` wins on collision.
- `CachingBackend` / `AsyncCachingBackend` — decorator wrapping any other backend with TTL-based expiry, LRU cap, and negative caching. Thread-safe.

#### Cross-cutting

- **Offline-aware reads** — every backend's `read()` accepts `online: bool = True`. Raw backends (CLI/SDK) raise `OpOfflineError` immediately when `online=False`. `CachingBackend` returns live-cached entries or raises without delegating. `InMemoryBackend` honors its local store and propagates the flag to its fallback.
- **Doctests** on `OnePassword.__init__`, `.read`, `.resolve`, `.list_items`, `.get_item` as executable usage proofs.

### Not yet shipped

- `run_with_env` subprocess helper — resolve `op://` references in environment variables before spawning a child process.
- Item CRUD (`create_item` / `edit_item` / `delete_item`).

### Explicit non-goals

- No variable-substitution or template engine. Field values are references or literals with `||` fallback chains — nothing more.
