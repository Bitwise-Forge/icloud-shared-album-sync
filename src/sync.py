#!/usr/bin/env python3
"""
icloud-shared-album-sync

Pull photos and videos from a public iCloud Shared Album URL to a folder.
Idempotent by filename + declared file size.
"""

import hashlib
import logging
import os
import re
import signal
import sys
import threading
from urllib.parse import urlparse

import httpx

APPLE_SHARDS_HOST_TEMPLATE = "https://p{shard}-sharedstreams.icloud.com"
INITIAL_SHARD_PROBE = "23"

# Local filenames get an 8-hex suffix derived from photoGuid — makes them
# collision-proof across contributors and marks them as ours for pruning.
# Extension optional: local_filename() drops the extension when the source
# filename has none, and the regex has to still match what we produced.
_MANAGED_NAME_RE = re.compile(r"__[0-9a-f]{8}(?:\.[^./]+)?$")

# Timeouts prevent indefinite hangs if Apple's endpoint stops responding
# mid-request. Downloads get a longer read window because large videos
# genuinely take time to stream.
#
# No transport-level retries: httpx's top-level convenience functions
# don't accept `transport=`, and the daemon loop's SYNC_INTERVAL_HOURS
# already retries the whole sync on transient failures. If we ever want
# smarter retries (backoff, per-URL policies), reach for tenacity.
_POST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=10.0, read=120.0)

log = logging.getLogger("sync")


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        force=True,
    )
    # Silence httpx's per-request INFO chatter. Two reasons: it's noisy,
    # and it logs signed CDN URLs that stay valid for ~3 hours — bad thing
    # to leak into shared logs. Users who want the detail can override.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def extract_token(url: str) -> str:
    fragment = urlparse(url).fragment
    if not fragment or not fragment.startswith("B"):
        raise ValueError(f"URL does not look like an iCloud shared album URL: {url}")
    return fragment


def _post_json(url: str, payload: dict) -> httpx.Response:
    # Apple's endpoint expects Content-Type: text/plain even though the body
    # is JSON, and Origin: https://www.icloud.com to mimic the web viewer.
    # httpx doesn't follow 3xx on POST by default, so Apple's 330 shard-
    # redirect comes back as a normal Response we can inspect.
    return httpx.post(
        url,
        json=payload,
        headers={
            "Content-Type": "text/plain",
            "Origin": "https://www.icloud.com",
        },
        timeout=_POST_TIMEOUT,
    )


def resolve_shard(token: str) -> str:
    probe_url = (
        APPLE_SHARDS_HOST_TEMPLATE.format(shard=INITIAL_SHARD_PROBE)
        + f"/{token}/sharedstreams/webstream"
    )
    r = _post_json(probe_url, {"streamCtag": None})
    if r.status_code == 200:
        return INITIAL_SHARD_PROBE
    host = r.headers.get("X-Apple-MMe-Host") or r.json().get("X-Apple-MMe-Host")
    if not host:
        raise RuntimeError(
            f"Unexpected shard-resolve response: status={r.status_code} "
            f"headers={dict(r.headers)} body={r.text}"
        )
    return host.split("-")[0][1:]


def fetch_stream(shard: str, token: str) -> dict:
    url = APPLE_SHARDS_HOST_TEMPLATE.format(shard=shard) + f"/{token}/sharedstreams/webstream"
    r = _post_json(url, {"streamCtag": None})
    if r.status_code != 200:
        raise RuntimeError(f"webstream failed: status={r.status_code} body={r.text}")
    return r.json()


def fetch_asset_urls(shard: str, token: str, photo_guids: list) -> dict:
    url = APPLE_SHARDS_HOST_TEMPLATE.format(shard=shard) + f"/{token}/sharedstreams/webasseturls"
    r = _post_json(url, {"photoGuids": photo_guids})
    if r.status_code != 200:
        raise RuntimeError(f"webasseturls failed: status={r.status_code} body={r.text}")
    return r.json()


def best_derivative_key(derivatives: dict) -> str:
    keys = list(derivatives.keys())
    # Videos expose derivatives named like "720p", "360p", "PosterFrame".
    video_keys = [k for k in keys if k.endswith("p")]
    if video_keys:
        if "720p" in keys:
            return "720p"
        if "360p" in keys:
            return "360p"
        return video_keys[0]
    # Photo derivatives are numeric strings (long edge in pixels); pick the max.
    numeric = [k for k in keys if k.isdigit()]
    if numeric:
        return max(numeric, key=int)
    raise ValueError(f"Unrecognized derivative shape for asset: {keys}")


def build_download_url(asset_item: dict, locations: dict) -> str:
    loc = locations[asset_item["url_location"]]
    return f"{loc['scheme']}://{asset_item['url_location']}{asset_item['url_path']}"


def local_filename(photo_guid: str, apple_filename: str) -> str:
    base, ext = os.path.splitext(apple_filename)
    suffix = hashlib.sha1(photo_guid.encode()).hexdigest()[:8]
    return f"{base}__{suffix}{ext}"


def _prune_removed(output_dir: str, expected_names: set) -> int:
    removed = 0
    for entry in os.listdir(output_dir):
        path = os.path.join(output_dir, entry)
        if not os.path.isfile(path):
            continue
        if not _MANAGED_NAME_RE.search(entry):
            continue
        if entry in expected_names:
            continue
        os.remove(path)
        removed += 1
        log.info("prune %s", entry)
    return removed


def download(url: str, dest_path: str) -> int:
    # Stream to disk so memory doesn't scale with asset size. follow_redirects
    # is on because Apple's CDN can return redirects on signed URLs.
    total = 0
    with (
        httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=_DOWNLOAD_TIMEOUT,
        ) as r,
        open(dest_path, "wb") as f,
    ):
        r.raise_for_status()
        for chunk in r.iter_bytes(chunk_size=64 * 1024):
            f.write(chunk)
            total += len(chunk)
    return total


def sync_album(url: str, output_dir: str, prune: bool = True) -> None:
    token = extract_token(url)
    shard = resolve_shard(token)
    stream = fetch_stream(shard, token)

    album_name = stream.get("streamName", "(unnamed)")
    owner = f"{stream.get('userFirstName', '?')} {stream.get('userLastName', '?')}"
    log.info("album=%r owner=%r assets=%d", album_name, owner, len(stream["photos"]))

    os.makedirs(output_dir, exist_ok=True)

    photo_guids = [p["photoGuid"] for p in stream["photos"]]
    asset_response = fetch_asset_urls(shard, token, photo_guids)
    items = asset_response["items"]
    locations = asset_response["locations"]

    downloaded = 0
    skipped = 0
    expected_names = set()
    for photo in stream["photos"]:
        best_key = best_derivative_key(photo["derivatives"])
        deriv = photo["derivatives"][best_key]
        item = items[deriv["checksum"]]
        download_url = build_download_url(item, locations)
        apple_filename = item["url_path"].split("/")[-1].split("?")[0]
        filename = local_filename(photo["photoGuid"], apple_filename)
        expected_names.add(filename)
        dest = os.path.join(output_dir, filename)

        expected_size = int(deriv.get("fileSize", 0))
        if os.path.exists(dest) and expected_size and os.path.getsize(dest) == expected_size:
            log.debug("skip %s (already %d bytes)", filename, expected_size)
            skipped += 1
            continue

        bytes_written = download(download_url, dest)
        downloaded += 1
        contrib = photo.get("contributorFullName", "?")
        caption = photo.get("caption") or ""
        log.info(
            "pull %s (%s, %s bytes) by %s%s",
            filename,
            best_key,
            f"{bytes_written:,}",
            contrib,
            f" — {caption!r}" if caption else "",
        )

    pruned = _prune_removed(output_dir, expected_names) if prune else 0
    log.info(
        "done downloaded=%d skipped=%d pruned=%d output=%s",
        downloaded,
        skipped,
        pruned,
        output_dir,
    )


def _env(name: str, default=None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        log.error("missing required env var: %s", name)
        sys.exit(2)
    return val


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def _handler(signum, _frame):
        log.info("received signal %d, will stop after current run", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main() -> int:
    _configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

    url = _env("SHARED_ALBUM_URL", required=True)
    output_dir = _env("OUTPUT_DIR", "/photos")
    interval_raw = _env("SYNC_INTERVAL_HOURS", "0")
    prune = _env("PRUNE_REMOVED", "true").lower() != "false"
    try:
        interval_hours = float(interval_raw)
    except ValueError:
        log.error("SYNC_INTERVAL_HOURS must be numeric, got %r", interval_raw)
        return 2

    stop = threading.Event()
    _install_signal_handlers(stop)

    while not stop.is_set():
        try:
            sync_album(url, output_dir, prune=prune)
        except Exception:
            log.exception("sync failed")
        if interval_hours <= 0:
            break
        interval_seconds = interval_hours * 3600
        log.info("sleeping %.2fh until next sync", interval_hours)
        if stop.wait(interval_seconds):
            log.info("stop requested during sleep, exiting")
            break

    return 0


if __name__ == "__main__":
    sys.exit(main())
