# AGENTS.md

Instructions for AI coding agents working in this repository. This is a quick-reference; [CONTRIBUTING.md](./CONTRIBUTING.md) is authoritative on process.

## What this project is

A Docker-packaged Python utility that syncs public iCloud Shared Albums to a folder. Runtime deps are deliberately minimal — currently just `httpx` for the HTTP layer. Multi-arch image (`linux/amd64` + `linux/arm64`) targeting `ghcr.io/bitwise-forge/icloud-shared-album-sync`. Owned by [Bitwise Forge](https://bitwiseforge.com), MIT-licensed.

The API this tool talks to (`sharedstreams.icloud.com`) is undocumented and unofficial. Apple can change it on any release without notice. Treat that risk seriously.

## Repository layout

```
├── src/icloud_sync/           # Package. Modules split by responsibility.
│   ├── __init__.py            # Public API surface (main, sync_album).
│   ├── __main__.py            # `python -m icloud_sync` entry point.
│   ├── apple_api.py           # HTTP transport + Apple's 330-redirect protocol.
│   ├── manifest.py            # Token / derivative / sort-key logic. Pure.
│   ├── storage.py             # Filenames, pruning, disk-budget math.
│   ├── orchestrator.py        # sync_album — glues the three above together.
│   └── cli.py                 # Env parsing, signal handling, main loop.
├── tests/                     # Pytest suite. 100% line + branch coverage.
│   ├── conftest.py            # Shared fixtures + builders.
│   ├── test_apple_api.py
│   ├── test_manifest.py
│   ├── test_storage.py
│   ├── test_orchestrator.py
│   └── test_cli.py
├── Dockerfile                 # python:3.14-alpine + non-root app user (uid 1000).
├── .dockerignore
├── pyproject.toml             # Project metadata, dep groups, and tool config
│                              # (pytest, coverage, ruff, ty, uv).
├── uv.lock                    # Locked dep tree. Commit this. `uv sync` reads it.
├── .pre-commit-config.yaml    # Git hook: ruff check + format, ty check.
├── .editorconfig              # 4-sp Python, 2-sp YAML, LF, no trailing WS.
├── .github/
│   ├── ISSUE_TEMPLATE/        # YAML forms.
│   ├── PULL_REQUEST_TEMPLATE.md
│   └── dependabot.yml
├── README.md                  # User-facing.
├── CONTRIBUTING.md            # Contributor process. Authoritative.
├── CODE_OF_CONDUCT.md         # Contributor Covenant 2.1.
├── SECURITY.md
├── CHANGELOG.md               # Keep a Changelog.
└── LICENSE                    # MIT.
```

## Hard rules — do not break these

1. **Runtime deps earn their keep.** Prefer stdlib when it's clean. Add a runtime dep only when it removes meaningful boilerplate, delivers capability the stdlib can't easily match, and comes from a well-maintained, minimal-transitive-surface package. Each addition is a design change — discuss it before implementing. Current runtime deps: `httpx`.
2. **Test coverage stays at 100%** — line and branch, measured by `uv run pytest --cov=icloud_sync --cov-report=term-missing`. Every new function, branch, or behavior gets a test.
3. **`ruff check`, `ruff format`, and `ty check` all pass.** These are enforced by pre-commit hooks locally and by CI. Do not bypass with `git commit --no-verify` — the same checks gate merges. Formatter output is authoritative; do not hand-format against it.
4. **Do not commit `photos/`, credentials, or real album URLs** beyond the shared test URL already used in the repo. `.gitignore` covers `photos/` — do not weaken it.
5. **Do not change Apple API request shapes on the strength of tests alone.** The suite mocks `apple_api._post_json` and `apple_api.download`. A change that passes tests but has never touched the real API will ship a regression.
6. **The managed-file naming pattern is load-bearing.** `local_filename()` writes files as `<base>__<8hex>[.ext]`. `_MANAGED_NAME_RE` matches those, and *only* those, for pruning. If either side moves, both must move together, and the round-trip test must still pass.

## Ask before doing

- Adding a new runtime dep (see hard rule #1 for the bar it must clear)
- Removing an existing runtime dep in a way that reintroduces boilerplate
- Changing the shape of Apple API requests or response parsing
- Adding a new top-level module inside `src/icloud_sync/`. The five current modules cover HTTP, manifest reasoning, filesystem, orchestration, and process concerns — a sixth is a design change.
- Changing lint / format / type-check rules (`[tool.ruff]` or `[tool.ty]` in `pyproject.toml`)
- Changing the license, the scope statement, or the copyright holder
- Breaking backward compatibility of env vars or the `_MANAGED_NAME_RE` pattern

## Safe to do without asking

- Add tests for uncovered scenarios
- Fix typos or clarify wording in any Markdown file
- Update the CHANGELOG's `[Unreleased]` section as you make behavior changes
- Bump pinned dev deps in `requirements-dev.txt`
- Improve log messages (respecting the INFO/DEBUG conventions below)
- Refactor within a single function to improve readability without changing behavior

## Development setup

The project uses [uv](https://docs.astral.sh/uv/) for environment and dep management. Install uv once, then:

```bash
uv sync                      # creates .venv/, installs locked deps
uv run pre-commit install    # arms the git hook — do NOT skip
```

After `pre-commit install`, every `git commit` runs `ruff check`, `ruff format`, and `ty check` against staged files. Bad commits are blocked locally *before* they hit the remote.

Prefix commands with `uv run` to execute inside the project's environment without activating the venv. Old-school `source .venv/bin/activate` still works if you prefer.

## Common commands

```bash
# Run the full quality gate (same checks pre-commit runs)
uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check

# Auto-fix lint findings and reformat
uv run ruff check --fix src tests
uv run ruff format src tests

# Run the tests
uv run pytest

# Coverage report
uv run pytest --cov=icloud_sync --cov-report=term-missing

# Run the tool against a real album (dev, no Docker)
SHARED_ALBUM_URL='https://www.icloud.com/sharedalbum/#B2AJ...' \
  OUTPUT_DIR="$PWD/photos" \
  PYTHONPATH=src uv run python -m icloud_sync

# Update a locked dep (e.g. bump pytest)
uv lock --upgrade-package pytest

# Add a new dev dep
uv add --dev <package>

# Build the Docker image (host arch)
docker build -t icloud-shared-album-sync:local .

# Run the built image once against a real album
docker run --rm \
  -e SHARED_ALBUM_URL='https://www.icloud.com/sharedalbum/#B2AJ...' \
  -v "$PWD/photos:/photos" \
  icloud-shared-album-sync:local

# Multi-arch build (both amd64 + arm64). Requires the docker-container driver.
docker buildx create --name isas-multi --driver docker-container --bootstrap
docker buildx build --builder isas-multi \
  --platform linux/amd64,linux/arm64 \
  -t icloud-shared-album-sync:multi .
docker buildx rm isas-multi
```

## Runtime env vars

| Var | Default | Purpose |
| --- | --- | --- |
| `SHARED_ALBUM_URL` | *(required)* | Full public Shared Album URL. Accepts both `https://www.icloud.com/sharedalbum/#TOKEN` and `https://share.icloud.com/photos/TOKEN`. |
| `OUTPUT_DIR` | `/photos` | Where to write assets. |
| `SYNC_INTERVAL_HOURS` | `0` | `0` = single-shot. `> 0` = daemon loop with sleep between syncs. |
| `STORAGE_BUFFER_PERCENT` | `10` | Percentage of the output volume's total capacity to keep untouched. Float, 2-decimal precision, range `[0, 100)`. |
| `AUTOPRUNE_ON_LOW_STORAGE` | `false` | If `true` and the album exceeds the budget, keep the newest slice that fits and prune older photos locally. If `false`, log an error and skip the run without touching disk. |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows skip decisions. |

## Coding conventions

- **Formatter output is authoritative.** `ruff format` decides quote style, line breaks, spacing. Do not fight it. If a formatting choice feels wrong, discuss the config, don't manually override.
- **Lint rules are set in `pyproject.toml`** under `[tool.ruff.lint]`. Current active groups: `E`, `F`, `I`, `UP`, `B`, `SIM`, `RUF`. Adding or removing groups is an "ask first" change.
- **Type checker is `ty`.** Configured in `pyproject.toml` under `[tool.ty]`. When it flags a type mismatch, fix the root cause (use the correct type, not `# type: ignore`) unless the type stub is genuinely wrong.
- **Comments explain *why*, not *what*.** Delete comments that restate code. Keep comments that record an invariant, a workaround, or a non-obvious constraint.
- **Type hints stay light.** Public functions have return types and parameter types for public callers; internal helpers don't need full typing. Do not add defensive hints.
- **Logging levels:**
  - `INFO` for state changes: album metadata, per-asset pull, per-asset prune, sleep, wake, signal received, exit.
  - `DEBUG` for high-frequency signals: per-asset skip decisions, request/response internals.
  - `ERROR` for anticipated failures caught via `try/except` (missing env vars, sync loop failures).
- **Functions stay small.** The package boundaries under `src/icloud_sync/` are load-bearing — put HTTP work in `apple_api`, filesystem work in `storage`, pure data reasoning in `manifest`. Do not blur the lines.
- **No fallbacks or defensive validation for scenarios that can't happen.** Trust internal callers. Only validate at boundaries: env-var reading, Apple response parsing.

## Testing conventions

- Suite is split per-module — `test_apple_api.py`, `test_manifest.py`, `test_storage.py`, `test_orchestrator.py`, `test_cli.py`. A test belongs in the file that matches the module it exercises.
- Shared fixtures (`album`, `main_env`) and shared builders (`_stream_fixture`, `_asset_urls_fixture`, `_MockPostJson`, `_large_album_stream`, `_large_asset_urls`) live in `tests/conftest.py`. Import plain builders with `from conftest import ...`; fixtures are auto-injected by name.
- Use `@pytest.mark.parametrize` for table-driven tests. Prefer one parametrized test with N cases over N near-identical test methods.
- Use `monkeypatch` for stubbing — not `unittest.mock.patch` context managers.
- Use `tmp_path` for filesystem work — not `tempfile.TemporaryDirectory` in setup/teardown.
- Never touch Apple's real API from the suite. Every network call is stubbed at `apple_api._post_json` or `apple_api.download`.
- Never write outside `tmp_path`. Every filesystem test uses the pytest fixture.
- The coverage config in `pyproject.toml` excludes `if __name__ == "__main__":` lines and the `__main__.py` trampoline — do not add other exclusions without discussion.

## Docker specifics

- **Two-stage build.** Builder stage (`ghcr.io/astral-sh/uv:python3.14-alpine`) installs runtime deps into a venv via uv (using `pyproject.toml` + `uv.lock`); runtime stage (`python:3.14-alpine`) copies just the venv and the `icloud_sync` package. Keeps uv itself out of the shipped image.
- Runtime user: `app`, UID 1000, `nologin` shell. Named `app` because both Debian and BusyBox/Alpine ship a stock `sync` user that `adduser` would collide with.
- ENTRYPOINT is JSON array (exec form) invoking `python3 -m icloud_sync`, so `SIGTERM` reaches Python's signal handler cleanly.
- Image size target: keep under 60 MB. Currently ~53 MB.
- `PYTHONUNBUFFERED=1` in ENV — logs stream live to `docker logs`, no `-u` flag needed.
- `PATH="/app/.venv/bin:$PATH"` in ENV so `python3` resolves to the venv's interpreter.
- Multi-arch builds require the `docker-container` driver; the default `docker` driver is single-arch only.

## Scope guardrails

**In scope:** reliability, Apple API compatibility, edge cases (unusual filenames, large albums, transient network errors), sidecar metadata (contributor / caption / dateCreated), Docker image improvements, docs.

**Out of scope:**

- Authenticated (non-public) Shared Album support — this tool intentionally does not touch Apple ID credentials.
- Full iCloud Photos Library sync — use [icloudpd](https://github.com/icloud-photos-downloader/icloud_photos_downloader) instead.
- Third-party runtime deps.

If a task is ambiguously in or out of scope, open a draft issue and ask before writing code.

## Cross-references

- **User-facing docs:** [README.md](./README.md)
- **Contributor process:** [CONTRIBUTING.md](./CONTRIBUTING.md)
- **Change log:** [CHANGELOG.md](./CHANGELOG.md)
- **Security reporting:** [SECURITY.md](./SECURITY.md)
- **Community expectations:** [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md)

If `AGENTS.md` conflicts with any linked doc above, the linked doc wins.
