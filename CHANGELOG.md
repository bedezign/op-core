# Changelog

All notable changes to op-core will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-05-24

### Added

- **`ItemURL(href, label, primary)`** — frozen dataclass exported from `op_core` representing a URL entry on a 1Password item. `label` defaults to `"website"` (1Password's UI convention for an unlabeled URL) and `primary` defaults to `False`; only `href` is required. Both backend parsers populate these defaults when the source payload omits or empties the corresponding field, so every parsed `ItemURL` carries a non-empty label and a boolean `primary`.
- **`Item.urls: tuple[ItemURL, ...]`** — new attribute on the canonical item model carrying the top-level URLs the upstream JSON has always included but the parser previously discarded. Defaults to `()` so existing `Item(...)` callers keep working.
- **`Item.url(label)`** — return the first URL with a matching label, or `None`. Case-sensitive, symmetric with `Item.field(label)`.
- **`Item.primary_url()`** — return the first URL marked `primary=True`, or `None`. Does not guess by falling back to the first entry.
- URL parsing in `CLIBackend` / `AsyncCLIBackend` (from `data["urls"]`) and `SDKBackend` / `AsyncSDKBackend` (from `sdk_item.websites`). URL entries lacking an `href` are dropped.

### Fixed

- **`op_core.__version__` synced to package version.** It had drifted to `"0.1.0"` since the initial release — `pyproject.toml` was bumped to `0.2.0` in the previous commit but the in-package `__version__` constant was not. As a side effect of this release, `__version__`, `pyproject.toml`, and `_INTEGRATION_VERSION` (the SDK integration string) are all aligned at `0.3.0`. Consumers that read `op_core.__version__` between the 0.2.0 and 0.3.0 releases got the wrong value.
- **README.md and INTEGRATION.md now document `list_vaults()`.** The method shipped in 0.2.0 without user-facing documentation; this release adds the missing prose alongside the URL-feature documentation.

### Notes

- URLs are **not** addressable via `read()`. The 1Password CLI rejects `op read op://vault/item/<url-label>` with a "not a field" error; `InMemoryBackend` matches that contract and does not index URL labels alongside fields. Validators that need to distinguish a URL-label token from a missing field on the same item can inspect `Item.urls` directly.
- The official 1Password SDK's `Website` type has no `primary` flag — it exposes only `url`, `label`, and `autofill_behavior`. URLs sourced via `SDKBackend` / `AsyncSDKBackend` therefore always carry `primary=False`. Use `CLIBackend` if the primary marker matters.

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
