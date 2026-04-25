# Security Policy

## Reporting a vulnerability

If you find a security issue in `op-core`, please **do not** open a public GitHub issue.

Instead, email the maintainer listed in [`pyproject.toml`](pyproject.toml) with:

- A description of the issue.
- Steps to reproduce, ideally with a minimal proof-of-concept.
- The affected version of `op-core` and the backend(s) involved.
- Any disclosure timeline you have in mind.

You should expect an acknowledgement within a few business days. Once a fix is ready, we will coordinate disclosure with you.

## Scope

`op-core` is a thin toolkit on top of 1Password's CLI and SDK. Its security surface is:

- **Subprocess invocation** in `CLIBackend` (the `op` binary). Auth tokens are passed via environment variables, never on the command line.
- **Secret handling** in `FieldValue` and the `OnePassword` facade. Resolved values pass through Python's normal string handling — no custom in-memory protection.
- **Persistence** via `FieldValue.to_dict()` / `from_dict()`. Anything you serialize lands on disk in plain JSON unless you encrypt it yourself; this is by design.

Issues outside this surface (e.g. bugs in 1Password's CLI or SDK, vulnerabilities in your application's secret-handling code) should be reported upstream or to the affected project.

## Supported versions

`op-core` is in alpha. Until `1.0`, only the latest minor release receives security fixes.

## Disclosure

Once a fix is published, the corresponding `CHANGELOG.md` entry will note that the change addresses a security issue. We will not retroactively redact prior versions from PyPI.
