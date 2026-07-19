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
- [ ] Coverage remains at 100% (`pytest --cov=icloud_sync --cov-report=term-missing`)
- [ ] Runtime behavior verified against a real public Shared Album (if you changed anything in `apple_api`, `orchestrator`, or the Apple-facing part of `manifest`)
- [ ] `docker build .` succeeds and the resulting image runs (if you changed the `Dockerfile`)

## Checklist

- [ ] This PR does one thing
- [ ] No new runtime dependencies (the sole runtime dep is `httpx` — see AGENTS.md hard rule #1)
- [ ] Tests added or updated for the change
- [ ] README updated if user-visible behavior changed

## Anything reviewers should watch for

<!-- Optional. Call out subtle risks, breaking changes, or things you're
     unsure about — better than us finding them cold. -->
