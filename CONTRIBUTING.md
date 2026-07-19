# Contributing

Thanks for wanting to help. This is a small, single-purpose tool with a narrow scope, but there's real work to be done keeping it in step with Apple's undocumented API and improving its ergonomics. Bug reports, small focused PRs, and documentation improvements are all welcome.

## Ways to contribute

- **File a bug report** — open an issue using the "Bug report" template. Include the log output at `LOG_LEVEL=DEBUG` and (if you can share it) the album URL that triggered the issue.
- **Propose a feature** — open an issue using the "Feature request" template. Check the *Scope* section below first.
- **Send a pull request** — for bug fixes, small features, doc improvements, or Apple API compatibility patches.
- **Improve the tests** — if you find a scenario the suite doesn't cover, a test that exposes the gap is one of the highest-leverage contributions.

## Scope

This tool exists to sync **public** iCloud Shared Albums (the "Public Website" toggle) to a folder. In scope:

- Reliability, correctness, and Apple API compatibility fixes
- Better handling of edge cases (unusual filenames, large albums, transient network errors)
- Sidecar metadata (contributor, caption, `dateCreated`) written alongside media files
- Docker image improvements (smaller size, better logging shape, healthchecks)
- Documentation

Out of scope:

- **Authenticated (non-public) Shared Album support.** This tool intentionally does not touch Apple ID credentials.
- **Full iCloud Photos Library sync.** Use [icloudpd](https://github.com/icloud-photos-downloader/icloud_photos_downloader) if that's what you need.
- **Casual third-party runtime dependencies.** Every runtime dep has to earn its keep — removing meaningful boilerplate or delivering capability the stdlib can't cleanly match. Current runtime deps: `httpx`. See AGENTS.md hard rule #1 for the bar new additions must clear. The image is ~53 MB and the supply-chain surface is deliberately small.

## Development setup

The project uses [uv](https://docs.astral.sh/uv/) for environment and dependency management. Install uv once ([install guide](https://docs.astral.sh/uv/getting-started/installation/)), then:

```bash
git clone https://github.com/Bitwise-Forge/icloud-shared-album-sync
cd icloud-shared-album-sync
uv sync                        # creates .venv/, installs dev deps from uv.lock
uv run pre-commit install      # arms the quality-gate git hook
```

`uv sync` reads `pyproject.toml` and `uv.lock` and produces a byte-identical environment across contributors and CI. `uv run <cmd>` runs commands inside that environment without needing to activate the venv.

`pre-commit install` is not optional. It arms the git hook that runs `ruff check`, `ruff format`, and `ty check` on every commit. Skip it and your first PR will bounce off CI for issues you could have caught locally.

Run the tests:

```bash
uv run pytest
```

With coverage:

```bash
uv run pytest --cov=icloud_sync --cov-report=term-missing
```

Coverage is expected to stay at **100%**. Every new function or branch needs a test.

## Quality gate

Three tools run automatically on every commit (via pre-commit) and on every push (via CI):

| Tool | Purpose | Config location |
| --- | --- | --- |
| [Ruff](https://docs.astral.sh/ruff/) `check` | Lint. Catches unused imports, dead code, common bug patterns, unsorted imports. | `[tool.ruff.lint]` in `pyproject.toml` |
| [Ruff](https://docs.astral.sh/ruff/) `format` | Format. Opinionated, Black-compatible. Formatter output is authoritative. | `[tool.ruff.format]` in `pyproject.toml` |
| [ty](https://docs.astral.sh/ty/) `check` | Type check. Fast, incremental, from the makers of Ruff and uv. | `[tool.ty]` in `pyproject.toml` |

To run them manually before committing:

```bash
uv run ruff check src tests           # lint
uv run ruff format --check src tests  # verify formatted
uv run ty check                       # type check
```

To auto-fix:

```bash
uv run ruff check --fix src tests
uv run ruff format src tests
```

**Do not bypass with `git commit --no-verify`.** The same checks gate merges — you'll just discover the failure on GitHub instead of locally.

## Style expectations

- **Runtime deps stay minimal.** Currently just `httpx`. Adding another is a design change — open an issue first to discuss the boilerplate it removes or the capability it delivers. Dev-time deps (in `[dependency-groups.dev]` in `pyproject.toml`) don't count against this.
- Keep functions small and readable. The source is organized as an `icloud_sync/` package with modules split by responsibility (`apple_api`, `manifest`, `storage`, `orchestrator`, `cli`) — respect those boundaries. Anything that talks HTTP goes in `apple_api`; filesystem work goes in `storage`; pure data reasoning goes in `manifest`.
- Comments explain *why*, not *what*. If a comment restates what the code does, delete it.
- Log at `INFO` for state changes and per-asset actions; `DEBUG` for skip decisions and other high-frequency signals.

## Pull request expectations

- **Do one thing.** A refactor + a bug fix in the same PR is harder to review than two small PRs.
- **Add or update tests** for every behavior change. Coverage stays at 100%.
- **Update the README** for user-visible changes (new env vars, new behavior, changed defaults).
- **Verify against a real album** if you touched anything in `orchestrator.sync_album` or `apple_api` (`resolve_shard`, `fetch_stream`, `fetch_asset_urls`, `download`). Tests mock the network — integration confidence requires a live run.
- **Verify the Docker build** if you changed the `Dockerfile`. Ideally for both `linux/amd64` and `linux/arm64` via `docker buildx`.
- **Small commits, meaningful messages.** No pressure to squash if the history reads clean.

The PR template auto-loads when you open a pull request and walks through the above as checkboxes.

## Apple API risk

The `sharedstreams.icloud.com` endpoints this tool depends on are undocumented and unofficial. Apple can change them on any iOS/macOS release. If you're touching request/response shapes:

- Test against multiple real albums with different asset shapes (photos only, videos only, mixed, one asset, many assets).
- Note the iOS version you tested with in the PR description.
- Consider whether the change should be defensive (accepting both old and new response shapes) or a hard version bump.

## Reporting security issues

Please don't file security issues as public GitHub issues. See [SECURITY.md](./SECURITY.md) for the private reporting channel.

## Licensing

By submitting a pull request, you agree that your contribution is licensed under the MIT License (see [LICENSE](./LICENSE)).

## Code of conduct

Participation in this project is governed by the [Contributor Covenant Code of Conduct](./CODE_OF_CONDUCT.md).

## Questions

Not sure whether an idea is in scope, or whether a bug is worth reporting? Open a draft issue and we'll figure it out together. Better to check first than to spend hours on a PR that gets closed.

Thanks again.
