"""Tests for icloud_sync.apple_api — HTTP transport, shard resolution,
Apple's 330-redirect protocol, streaming asset download."""

import httpx
import pytest
from conftest import _asset_urls_fixture, _MockPostJson, _stream_fixture

from icloud_sync import apple_api

# ---------- build_download_url ---------------------------------------------


def test_build_download_url_assembles_https_url():
    item = {"url_location": "cdn.example.com", "url_path": "/x/foo.jpg?sig=1"}
    locations = {"cdn.example.com": {"scheme": "https"}}
    assert (
        apple_api.build_download_url(item, locations) == "https://cdn.example.com/x/foo.jpg?sig=1"
    )


# ---------- resolve_shard --------------------------------------------------


def test_resolve_shard_returns_initial_probe_on_200(monkeypatch):
    fake = _MockPostJson(_stream_fixture(), _asset_urls_fixture())
    monkeypatch.setattr(apple_api, "_post_json", fake)
    assert apple_api.resolve_shard("TOKEN123") == apple_api.INITIAL_SHARD_PROBE


def test_resolve_shard_follows_330_via_header(monkeypatch):
    fake = _MockPostJson(
        _stream_fixture(),
        _asset_urls_fixture(),
        shard_probe_status=330,
        shard_host="p134-sharedstreams.icloud.com",
    )
    monkeypatch.setattr(apple_api, "_post_json", fake)
    assert apple_api.resolve_shard("TOKEN123") == "134"


def test_resolve_shard_raises_when_no_host_supplied(monkeypatch):
    fake = _MockPostJson(
        _stream_fixture(),
        _asset_urls_fixture(),
        shard_probe_status=330,
        shard_host=None,
    )
    monkeypatch.setattr(apple_api, "_post_json", fake)
    with pytest.raises(RuntimeError):
        apple_api.resolve_shard("TOKEN123")


# ---------- _post_json -----------------------------------------------------


def test_post_json_sends_expected_request_and_returns_response(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers", {})
        return httpx.Response(200, headers={"X-Foo": "bar"}, json={"result": "ok"})

    monkeypatch.setattr(apple_api.httpx, "post", fake_post)
    r = apple_api._post_json("https://example.com/foo", {"a": 1})

    assert r.status_code == 200
    assert r.headers.get("X-Foo") == "bar"
    assert r.json() == {"result": "ok"}
    # Verify the request was shaped the way Apple expects.
    assert captured["url"] == "https://example.com/foo"
    assert captured["json"] == {"a": 1}
    assert captured["headers"]["Content-Type"] == "text/plain"
    assert captured["headers"]["Origin"] == "https://www.icloud.com"


def test_post_json_returns_non_2xx_response_without_raising(monkeypatch):
    """Apple's shard-redirect returns 330 on POST. httpx doesn't follow
    redirects on POST by default, so this comes back as a normal Response
    that _post_json returns as-is — no exception dance required."""

    def fake_post(_url, **_kwargs):
        return httpx.Response(
            330,
            headers={"X-Apple-MMe-Host": "p42-sharedstreams.icloud.com"},
            json={"X-Apple-MMe-Host": "p42-sharedstreams.icloud.com"},
        )

    monkeypatch.setattr(apple_api.httpx, "post", fake_post)
    r = apple_api._post_json("https://example.com", {})

    assert r.status_code == 330
    assert r.headers.get("X-Apple-MMe-Host") == "p42-sharedstreams.icloud.com"
    assert r.json()["X-Apple-MMe-Host"] == "p42-sharedstreams.icloud.com"


# ---------- fetch_stream / fetch_asset_urls error paths --------------------


def test_fetch_stream_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(
        apple_api, "_post_json", lambda _u, _p: httpx.Response(500, json={"e": "x"})
    )
    with pytest.raises(RuntimeError, match="webstream failed"):
        apple_api.fetch_stream("23", "TOKEN")


def test_fetch_asset_urls_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(
        apple_api, "_post_json", lambda _u, _p: httpx.Response(500, json={"e": "x"})
    )
    with pytest.raises(RuntimeError, match="webasseturls failed"):
        apple_api.fetch_asset_urls("23", "TOKEN", ["guid1"])


# ---------- download -------------------------------------------------------


class _FakeStreamResponse:
    """Stand-in for the response object httpx.stream() yields as a
    context manager. iter_bytes chunks the payload the way httpx does
    so download()'s streaming loop is genuinely exercised."""

    def __init__(self, payload, chunk_size=16 * 1024):
        self._payload = payload
        self._chunk_size = chunk_size

    def raise_for_status(self):
        return None

    def iter_bytes(self, chunk_size=None):
        step = chunk_size or self._chunk_size
        for i in range(0, len(self._payload), step):
            yield self._payload[i : i + step]


class _FakeStreamCM:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return _FakeStreamResponse(self._payload)

    def __exit__(self, *_args):
        return None


def test_download_streams_response_bytes_to_disk(tmp_path, monkeypatch):
    # Payload larger than the chunk size — proves we actually iterate.
    payload = b"hello world" * 10_000

    def fake_stream(method, url, **_kwargs):
        assert method == "GET"
        assert url == "https://example.com/x"
        return _FakeStreamCM(payload)

    monkeypatch.setattr(apple_api.httpx, "stream", fake_stream)
    dest = str(tmp_path / "out.bin")
    written = apple_api.download("https://example.com/x", dest)

    assert written == len(payload)
    with open(dest, "rb") as f:
        assert f.read() == payload
