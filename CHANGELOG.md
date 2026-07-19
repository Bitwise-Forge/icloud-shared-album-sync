# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Python scraper for public iCloud Shared Albums (`src/sync.py`) using
  [httpx](https://www.python-httpx.org/) as the sole runtime dependency.
  Comes with sane timeouts, streaming downloads for large videos, and
  clean handling of Apple's 330 shard-redirect protocol.
- Environment-variable configuration: `SHARED_ALBUM_URL`, `OUTPUT_DIR`,
  `SYNC_INTERVAL_HOURS`, `PRUNE_REMOVED`, `LOG_LEVEL`.
- Idempotent syncs, keyed by filename + declared file size — re-runs skip
  assets already on disk.
- Collision-proof local filenames: original name plus an 8-hex hash of the
  asset's `photoGuid`. Prevents overwrites when contributors share filenames
  and marks files as owned by this tool for safe pruning.
- Deletion reconciliation: files that this tool downloaded but are no longer
  in the album's manifest are removed on the next sync. Opt out with
  `PRUNE_REMOVED=false`. Files not matching the tool's naming pattern are
  never touched.
- Signal-clean daemon loop when `SYNC_INTERVAL_HOURS > 0`. `SIGTERM` /
  `SIGINT` drain gracefully.
- Docker image based on `python:3.13-slim` (~145 MB), running as a non-root
  `app` user (UID 1000).
- Multi-arch (`linux/amd64` + `linux/arm64`) buildability via `docker buildx`.
- Pytest test suite with 100% line and branch coverage (72 tests).
- Quality gate enforced via pre-commit: [Ruff](https://docs.astral.sh/ruff/)
  for lint + format, [ty](https://docs.astral.sh/ty/) for type checking.
  Same tools run in CI so `--no-verify` bypasses don't save you.
- [uv](https://docs.astral.sh/uv/) as the environment and dependency manager.
  `uv sync` provisions a locked venv from `pyproject.toml` + `uv.lock`,
  `uv run <cmd>` executes tools inside it without needing shell activation.
  Same install byte-for-byte across contributors and CI.
- Contribution documentation: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`
  (Contributor Covenant v2.1), `SECURITY.md`, `AGENTS.md` (AI coding-tool
  onboarding), pull-request template, bug and feature-request issue
  templates.

### Known limitations

- No contributor / caption / `dateCreated` sidecar written yet.
- Only public Shared Albums are supported. Private (auth-required) albums are
  out of scope by design.

[Unreleased]: https://github.com/Bitwise-Forge/icloud-shared-album-sync/compare/HEAD...HEAD
