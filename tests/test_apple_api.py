"""Tests for icloud_sync.apple_api — HTTP transport, shard resolution,
Apple's 330-redirect protocol, streaming asset download."""

import logging

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
        captured["timeout"] = kwargs.get("timeout")
        return httpx.Response(200, headers={"X-Foo": "bar"}, json={"result": "ok"})

    monkeypatch.setattr(apple_api.httpx, "post", fake_post)
    apple_api.configure(api_timeout_seconds=99.0)
    r = apple_api._post_json("https://example.com/foo", {"a": 1})

    assert r.status_code == 200
    assert r.headers.get("X-Foo") == "bar"
    assert r.json() == {"result": "ok"}
    # Verify the request was shaped the way Apple expects.
    assert captured["url"] == "https://example.com/foo"
    assert captured["json"] == {"a": 1}
    assert captured["headers"]["Content-Type"] == "text/plain"
    assert captured["headers"]["Origin"] == "https://www.icloud.com"
    # The configured ceiling reaches httpx, with connect kept separate.
    assert captured["timeout"].read == 99.0
    assert captured["timeout"].connect == apple_api.CONNECT_TIMEOUT_SECONDS


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


# ---------- timeout policy: configure / escalation / slow warning ----------


class _ScriptedSend:
    """Replaces apple_api._send so tests drive elapsed time and failures
    directly instead of doing real timing. Each script entry is either an
    exception to raise or a float 'elapsed' to report alongside a 200."""

    def __init__(self, *script):
        self.script = list(script)
        self.ceilings = []

    def __call__(self, url, payload, timeout_seconds):
        self.ceilings.append(timeout_seconds)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return httpx.Response(200, json={"ok": True}), step


def test_configure_sets_timeouts_and_clears_escalation():
    apple_api._escalated_keys.add(("example.com", "webstream"))
    apple_api.configure(api_timeout_seconds=42.0, download_timeout_seconds=7.0)
    assert apple_api._api_timeout_seconds == 42.0
    assert apple_api._download_timeout_seconds == 7.0
    assert apple_api._escalated_keys == set()


def test_configure_defaults_match_module_constants():
    apple_api.configure()
    assert apple_api._api_timeout_seconds == apple_api.DEFAULT_API_TIMEOUT_SECONDS
    assert apple_api._download_timeout_seconds == apple_api.DEFAULT_DOWNLOAD_TIMEOUT_SECONDS


def test_effective_timeout_doubles_only_while_escalated():
    apple_api.configure(api_timeout_seconds=100.0)
    key = ("p1-sharedstreams.icloud.com", "webstream")
    assert apple_api._effective_api_timeout(key) == 100.0
    apple_api._escalated_keys.add(key)
    assert apple_api._effective_api_timeout(key) == 100.0 * apple_api.TIMEOUT_ESCALATION_FACTOR


def test_escalation_key_splits_host_and_endpoint_without_the_token():
    key = apple_api._escalation_key(
        "https://p134-sharedstreams.icloud.com/SECRETTOKEN/sharedstreams/webasseturls"
    )
    assert key == ("p134-sharedstreams.icloud.com", "webasseturls")
    assert "SECRETTOKEN" not in str(key)


def test_fast_response_does_not_warn(monkeypatch, caplog):
    apple_api.configure(api_timeout_seconds=100.0)
    monkeypatch.setattr(apple_api, "_send", _ScriptedSend(10.0))
    caplog.set_level(logging.WARNING, logger="sync")
    apple_api._post_json("https://example.com/webstream", {})
    assert not [r for r in caplog.records if "slow Apple response" in r.message]


def test_response_past_half_the_ceiling_warns(monkeypatch, caplog):
    """The early-warning signal: a call that still succeeds but is eating
    more than SLOW_RESPONSE_WARN_RATIO of its budget should say so, so a
    growing album surfaces before it starts failing outright."""
    apple_api.configure(api_timeout_seconds=100.0)
    monkeypatch.setattr(apple_api, "_send", _ScriptedSend(60.0))
    caplog.set_level(logging.WARNING, logger="sync")
    apple_api._post_json("https://example.com/webstream", {})
    warnings = [r for r in caplog.records if "slow Apple response" in r.message]
    assert len(warnings) == 1
    assert "60.0s of a 100.0s ceiling (60%)" in warnings[0].getMessage()


def test_timeout_escalates_once_and_retries_at_double(monkeypatch, caplog):
    apple_api.configure(api_timeout_seconds=100.0)
    send = _ScriptedSend(httpx.ReadTimeout("too slow"), 150.0)
    monkeypatch.setattr(apple_api, "_send", send)
    caplog.set_level(logging.WARNING, logger="sync")

    r = apple_api._post_json("https://example.com/webstream", {})

    assert r.status_code == 200
    assert send.ceilings == [100.0, 200.0]  # base, then doubled
    assert apple_api._escalated_keys == {("example.com", "webstream")}
    assert any("retrying once at 200.0s" in rec.getMessage() for rec in caplog.records)


def test_escalation_is_sticky_across_calls(monkeypatch):
    """A manifest too slow for the base ceiling will be too slow next cycle
    too. The second call should start at the raised ceiling rather than
    re-paying the failed base attempt."""
    apple_api.configure(api_timeout_seconds=100.0)
    send = _ScriptedSend(httpx.ReadTimeout("too slow"), 150.0, 150.0)
    monkeypatch.setattr(apple_api, "_send", send)

    apple_api._post_json("https://example.com/webstream", {})
    apple_api._post_json("https://example.com/webstream", {})

    assert send.ceilings == [100.0, 200.0, 200.0]


def test_second_timeout_while_escalated_raises_without_a_third_attempt(monkeypatch, caplog):
    apple_api.configure(api_timeout_seconds=100.0)
    send = _ScriptedSend(
        httpx.ReadTimeout("too slow"),
        150.0,
        httpx.ReadTimeout("still too slow"),
    )
    monkeypatch.setattr(apple_api, "_send", send)
    caplog.set_level(logging.ERROR, logger="sync")

    apple_api._post_json("https://example.com/webstream", {})  # escalates
    with pytest.raises(httpx.ReadTimeout):
        apple_api._post_json("https://example.com/webstream", {})

    assert send.ceilings == [100.0, 200.0, 200.0]  # no fourth call
    assert any("giving up on this run" in rec.getMessage() for rec in caplog.records)


def test_escalated_retry_that_also_times_out_propagates(monkeypatch):
    """Both attempts inside a single _post_json time out — the caller sees
    the exception rather than an endless doubling ladder."""
    apple_api.configure(api_timeout_seconds=100.0)
    send = _ScriptedSend(httpx.ReadTimeout("a"), httpx.ReadTimeout("b"))
    monkeypatch.setattr(apple_api, "_send", send)

    with pytest.raises(httpx.ReadTimeout):
        apple_api._post_json("https://example.com/webstream", {})
    assert send.ceilings == [100.0, 200.0]


def test_escalation_clears_when_a_call_fits_the_base_ceiling(monkeypatch, caplog):
    apple_api.configure(api_timeout_seconds=100.0)
    send = _ScriptedSend(httpx.ReadTimeout("too slow"), 150.0, 20.0, 20.0)
    monkeypatch.setattr(apple_api, "_send", send)
    caplog.set_level(logging.INFO, logger="sync")

    apple_api._post_json("https://example.com/webstream", {})  # escalates
    apple_api._post_json("https://example.com/webstream", {})  # 20s — fits base
    assert apple_api._escalated_keys == set()
    assert any("escalation cleared" in rec.getMessage() for rec in caplog.records)

    apple_api._post_json("https://example.com/webstream", {})
    assert send.ceilings == [100.0, 200.0, 200.0, 100.0]  # back to base


def test_escalation_persists_when_call_still_exceeds_base(monkeypatch):
    """Succeeding at 150s under a 200s escalated ceiling does not prove the
    100s base would have worked, so escalation must stay on."""
    apple_api.configure(api_timeout_seconds=100.0)
    send = _ScriptedSend(httpx.ReadTimeout("too slow"), 150.0, 150.0)
    monkeypatch.setattr(apple_api, "_send", send)

    apple_api._post_json("https://example.com/webstream", {})
    apple_api._post_json("https://example.com/webstream", {})
    assert apple_api._escalated_keys == {("example.com", "webstream")}


def test_escalated_retry_warns_when_slow_against_raised_ceiling(monkeypatch, caplog):
    apple_api.configure(api_timeout_seconds=100.0)
    monkeypatch.setattr(apple_api, "_send", _ScriptedSend(httpx.ReadTimeout("too slow"), 180.0))
    caplog.set_level(logging.WARNING, logger="sync")
    apple_api._post_json("https://example.com/webstream", {})
    assert any("180.0s of a 200.0s ceiling" in r.getMessage() for r in caplog.records)


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


def test_download_uses_configured_download_timeout(tmp_path, monkeypatch):
    captured = {}

    def fake_stream(_method, _url, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _FakeStreamCM(b"x")

    monkeypatch.setattr(apple_api.httpx, "stream", fake_stream)
    apple_api.configure(download_timeout_seconds=45.0)
    apple_api.download("https://example.com/x", str(tmp_path / "out.bin"))

    assert captured["timeout"].read == 45.0
    assert captured["timeout"].connect == apple_api.CONNECT_TIMEOUT_SECONDS


def test_a_fast_endpoint_does_not_clear_another_endpoints_escalation(monkeypatch):
    """Regression guard for a bug caught against the live API: escalation
    used to be one global flag, so `webasseturls` (measured 6.5s on a
    210-asset album) cleared the escalation `webstream` (32.4s) needed.
    The next cycle then timed out and re-escalated — thrashing forever
    instead of sticking. State is per (host, endpoint) for this reason."""
    apple_api.configure(api_timeout_seconds=100.0)
    send = _ScriptedSend(
        httpx.ReadTimeout("webstream too slow"),  # webstream at base
        150.0,  # webstream escalated retry
        6.5,  # webasseturls, comfortably inside base
        150.0,  # webstream again — must still be escalated
    )
    monkeypatch.setattr(apple_api, "_send", send)

    apple_api._post_json("https://p1.icloud.com/T/sharedstreams/webstream", {})
    apple_api._post_json("https://p1.icloud.com/T/sharedstreams/webasseturls", {})
    apple_api._post_json("https://p1.icloud.com/T/sharedstreams/webstream", {})

    # 100 base, 200 escalated, 100 for the untouched webasseturls, 200 again.
    assert send.ceilings == [100.0, 200.0, 100.0, 200.0]
    assert apple_api._escalated_keys == {("p1.icloud.com", "webstream")}


def test_shard_probe_does_not_clear_the_resolved_shards_escalation(monkeypatch):
    """Same hazard via a different route: the p23 probe answers its 330 in
    ~0.8s regardless of album size, so keying on endpoint name alone would
    let it clear the real shard's escalation. Host is part of the key."""
    apple_api.configure(api_timeout_seconds=100.0)
    send = _ScriptedSend(
        httpx.ReadTimeout("real shard too slow"),
        150.0,
        0.8,  # p23 probe, fast 330
        150.0,
    )
    monkeypatch.setattr(apple_api, "_send", send)

    real = "https://p134-sharedstreams.icloud.com/T/sharedstreams/webstream"
    probe = "https://p23-sharedstreams.icloud.com/T/sharedstreams/webstream"
    apple_api._post_json(real, {})
    apple_api._post_json(probe, {})
    apple_api._post_json(real, {})

    assert send.ceilings == [100.0, 200.0, 100.0, 200.0]
    assert apple_api._escalated_keys == {("p134-sharedstreams.icloud.com", "webstream")}
