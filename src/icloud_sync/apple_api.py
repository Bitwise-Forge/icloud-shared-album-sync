"""HTTP transport for Apple's shared-streams API.

Owns the low-level talking-to-Apple concerns: shard resolution, the
330-redirect protocol, POST-request shaping, timeout policy, and
streaming asset downloads. Nothing here reasons about photo metadata or
filesystem layout — those live in `manifest` and `storage` respectively.

Timeout policy exists because Apple's `webstream` and `webasseturls`
latency scales with the album's *asset count*, not its byte volume. A
measured 210-asset album answers `webstream` in ~32 seconds; a 103-asset
album in ~17. Albums grow, so any fixed ceiling eventually gets crossed,
and when it is, the failure is total and silent: the run aborts, the
daemon sleeps, and the local cache keeps serving stale content.

Two mechanisms guard that:

  * A generous configurable base timeout (`APPLE_API_TIMEOUT_SECONDS`).
  * One-shot escalation — if a call times out, the ceiling is doubled
    and the call is retried exactly once. Escalation is *sticky* for the
    life of the process, because a manifest too slow for the base ceiling
    will be too slow on every subsequent cycle too; paying the failed
    base attempt once is right, paying it hourly is not. It resets
    automatically when a call completes inside the base ceiling again.

Escalation is tracked per (host, endpoint) rather than globally because
these endpoints have wildly different latency profiles against the same
album — measured on a 210-asset album: the p23 shard probe returns its
330 redirect in 0.8s, `webstream` on the resolved shard takes 32s, and
`webasseturls` takes 6.5s. Shared state would let either fast call clear
the escalation `webstream` depends on, and the next cycle would time out
and escalate all over again — thrashing instead of sticking.

Note that escalation is deliberately not a general-purpose retry. It
fires only on `httpx.TimeoutException`, because that is the one failure
where trying again with a bigger budget is a genuinely different attempt
rather than the same request re-sent in hope.
"""

import logging
import time

import httpx

log = logging.getLogger("sync")

APPLE_SHARDS_HOST_TEMPLATE = "https://p{shard}-sharedstreams.icloud.com"
INITIAL_SHARD_PROBE = "23"

# Connect is separate from read: establishing a TCP+TLS session to Apple
# either works quickly or is not going to work at all, so it stays tight
# regardless of how long we're willing to wait for the response body.
CONNECT_TIMEOUT_SECONDS = 10.0

DEFAULT_API_TIMEOUT_SECONDS = 180.0
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 120.0

# Multiplier applied to the base ceiling on the one escalated retry.
TIMEOUT_ESCALATION_FACTOR = 2.0

# Emit a warning once a response consumes this share of its ceiling. The
# point is lead time: an album creeping toward the limit should show up in
# the logs while it still succeeds, not after it starts failing.
SLOW_RESPONSE_WARN_RATIO = 0.5

_api_timeout_seconds = DEFAULT_API_TIMEOUT_SECONDS
_download_timeout_seconds = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
_escalated_keys: set[tuple[str, str]] = set()


def configure(
    api_timeout_seconds: float = DEFAULT_API_TIMEOUT_SECONDS,
    download_timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
) -> None:
    """Install timeout policy for this process and clear any escalation.

    Called once from `cli.main` after env validation. Kept as module
    state rather than threaded through every call signature so that
    `cli` stays the single place that reads and validates env vars.
    """
    global _api_timeout_seconds, _download_timeout_seconds
    _api_timeout_seconds = api_timeout_seconds
    _download_timeout_seconds = download_timeout_seconds
    _escalated_keys.clear()


def _escalation_key(url: str) -> tuple[str, str]:
    """(host, endpoint) — deliberately excludes the album token so no
    credential-ish material ends up held in a second place."""
    parsed = httpx.URL(url)
    return (parsed.host, parsed.path.rsplit("/", 1)[-1])


def _effective_api_timeout(key: tuple[str, str]) -> float:
    if key in _escalated_keys:
        return _api_timeout_seconds * TIMEOUT_ESCALATION_FACTOR
    return _api_timeout_seconds


def _warn_if_slow(url: str, elapsed: float, ceiling: float) -> None:
    if elapsed < ceiling * SLOW_RESPONSE_WARN_RATIO:
        return
    log.warning(
        "slow Apple response: %.1fs of a %.1fs ceiling (%.0f%%) for %s — "
        "album growth pushes this toward a timeout; consider raising "
        "APPLE_API_TIMEOUT_SECONDS",
        elapsed,
        ceiling,
        elapsed / ceiling * 100,
        url.rsplit("/", 1)[-1],
    )


def _send(url: str, payload: dict, timeout_seconds: float) -> tuple[httpx.Response, float]:
    # Apple's endpoint expects Content-Type: text/plain even though the body
    # is JSON, and Origin: https://www.icloud.com to mimic the web viewer.
    # httpx doesn't follow 3xx on POST by default, so Apple's 330 shard-
    # redirect comes back as a normal Response we can inspect.
    started = time.monotonic()
    r = httpx.post(
        url,
        json=payload,
        headers={
            "Content-Type": "text/plain",
            "Origin": "https://www.icloud.com",
        },
        timeout=httpx.Timeout(timeout_seconds, connect=CONNECT_TIMEOUT_SECONDS),
    )
    return r, time.monotonic() - started


def _post_json(url: str, payload: dict) -> httpx.Response:
    key = _escalation_key(url)
    ceiling = _effective_api_timeout(key)
    try:
        r, elapsed = _send(url, payload, ceiling)
    except httpx.TimeoutException:
        if key in _escalated_keys:
            # Already running at the raised ceiling. One escalation only —
            # a second doubling would just extend a stall we've now seen
            # twice, and the daemon's failure backoff is the better tool.
            log.error(
                "Apple %s timed out at the escalated %.1fs ceiling; giving up on this run",
                key[1],
                ceiling,
            )
            raise
        _escalated_keys.add(key)
        escalated_ceiling = _effective_api_timeout(key)
        log.warning(
            "Apple %s timed out at %.1fs; retrying once at %.1fs "
            "(escalation stays active for this endpoint until a call fits the base ceiling)",
            key[1],
            ceiling,
            escalated_ceiling,
        )
        r, elapsed = _send(url, payload, escalated_ceiling)
        _warn_if_slow(url, elapsed, escalated_ceiling)
        return r

    _warn_if_slow(url, elapsed, ceiling)
    if key in _escalated_keys and elapsed < _api_timeout_seconds:
        # Conditions recovered — the base ceiling would have been enough
        # for this endpoint, so stop paying for the doubled one.
        _escalated_keys.discard(key)
        log.info(
            "Apple %s back inside the %.1fs base ceiling (%.1fs); escalation cleared",
            key[1],
            _api_timeout_seconds,
            elapsed,
        )
    return r


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
            timeout=httpx.Timeout(
                _download_timeout_seconds,
                connect=CONNECT_TIMEOUT_SECONDS,
                read=_download_timeout_seconds,
            ),
        ) as r,
        open(dest_path, "wb") as f,
    ):
        r.raise_for_status()
        for chunk in r.iter_bytes(chunk_size=64 * 1024):
            f.write(chunk)
            total += len(chunk)
    return total
