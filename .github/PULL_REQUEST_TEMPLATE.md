<!--
Thanks for the PR! A short template — no forms, just enough to help the
review.
-->

## What

<!-- One or two sentences. -->

## Why

<!-- Link the issue or motivate the change. -->

## Testing

- [ ] `pytest` passes locally
- [ ] Coverage remains at 100% (`pytest --cov=sync --cov-report=term-missing`)
- [ ] Runtime behavior verified against a real public Shared Album (if you changed `sync_album`, `resolve_shard`, `fetch_stream`, `fetch_asset_urls`, or `download`)
- [ ] `docker build .` succeeds and the resulting image runs (if you changed the `Dockerfile`)

## Checklist

- [ ] This PR does one thing
- [ ] No new runtime dependencies (`src/sync.py` is stdlib-only by design)
- [ ] Tests added or updated for the change
- [ ] README updated if user-visible behavior changed

## Anything reviewers should watch for

<!-- Optional. Call out subtle risks, breaking changes, or things you're
     unsure about — better than us finding them cold. -->
