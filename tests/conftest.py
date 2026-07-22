"""Shared fixtures and builders for the icloud_sync test suite.

Fixtures (`album`, `main_env`) are picked up automatically by pytest.
Plain builders (`_stream_fixture`, `_asset_urls_fixture`, `_MockPostJson`,
`_large_album_stream`, `_large_asset_urls`) are importable directly —
`from conftest import _stream_fixture` etc. — because pytest puts the
tests/ directory on sys.path during collection.
"""

import httpx
import pytest

from icloud_sync import apple_api, manifest, orchestrator


def _stream_fixture():
    """A small manifest: one photo (two derivatives) + one video (three)."""
    return {
        "streamName": "TestAlbum",
        "userFirstName": "Chris",
        "userLastName": "Human",
        "photos": [
            {
                "photoGuid": "GUID-PHOTO-001",
                "contributorFullName": "Chris Human",
                "caption": None,
                "batchDateCreated": "2026-07-18T21:00:00Z",
                "dateCreated": "2026-07-05T12:00:00Z",
                "derivatives": {
                    "342": {"checksum": "cksum-photo-small", "fileSize": "1000"},
                    "2048": {"checksum": "cksum-photo-large", "fileSize": "50000"},
                },
            },
            {
                "photoGuid": "GUID-VIDEO-001",
                "contributorFullName": "Kim Human",
                "caption": "birthday!",
                "batchDateCreated": "2026-07-18T21:00:00Z",
                "dateCreated": "2026-07-10T09:00:00Z",
                "derivatives": {
                    "360p": {"checksum": "cksum-video-small", "fileSize": "2000"},
                    "720p": {"checksum": "cksum-video-large", "fileSize": "100000"},
                    "PosterFrame": {"checksum": "cksum-poster", "fileSize": "500"},
                },
            },
        ],
    }


def _asset_urls_fixture():
    return {
        "items": {
            "cksum-photo-large": {
                "url_location": "cdn.example.com",
                "url_path": "/x/IMG_0001.JPG?sig=abc",
            },
            "cksum-video-large": {
                "url_location": "cdn.example.com",
                "url_path": "/x/IMG_0002.mp4?sig=def",
            },
        },
        "locations": {
            "cdn.example.com": {"scheme": "https"},
        },
    }


class _MockPostJson:
    """Route-aware fake for apple_api._post_json.

    Distinguishes shard-probe / webstream / webasseturls by URL substring
    and can simulate a 330 shard-redirect via constructor args. Returns
    httpx.Response instances so the code under test sees the same shape
    as a live httpx call.
    """

    def __init__(self, stream, asset_urls, shard_probe_status=200, shard_host=None):
        self.stream = stream
        self.asset_urls = asset_urls
        self.shard_probe_status = shard_probe_status
        self.shard_host = shard_host  # e.g. "p134-sharedstreams.icloud.com"
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        if "webasseturls" in url:
            # Mirror Apple's real behavior: empty photoGuids returns 400
            # "Validation Failed". Orchestrator must short-circuit before
            # this call when the album has no photos.
            if not payload.get("photoGuids"):
                return httpx.Response(400, text="Validation Failed: missing photoGuids")
            return httpx.Response(200, json=self.asset_urls)
        if self.shard_probe_status != 200 and f"/p{apple_api.INITIAL_SHARD_PROBE}-" in url:
            headers = {}
            if self.shard_host:
                headers["X-Apple-MMe-Host"] = self.shard_host
            return httpx.Response(self.shard_probe_status, headers=headers, json={})
        return httpx.Response(200, json=self.stream)


def _large_album_stream(sizes_by_dateCreated):
    """Build a stream where photo N has dateCreated = 2026-06-{01+N}. All
    share one batchDateCreated, so dateCreated determines sort order.
    Sizes (in bytes) come from the input mapping."""
    photos = []
    for i, size in enumerate(sizes_by_dateCreated):
        photos.append(
            {
                "photoGuid": f"GUID-{i:03d}",
                "contributorFullName": "Chris",
                "caption": None,
                "batchDateCreated": "2026-07-18T21:00:00Z",
                "dateCreated": f"2026-06-{i + 1:02d}T12:00:00Z",
                "derivatives": {
                    "2048": {"checksum": f"cksum-{i:03d}", "fileSize": str(size)},
                },
            }
        )
    return {
        "streamName": "Big",
        "userFirstName": "Chris",
        "userLastName": "Human",
        "photos": photos,
    }


def _large_asset_urls(n):
    return {
        "items": {
            f"cksum-{i:03d}": {
                "url_location": "cdn.example.com",
                "url_path": f"/x/IMG_{i:03d}.JPG?sig=x",
            }
            for i in range(n)
        },
        "locations": {"cdn.example.com": {"scheme": "https"}},
    }


@pytest.fixture
def album(tmp_path, monkeypatch):
    """Bundle stream, asset_urls, output dir, and a `run` callable into
    one fixture. Each test can override the stream or asset_urls on the
    fly via `album.run(stream=..., asset_urls=...)`."""

    class _Album:
        stream = _stream_fixture()
        asset_urls = _asset_urls_fixture()
        output_dir = str(tmp_path / "photos")

        def run(
            self,
            stream=None,
            asset_urls=None,
            buffer_percent=0.0,
            autoprune_on_low_storage=False,
        ):
            s = stream if stream is not None else self.stream
            a = asset_urls if asset_urls is not None else self.asset_urls

            fake_post = _MockPostJson(s, a)
            url_to_size = {}
            for photo in s["photos"]:
                best = manifest.best_derivative_key(photo["derivatives"])
                deriv = photo["derivatives"][best]
                item = a["items"][deriv["checksum"]]
                url = apple_api.build_download_url(item, a["locations"])
                url_to_size[url] = int(deriv["fileSize"])

            def fake_download(url, dest_path):
                size = url_to_size[url]
                with open(dest_path, "wb") as f:
                    f.write(b"\0" * size)
                return size

            monkeypatch.setattr(apple_api, "_post_json", fake_post)
            monkeypatch.setattr(apple_api, "download", fake_download)
            orchestrator.sync_album(
                "https://www.icloud.com/sharedalbum/#BTEST",
                self.output_dir,
                buffer_percent,
                autoprune_on_low_storage,
            )

    return _Album()


@pytest.fixture
def main_env(monkeypatch, tmp_path):
    """Baseline env for main() tests. Individual tests override as needed."""
    monkeypatch.setenv("SHARED_ALBUM_URL", "https://www.icloud.com/sharedalbum/#B2AJ")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("SYNC_INTERVAL_HOURS", "0")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.delenv("STORAGE_BUFFER_PERCENT", raising=False)
    monkeypatch.delenv("AUTOPRUNE_ON_LOW_STORAGE", raising=False)
    return monkeypatch
