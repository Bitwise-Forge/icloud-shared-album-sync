# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] - 2026-07-22

Fix: emptied-album sync crashed at the `webasseturls` call instead of
pruning the local cache.

### Fixed

- Empty-album sync no longer crashes. When a Shared Album's photo list
  is empty, Apple's `webasseturls` endpoint rejects an empty
  `photoGuids` list with HTTP 400 "Validation Failed: missing
  photoGuids". The orchestrator was calling it unconditionally and
  raising `RuntimeError` before the prune step ran, leaving stale
  locally-cached files on disk. The orchestrator now short-circuits
  when the manifest has no photos, skips the `webasseturls` call, and
  goes straight to prune — the mirror-the-album contract holds and
  everything previously synced gets evicted.
- Test suite's `_MockPostJson` now mirrors Apple's real behavior and
  returns 400 on empty `photoGuids`. Previously the mock returned 200
  regardless, letting `test_sync_empty_manifest_prunes_all_managed_files`
  falsely pass against unfixed orchestrator code.

## [0.2.0] - 2026-07-19

Storage-aware sync for constrained hosts, broader URL support, and a
much smaller container image.

### Added

- `STORAGE_BUFFER_PERCENT` (default `10`): reserves a percentage of the
  output volume's total capacity as untouchable headroom for the OS,
  logs, and anything else sharing the disk. Accepts a float, rounded to
  two decimal places, validated to `[0, 100)`.
- `AUTOPRUNE_ON_LOW_STORAGE` (default `false`): opt-in cache-eviction
  mode for constrained hosts (Raspberry Pi + SD card, etc.). When the
  album would exceed the available budget, keeps the newest slice that
  fits and prunes older photos locally to make room. Newest is defined
  by `batchDateCreated` (upload time), then `dateCreated` (capture time)
  as a tiebreaker within a batch, then `photoGuid` for full determinism.
- Preflight disk-budget check runs before any downloads. When the album
  would exceed the budget and `AUTOPRUNE_ON_LOW_STORAGE=false`, the sync
  logs a specific error and skips the run without touching disk.
- Support for Apple's short-link URL shape,
  `https://share.icloud.com/photos/TOKEN`, in addition to the classic
  `https://www.icloud.com/sharedalbum/#TOKEN` form.

### Changed

- Base image switched from `python:3.14-slim` to `python:3.14-alpine`.
  Image size dropped from ~146 MB to ~53 MB (-64%) with no functional
  changes.
- Token validation now accepts any base62 first character. The prior
  check hard-coded a `B` prefix based on a single example URL — real
  Apple tokens can start with any base62 character.
- Source is organized as a Python package (`icloud_sync/`) with modules
  split by responsibility: `apple_api` (HTTP transport), `manifest`
  (token / derivative / sort-key logic), `storage` (filenames, pruning,
  disk-budget math), `orchestrator` (`sync_album`), and `cli` (env
  parsing, signal handling, main loop). Test suite split along the same
  boundaries. Container entry point is now `python3 -m icloud_sync`;
  running from source uses `PYTHONPATH=src python3 -m icloud_sync`.

### Removed

- `PRUNE_REMOVED` env var. Pruning is now unconditional — the iCloud
  album is the source of truth and the local folder mirrors it, so
  keeping orphaned assets around served no real use case.

## [0.1.0] - 2026-07-18

Initial public release.

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
- Docker image based on `python:3.14-slim` (~146 MB), running as a non-root
  `app` user (UID 1000).
- Multi-arch (`linux/amd64` + `linux/arm64`) image published to
  `ghcr.io/bitwise-forge/icloud-shared-album-sync`. Each release ships with
  SLSA build provenance and an SPDX SBOM attached.
- Pytest test suite with 100% line and branch coverage (72 tests).
- Quality gate enforced via pre-commit and CI: [Ruff](https://docs.astral.sh/ruff/)
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

[Unreleased]: https://github.com/Bitwise-Forge/icloud-shared-album-sync/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/Bitwise-Forge/icloud-shared-album-sync/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Bitwise-Forge/icloud-shared-album-sync/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Bitwise-Forge/icloud-shared-album-sync/releases/tag/v0.1.0
