"""HTTP transport for Apple's shared-streams API.

Owns the low-level talking-to-Apple concerns: shard resolution, the
330-redirect protocol, POST-request shaping, and streaming asset
downloads. Nothing here reasons about photo metadata or filesystem
layout — those live in `manifest` and `storage` respectively.
"""

import httpx

APPLE_SHARDS_HOST_TEMPLATE = "https://p{shard}-sharedstreams.icloud.com"
INITIAL_SHARD_PROBE = "23"

# Timeouts prevent indefinite hangs if Apple's endpoint stops responding
# mid-request. Downloads get a longer read window because large videos
# genuinely take time to stream.
_POST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=10.0, read=120.0)


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


def build_download_url(asset_item: dict, locations: dict) -> str:
    loc = locations[asset_item["url_location"]]
    return f"{loc['scheme']}://{asset_item['url_location']}{asset_item['url_path']}"


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
