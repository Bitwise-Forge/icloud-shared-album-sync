# icloud-shared-album-sync

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![Contributor Covenant](https://img.shields.io/badge/Contributor%20Covenant-2.1-4baaaa.svg)](./CODE_OF_CONDUCT.md)

Keep a folder in sync with a public iCloud Shared Album — photos, videos, and all — using a tiny Docker image with zero third-party dependencies.

Point it at a Shared Album URL, mount a folder, and let it run. Anything anyone adds to the album shows up in the folder on the next sync. Delete something from the album and it disappears locally too. Great for digital picture frames, family photo backups, or any workflow that ends with "…and now I want those photos on my server."

## Quickstart

```bash
docker run --rm \
  -e SHARED_ALBUM_URL='https://www.icloud.com/sharedalbum/#B2AJ...' \
  -v "$PWD/photos:/photos" \
  ghcr.io/bitwise-forge/icloud-shared-album-sync:latest
```

One shot — pulls the current album contents into `./photos/` and exits. Set `SYNC_INTERVAL_HOURS` if you want it to loop on its own.

> **Pre-release:** the GHCR image starts publishing at `v0.1.0`. Until then, [build it yourself](#building-the-image) — the Dockerfile is in the repo and takes about 30 seconds.

## Docker Compose

Compose is the recommended shape if you have more than one album. One service per album, each with its own URL, its own folder, and its own cadence:

```yaml
services:
  frame-parents:
    image: ghcr.io/bitwise-forge/icloud-shared-album-sync:latest
    environment:
      SHARED_ALBUM_URL: 'https://www.icloud.com/sharedalbum/#B2AJ...'
      SYNC_INTERVAL_HOURS: '12'
    volumes:
      - ./photos/frame-parents:/photos
    restart: unless-stopped

  frame-in-laws:
    image: ghcr.io/bitwise-forge/icloud-shared-album-sync:latest
    environment:
      SHARED_ALBUM_URL: 'https://www.icloud.com/sharedalbum/#B2BK...'
      SYNC_INTERVAL_HOURS: '24'
    volumes:
      - ./photos/frame-in-laws:/photos
    restart: unless-stopped
```

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `SHARED_ALBUM_URL` | *(required)* | The full public Shared Album URL, including the `#B...` fragment. Get it from Photos.app → Share → Public Website. |
| `OUTPUT_DIR` | `/photos` | Where inside the container to write assets. Mount a host folder here. |
| `SYNC_INTERVAL_HOURS` | `0` | If `> 0`, run continuously and sleep this many hours between syncs. If `0` (the default), sync once and exit. |
| `PRUNE_REMOVED` | `true` | If `true`, files this tool downloaded that are no longer in the album get deleted locally on the next sync. Set to `false` to keep local copies of removed assets. Only files that match the tool's naming pattern (see below) are ever pruned — files you dropped in the folder by hand are safe. |
| `LOG_LEVEL` | `INFO` | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

## How it works

Apple's Shared Streams API is a short conversation:

1. Resolve the correct shard host for this album.
2. Fetch the album manifest — one entry per photo/video, with contributor, date, caption, and per-derivative CDN references.
3. Fetch signed CDN URLs (~3 hour expiry) for the assets we want.
4. Download the best available derivative per asset. Photos use the largest numeric derivative (typically `2048`, the long-edge in pixels). Videos use `720p` when present, `360p` otherwise.

Files land under the filename Apple assigns, with a short hash of the asset's unique ID appended before the extension: `IMG_5744.JPG` → `IMG_5744__a1b2c3d4.JPG`. That hash is deterministic per asset, which does two things: it prevents collisions when two contributors happen to upload files with the same name, and it marks the file as "managed by this tool" so `PRUNE_REMOVED` can safely clean up without touching anything else in the folder.

EXIF, GPS, and iPhone-model metadata come through untouched inside Apple's shared-album compression. Re-runs are idempotent — assets whose local size matches the manifest are skipped, so a scheduled sync stays fast in steady state.

## About the Apple API

The endpoints this tool uses (`p*-sharedstreams.icloud.com`) are the ones behind Apple's public web viewer at `www.icloud.com/sharedalbum/`. They are undocumented, unofficial, and can change or disappear on any iOS/macOS release. If Apple changes the shape of the response, this tool will break until it's updated to match.

If you rely on this in production, pin a specific version tag rather than tracking `:latest`.

## Running from source

Python 3.9 or newer, no packages to install:

```bash
export SHARED_ALBUM_URL='https://www.icloud.com/sharedalbum/#B2AJ...'
export OUTPUT_DIR="$PWD/photos"
python3 src/sync.py
```

## Building the image

If you'd rather build the image locally than pull from GHCR — for a private mirror, an air-gapped environment, or just because `v0.1.0` hasn't shipped yet — the Dockerfile is at the repo root and needs no build args:

```bash
git clone https://github.com/Bitwise-Forge/icloud-shared-album-sync
cd icloud-shared-album-sync
docker build -t icloud-shared-album-sync:local .
```

Then substitute `icloud-shared-album-sync:local` wherever the Quickstart and Compose examples show `ghcr.io/bitwise-forge/icloud-shared-album-sync:latest`.

The resulting image is `~145 MB`, based on `python:3.13-slim`, and runs as a non-root `app` user (UID 1000) inside the container. Multi-architecture builds (`linux/amd64` + `linux/arm64`) work via `docker buildx` and a `docker-container` driver — that's how the published GHCR image is produced.

## Testing

Test suite runs with `pytest`. Set up a virtualenv once, install the dev deps, then run:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

With a coverage report:

```bash
pytest --cov=sync --cov-report=term-missing
```

Every filesystem test uses pytest's `tmp_path` fixture; every network call is stubbed via `monkeypatch`. The suite never touches Apple's real API or writes files outside the temp dir.

Coverage groups:

- **Pure logic:** URL parsing, best-derivative selection (photo, video, edge cases), collision-proof local filename generation, the managed-file naming regex.
- **Shard resolution:** happy-path 200, 330 redirect via response header, missing-host error path.
- **End-to-end `sync_album`:** creates the output directory; downloads every asset at the manifest's declared size; skips unchanged files on re-run; re-downloads on size mismatch; prunes orphans that match the tool's naming pattern; leaves manual (non-matching) files alone; honours `PRUNE_REMOVED=false`; handles filename collisions across contributors; prunes assets removed from the album on the next sync; handles an empty manifest.

## What it doesn't do (yet)

- **Write contributor / caption / date sidecars.** The API exposes all three; a future release will write them alongside the media as JSON or XMP.
- **Handle private (non-public) Shared Albums.** Only works with albums that have the "Public Website" toggle enabled.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](./CONTRIBUTING.md) for setup, expectations, and the scope guide. Participation is governed by the [Code of Conduct](./CODE_OF_CONDUCT.md). Security issues should be reported privately per [SECURITY.md](./SECURITY.md).

Changes per release are tracked in [CHANGELOG.md](./CHANGELOG.md).

## License

MIT — see [LICENSE](./LICENSE).

## Support

This is a community-supported open source project. Issues and pull requests are welcome; there is no SLA and no obligation to fix. If you find a bug, open an issue with the log output and (if you can share it) the album URL that triggered it.

Built and maintained by [Bitwise Forge](https://bitwiseforge.com).

---

<sub>iCloud and Apple are trademarks of Apple Inc., registered in the U.S. and other countries. This project is not affiliated with, endorsed by, or sponsored by Apple Inc.</sub>
