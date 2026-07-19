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
- **Third-party runtime dependencies.** `src/sync.py` uses only the Python standard library. That constraint keeps the image small (~145 MB) and the supply chain minimal.

## Development setup

Clone the repo and set up a Python virtualenv:

```bash
git clone https://github.com/Bitwise-Forge/icloud-shared-album-sync
cd icloud-shared-album-sync
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Run the tests:

```bash
pytest
```

With coverage:

```bash
pytest --cov=sync --cov-report=term-missing
```

Coverage is expected to stay at **100%**. Every new function or branch needs a test.

## Style expectations

- **Runtime code is stdlib-only.** No `requests`, no `aiohttp`, no runtime pip installs. Dev-time deps (in `requirements-dev.txt`) are fine.
- Keep functions small and readable. This is one Python file for a reason — introducing packages or plugin systems is over-engineering here.
- Comments explain *why*, not *what*. If a comment restates what the code does, delete it.
- Log at `INFO` for state changes and per-asset actions; `DEBUG` for skip decisions and other high-frequency signals.

## Pull request expectations

- **Do one thing.** A refactor + a bug fix in the same PR is harder to review than two small PRs.
- **Add or update tests** for every behavior change. Coverage stays at 100%.
- **Update the README** for user-visible changes (new env vars, new behavior, changed defaults).
- **Verify against a real album** if you touched anything in `sync_album`, `resolve_shard`, `fetch_stream`, `fetch_asset_urls`, or `download`. Tests mock the network — integration confidence requires a live run.
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
