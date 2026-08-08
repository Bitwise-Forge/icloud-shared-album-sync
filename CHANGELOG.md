# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-08-08

Fix: large albums silently stopped syncing. Apple's manifest calls scale
with asset count, and a 210-asset album consistently exceeded the
hardcoded 30-second timeout — every cycle, on every frame serving that
album.

### Fixed

- **Large-album syncs no longer stall silently.** Apple's `webstream`
  latency scales with the album's *asset count*, not its byte volume.
  Measured against live albums: 103 assets → ~17s, 210 assets → ~32s,
  against a hardcoded 30s ceiling. Past roughly 195 assets the call
  failed on every cycle with `httpx.ReadTimeout`, aborting the run
  before anything downloaded. Nothing downstream showed a symptom — a
  photo frame kept cycling its existing cache while new contributions
  silently never arrived. The base ceiling is now 180s and configurable.
- **A failed asset download no longer aborts the whole cycle.** Any
  exception from a single download ended the run, skipping every
  remaining asset *and* the prune step, so an asset deleted from the
  album could linger indefinitely. Failures are now logged per asset,
  the partial file is removed, and the cycle continues.
- **Failed runs no longer cost a full interval.** A run that raised sent
  the daemon straight into `SYNC_INTERVAL_HOURS` of sleep — an hour of
  staleness on an hourly frame for what may have been a momentary blip.

### Added

- `APPLE_API_TIMEOUT_SECONDS` (default `180`): read timeout for Apple's
  manifest calls. Covers ~1200 assets by the observed linear trend.
- `DOWNLOAD_TIMEOUT_SECONDS` (default `120`): read timeout for a single
  asset download, separate from the manifest timeout because large
  videos genuinely take time to stream.
- `RETRY_SLEEP_MINUTES` (default `5`): in daemon mode, how long to wait
  after a failed run before retrying. Doubles per consecutive failure,
  capped at `SYNC_INTERVAL_HOURS`, reset by the first success.
- One-shot timeout escalation. A manifest call that times out is retried
  exactly once at double the ceiling; a second timeout gives up rather
  than doubling again. Escalation is sticky per `(host, endpoint)` for
  the process lifetime — an album too slow for the base ceiling will be
  too slow next cycle, so later calls skip the doomed base attempt — and
  clears automatically once that endpoint answers inside the base
  ceiling. Keyed per endpoint because the three calls have very
  different profiles against the same album (shard probe ~0.8s,
  `webstream` ~32s, `webasseturls` ~6.5s); shared state let a fast call
  clear the escalation `webstream` depended on, causing it to re-time-out
  and re-escalate every cycle.
- Early-warning log line when any manifest call consumes more than half
  its ceiling, so a growing album surfaces in the logs while it is still
  succeeding rather than after it starts failing.
- Systemic-failure detection: a cycle where every attempted download
  failed raises rather than reporting success, which routes it to the
  fast-retry backoff instead of a full-interval sleep.

### Changed

- Download timeouts are now a single configurable read window rather
  than the previous split 60s-total / 120s-read pair, which could abort a
  large video that was streaming healthily.

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

[Unreleased]: https://github.com/Bitwise-Forge/icloud-shared-album-sync/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Bitwise-Forge/icloud-shared-album-sync/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/Bitwise-Forge/icloud-shared-album-sync/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Bitwise-Forge/icloud-shared-album-sync/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Bitwise-Forge/icloud-shared-album-sync/releases/tag/v0.1.0
