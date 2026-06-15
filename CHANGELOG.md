# Changelog

All notable changes to op-core will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] — 2026-06-11

### Added

- **`ResolverStack(layers, source)` / `AsyncResolverStack(layers, source)`** — an ordered list of cache layers over one source backend. Satisfies the `Backend` protocol, so `OnePassword(backend=stack)` works unchanged. The read walk is first-live-hit-wins with read-through back-fill into the writable layers above the hit; `get_item` / `list_items` / `list_vaults` route straight to the source. Fan-out `clear()` / `clear_misses()` touch every writable layer. Exported flat from `op_core`.
- **`MemoryLayer(ttl=300.0, max_entries=1024)`** — in-process read-write layer backed by an LRU store with monotonic-clock TTL expiry. Replaces the retired `CachingBackend`'s `read()` memoization. (`get_item` memoization and the `is_expired` predicate hook do not carry over — see Removed below.)
- **`FileReaderLayer(bucket="default", path=None)`** — read-only observer of one named on-disk set. Loads one consistent snapshot at construction (lock-free; atomic writes make it safe), honors the set's stored TTL, treats future-dated entries as expired, and degrades gracefully to "no entries" when the file is missing, corrupt, or holds no such bucket. Never writes: no entries added, no misses recorded, no purge rewrite, no lock sidecar.
- **`FileWriterLayer(ttl, bucket="default", path=None, max_entries=1024)`** — read-write on-disk layer over the same scrambled, purge-on-load, locked-merge-on-persist engine that shipped in 0.5.0. **`ttl` is required and has no default** — persisting a secret to disk must be an explicit caller choice. Exposes `clear()` / `clear_misses()` that retract both the in-memory mirror and the on-disk set, so a later merge cannot resurrect cleared entries.
- **`clear_cache_file(path=None)`** — delete the whole cache file (every set, every bucket), taken under the inter-process lock. Called by `op-cache clear`. Exported flat from `op_core`.
- **`op-cache` command** — standalone cache-management CLI, pure standard library, in the base install (no extra required):
  - `op-cache clear` — delete the cache file. Cold; no auth.
  - `op-cache info` — print file metadata and per-set statistics (bucket id, value and miss counts, stored TTL, entry ages, time to next expiry). Cold; no auth. **Never prints secret values or `op://` reference strings** — those are exactly what the on-disk scrambling protects.
  - `op-cache refresh --bucket ID` — re-resolve one named set's live entries through a source backend and re-store them under the set's own stored TTL. Interactive and auth-gated: with desktop auth this triggers an approval prompt (possibly biometric). **Do not bury it in non-interactive automation** that cannot satisfy the prompt — a stalled prompt looks like a hang. `--bucket` is required; there is no whole-file refresh. Limitation: refresh extends a live set before expiry; it cannot resurrect a set after expiry (expired entries are purged from the file).

### Changed

- **Per-layer expiry now uses a two-sided bound (`0 <= age <= ttl`):** a future-dated entry (clock skew) is treated as expired by the on-disk writer's lookup and purge too, matching the reader. Clock skew is not immortality.

### Removed

- **BREAKING: the caching decorators are removed.** `CachingBackend`, `AsyncCachingBackend`, `FileCachingBackend`, and `AsyncFileCachingBackend` no longer exist. Compose caching explicitly instead: wrap your source backend in a `ResolverStack` and place the layers you want — `MemoryLayer` for in-process caching, `FileWriterLayer(ttl=...)` for the on-disk cache (the TTL is now required and has no default). Cache files written by previous releases remain readable. Two features do not return: `get_item()` results are no longer cached anywhere (only `read()` goes through the layers), and the `is_expired` callback has no layer equivalent. See the migration recipes in the README.
- **BREAKING: `op-env` no longer persists resolved secrets by default, and `--no-cache` is removed.** From 0.4.0 through 0.5.0, every `op-env` run wrote resolved values to the on-disk cache with a 300 second TTL unless `--no-cache` was passed. Caching is now strictly opt-in: pass `--ttl N` (N > 0) to enable it. Runs without `--ttl` resolve through 1Password every time — with desktop auth this means an approval prompt per run. To restore the previous behavior, add `--ttl 300`. `--no-cache` is removed outright: invocations that still pass it now fail with an unknown-flag error — delete the flag; its behavior is the new default. Rationale: writing secrets to disk must be an explicit choice, never a default side effect.
- **`ttl_is_expired` is removed** from `op_core.backends`. It was the default-expiry predicate for the retired decorators and has no caller now that `MemoryLayer` inlines its TTL check. The one-line equivalent, if you need it, is `(time.monotonic() - cached_at) > ttl`.

### Notes

- **On-disk format unchanged.** Cache files written by 0.4.0 and 0.5.0 remain readable; no migration of the file itself is needed.
- **Staleness caveat.** A cached secret is the value that was live at the moment it was written, served unchanged for the full TTL window. Rotating or editing the item in 1Password does not invalidate any cache entry: there is no invalidation signal, and reads never re-check upstream while an entry is live. If a credential is rotated mid-window, every consumer of the cache keeps receiving the retired value until the entry expires, the cache is cleared (`op-cache clear`), or re-resolved (`op-cache refresh`). This applies to any writer layer, memory or file. Choose TTLs with your rotation procedures in mind, and make `op-cache clear` part of any manual rotation runbook.

## [0.5.0] — 2026-06-10

### Changed

- **`FileCachingBackend` / `AsyncFileCachingBackend` storage reworked** — all processes now share a single scrambled cache file (`cache.bin`) holding one **set** of entries per caching context, instead of one plaintext JSON file per reference set:
  - **Sets, keyed by `bucket`.** Both backends take a `bucket` id naming their set (`op-env` passes a hash of the composed environment's `op://` reference set, as before). The same credential cached under two sets is two independent entries — deliberate TTL isolation per context.
  - **Writer-owned per-set TTL.** The TTL a backend is constructed with is stamped into its set and obeyed by every later reader, so a reader can never keep a value beyond the writer's intention. A backend whose constructed TTL differs from its stored set's discards and rebuilds the set — a different TTL means the cache is reconstructed, never reinterpreted.
  - **Purge-on-load.** Every load walks *all* sets, drops entries expired by their own set's TTL (and sets left empty), and rewrites the file if anything was dropped — so any invocation scrubs everyone's stale values, not just its own.
  - **Locked merge-on-persist.** Writes re-read the file under an exclusive `flock` (sidecar `.lock` file) and merge newest-wins, so concurrent runs can neither clobber each other's sets nor resurrect just-purged entries.
  - **Scrambled at rest.** The payload is zlib-compressed and XOR-ed with a SHA-256 keystream derived from machine-local material (machine-id + uid) and a per-write random nonce, so resolved secrets never hit the filesystem as readable text and the file is useless copied off the machine. This is obfuscation, not encryption — it defeats casual reading, grep, secret scanners, and offline copies, not a same-user process. The existing protections (`0600` file in `0700` dir, ownership/symlink checks, atomic writes, degrade-to-inner-backend on corruption) are unchanged.

## [0.4.0] — 2026-06-09

### Added

- **`FileCachingBackend` / `AsyncFileCachingBackend`** — a persistent caching decorator that mirrors `CachingBackend` but writes the resolved reference→value map to a file with a TTL, so cache hits skip the wrapped backend (and thus skip `op` / the biometric prompt) **across separate process invocations**. The in-process `CachingBackend` does nothing for a short-lived CLI relaunched each run; this one lets repeated runs within a TTL window authenticate to 1Password at most once. Wraps any backend: `FileCachingBackend(CLIBackend(), ttl=300, path=...)`. Exported from `op_core`.
  - **Wall-clock TTL.** Entries are stamped with `time.time()` (not the monotonic clock `CachingBackend` uses, which resets every process start), so the TTL is meaningful across runs.
  - **Secret-aware storage.** The cache file holds resolved secret values, so it is written `0600` inside a `0700` directory, defaults to a RAM-backed location (`$XDG_RUNTIME_DIR/op-core/`, else `$TMPDIR/op-core-<uid>/`), is written atomically (temp file + `os.replace`), and is ignored on load if its ownership or permissions look tampered with. A corrupt or unreadable cache never crashes the caller — it degrades to the wrapped backend with a non-secret warning.
  - Honours the same `max_entries` LRU cap and negative caching as `CachingBackend`. `ttl<=0` disables persistence (the backend becomes a pass-through). Only `read()` is persisted; `get_item` / `list_items` / `list_vaults` pass through.
- **`default_cache_dir()`** — returns (creating `0700`) the directory persistent caches live in. Exported from `op_core`.
- **`op-env` command** — a console entry point (behind the `[cli]` extra: `pip install 'op-core[cli]'`) that composes an environment, resolves any `op://` references in it via op-core, and then either execs a child process or prints the result. Two subcommands:
  - `op-env exec [options] -- <command> [args...]` — replace the current process (`os.execvpe`) with `command` running under the resolved environment. A true exec (no lingering parent) matters for stdio JSON-RPC pipes. **Never prints resolved secret values.**
  - `op-env export [options] [--format env|json]` — print the resolved environment (only the keys the `.env` files introduced) as shell-safe `KEY='value'` lines (for `set -a; eval "$(...)"; set +a`) or a JSON object (for a headers helper). **Prints secret values by design** — only for `eval`/headers consumption, never an interactive terminal or a log.
  - Loads zero or more `--env-file PATH` files (repeatable, layered — first file to set a key wins; `--override` makes later files win). `--ttl SECONDS` (default 300) and `--no-cache` control the persistent cache; `--require KEY...` hard-fails if a named key is unresolved or empty.
  - **File-only by default.** The inherited process environment is ignored entirely — not inherited by the child, not an interpolation source. `--inherit-env` takes it along as the base (and interpolation source); `.env` files always override inherited values, so `PATH=${PATH}/extra` extends the inherited `PATH`. `--keep KEY` / `--drop KEY` (repeatable, require `--inherit-env`) allow- and deny-list the inherited variables; the filter is applied **before** the inherited env is used as either base or interpolation source, so a dropped variable can neither be inherited nor smuggled out via `LEAK=${DROPPED}`.
  - **`${VAR}` / `${VAR:-default}` interpolation** is applied to `.env`-introduced values, once, in a single forward pass, **before** `op://` resolution. References resolve against the inherited environment (only with `--inherit-env`) and earlier values — never an implicit `os.environ`. Resolved `op://` secret values are never themselves interpolated, so a secret containing `${...}` or `$` is passed through verbatim (no mangling, no environment injection from vault content). There is no recursive/fixpoint resolution.
  - `--ascend` additionally collects `.env` files by walking **up** the directory tree (nearest directory wins). It anchors on each `--env-file`'s directory (or the current directory when none is given) and looks for the basename of each `--env-file` plus any `--env-file-name NAME` (default `.env`). `--ascend-until PATH_OR_NAME` (repeatable) stops the walk at the first matching ancestor — a value with no `/` matches an ancestor directory by name, otherwise it is an exact path; the default boundary is `$HOME`. A hard security ceiling always applies: the walk never enters a world-writable, not-owned, or different-filesystem directory, and symlinked / world-writable / not-owned `.env` files are skipped — since the result feeds `exec`, an attacker who can plant a `.env` in an untrusted ancestor must not be able to inject environment into the child.
  - Backs resolution with `FileCachingBackend` keyed on a per-invocation cache file. The file name is a hash of the **set** of `op://` references in the composed environment (order-, key-name-, and downstream-argument-independent), so repeated runs resolving the same secrets share one cache file and one authentication, while unrelated invocations stay isolated and never clobber each other.

### Notes

- op-core's base install stays zero-dependency. `op-env` requires the `[cli]` extra (`python-dotenv` for `.env` parsing); `FileCachingBackend` is pure standard library and ships in the base.
- The intended pattern is that the on-disk `.env` holds **`op://` references, not raw secret values**, so the `.env` itself is safe at rest. op-core resolves the references at launch.

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
