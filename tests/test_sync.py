"""Tests for src/sync.py.

Two layers:

- Pure-logic units for token parsing, derivative selection, filename
  generation, and the managed-name regex — collapsed into parametrized
  tests where the cases are structurally identical.
- End-to-end sync_album() runs with sync._post_json and sync.download
  mocked via monkeypatch. Every filesystem test uses tmp_path — the suite
  never touches Apple's real API or writes files outside the temp dir.

Run from the repo root: `pytest`
"""

import io
import logging
import os
import re
import threading
import time
import urllib.error

import pytest

import sync


# ---------- fixture data (plain builders, not pytest fixtures) -------------

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
                "derivatives": {
                    "342":  {"checksum": "cksum-photo-small", "fileSize": "1000"},
                    "2048": {"checksum": "cksum-photo-large", "fileSize": "50000"},
                },
            },
            {
                "photoGuid": "GUID-VIDEO-001",
                "contributorFullName": "Kim Human",
                "caption": "birthday!",
                "derivatives": {
                    "360p":        {"checksum": "cksum-video-small", "fileSize": "2000"},
                    "720p":        {"checksum": "cksum-video-large", "fileSize": "100000"},
                    "PosterFrame": {"checksum": "cksum-poster",      "fileSize": "500"},
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
    """Route-aware fake for sync._post_json.

    Distinguishes shard-probe / webstream / webasseturls by URL substring
    and can simulate a 330 shard-redirect via constructor args.
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
            return 200, {}, self.asset_urls
        if (
            self.shard_probe_status != 200
            and f"/p{sync.INITIAL_SHARD_PROBE}-" in url
        ):
            headers = {}
            if self.shard_host:
                headers["X-Apple-MMe-Host"] = self.shard_host
            return self.shard_probe_status, headers, {}
        return 200, {}, self.stream


# ---------- extract_token --------------------------------------------------

def test_extract_token_valid():
    assert (
        sync.extract_token("https://www.icloud.com/sharedalbum/#B2AJ0DiRHGf731D")
        == "B2AJ0DiRHGf731D"
    )


@pytest.mark.parametrize("url", [
    "https://www.icloud.com/sharedalbum/",
    "https://www.icloud.com/sharedalbum/#XYZ",
    "https://www.icloud.com/sharedalbum/#",
])
def test_extract_token_rejects_invalid(url):
    with pytest.raises(ValueError):
        sync.extract_token(url)


# ---------- best_derivative_key --------------------------------------------

@pytest.mark.parametrize("derivatives,expected", [
    ({"342": {}, "1024": {}, "2048": {}}, "2048"),
    ({"342": {}}, "342"),
    ({"360p": {}, "720p": {}, "PosterFrame": {}}, "720p"),
    ({"360p": {}, "PosterFrame": {}}, "360p"),
    ({"480p": {}, "PosterFrame": {}}, "480p"),
])
def test_best_derivative_key(derivatives, expected):
    assert sync.best_derivative_key(derivatives) == expected


def test_best_derivative_key_unrecognized_shape_raises():
    with pytest.raises(ValueError):
        sync.best_derivative_key({"PosterFrame": {}, "Thumbnail": {}})


# ---------- local_filename -------------------------------------------------

def test_local_filename_is_deterministic():
    assert (
        sync.local_filename("GUID-X", "IMG_5744.JPG")
        == sync.local_filename("GUID-X", "IMG_5744.JPG")
    )


def test_local_filename_differs_by_guid():
    assert (
        sync.local_filename("GUID-A", "IMG_5744.JPG")
        != sync.local_filename("GUID-B", "IMG_5744.JPG")
    )


def test_local_filename_differs_by_source_name():
    assert (
        sync.local_filename("GUID-X", "IMG_5744.JPG")
        != sync.local_filename("GUID-X", "IMG_5745.JPG")
    )


@pytest.mark.parametrize("src,expected_ext", [
    ("IMG.JPG", ".JPG"),
    ("clip.mp4", ".mp4"),
    ("archive.tar.gz", ".gz"),
])
def test_local_filename_preserves_extension(src, expected_ext):
    assert sync.local_filename("GUID-X", src).endswith(expected_ext)


def test_local_filename_no_extension_leaves_none():
    name = sync.local_filename("GUID-X", "raw")
    assert re.match(r"^raw__[0-9a-f]{8}$", name)


def test_local_filename_multi_dot_splits_at_last_dot():
    name = sync.local_filename("GUID-X", "archive.tar.gz")
    assert name.startswith("archive.tar__")
    assert name.endswith(".gz")


@pytest.mark.parametrize("src", ["IMG.JPG", "clip.mp4", "raw", "foo.tar.gz"])
def test_local_filename_output_matches_managed_pattern(src):
    # Round-trip: every filename this function produces must be recognized
    # as ours by the pruning regex — otherwise we'd fail to clean up our own
    # downloads.
    assert sync._MANAGED_NAME_RE.search(sync.local_filename("GUID-X", src))


# ---------- managed-name regex ---------------------------------------------

@pytest.mark.parametrize("name,expected", [
    # Matches
    ("IMG_5744__a1b2c3d4.JPG", True),
    ("clip__deadbeef.mp4", True),
    ("archive__a1b2c3d4.gz", True),
    ("raw__a1b2c3d4", True),          # no-extension case
    # Rejections
    ("IMG_5744.JPG", False),           # no suffix
    ("IMG_5744__abc.JPG", False),      # hash too short
    ("IMG_5744__a1b2c3d4e5.JPG", False),  # hash too long
    ("IMG_5744__A1B2C3D4.JPG", False),  # uppercase hex — hashlib never emits this
])
def test_managed_name_regex(name, expected):
    assert bool(sync._MANAGED_NAME_RE.search(name)) is expected


# ---------- build_download_url ---------------------------------------------

def test_build_download_url_assembles_https_url():
    item = {"url_location": "cdn.example.com", "url_path": "/x/foo.jpg?sig=1"}
    locations = {"cdn.example.com": {"scheme": "https"}}
    assert (
        sync.build_download_url(item, locations)
        == "https://cdn.example.com/x/foo.jpg?sig=1"
    )


# ---------- resolve_shard --------------------------------------------------

def test_resolve_shard_returns_initial_probe_on_200(monkeypatch):
    fake = _MockPostJson(_stream_fixture(), _asset_urls_fixture())
    monkeypatch.setattr(sync, "_post_json", fake)
    assert sync.resolve_shard("TOKEN123") == sync.INITIAL_SHARD_PROBE


def test_resolve_shard_follows_330_via_header(monkeypatch):
    fake = _MockPostJson(
        _stream_fixture(), _asset_urls_fixture(),
        shard_probe_status=330,
        shard_host="p134-sharedstreams.icloud.com",
    )
    monkeypatch.setattr(sync, "_post_json", fake)
    assert sync.resolve_shard("TOKEN123") == "134"


def test_resolve_shard_raises_when_no_host_supplied(monkeypatch):
    fake = _MockPostJson(
        _stream_fixture(), _asset_urls_fixture(),
        shard_probe_status=330, shard_host=None,
    )
    monkeypatch.setattr(sync, "_post_json", fake)
    with pytest.raises(RuntimeError):
        sync.resolve_shard("TOKEN123")


# ---------- sync_album end-to-end ------------------------------------------

@pytest.fixture
def album(tmp_path, monkeypatch):
    """Bundle stream, asset_urls, output dir, and a `run` callable into
    one fixture. Each test can override the stream or asset_urls on the
    fly via `album.run(stream=..., asset_urls=...)`."""

    class _Album:
        stream = _stream_fixture()
        asset_urls = _asset_urls_fixture()
        output_dir = str(tmp_path / "photos")

        def run(self, prune=True, stream=None, asset_urls=None):
            s = stream if stream is not None else self.stream
            a = asset_urls if asset_urls is not None else self.asset_urls

            fake_post = _MockPostJson(s, a)
            url_to_size = {}
            for photo in s["photos"]:
                best = sync.best_derivative_key(photo["derivatives"])
                deriv = photo["derivatives"][best]
                item = a["items"][deriv["checksum"]]
                url = sync.build_download_url(item, a["locations"])
                url_to_size[url] = int(deriv["fileSize"])

            def fake_download(url, dest_path):
                size = url_to_size[url]
                with open(dest_path, "wb") as f:
                    f.write(b"\0" * size)
                return size

            monkeypatch.setattr(sync, "_post_json", fake_post)
            monkeypatch.setattr(sync, "download", fake_download)
            sync.sync_album(
                "https://www.icloud.com/sharedalbum/#BTEST",
                self.output_dir,
                prune=prune,
            )

    return _Album()


def test_sync_creates_output_dir(album):
    assert not os.path.isdir(album.output_dir)
    album.run()
    assert os.path.isdir(album.output_dir)


def test_sync_downloads_all_manifest_assets(album):
    album.run()
    assert len(os.listdir(album.output_dir)) == 2


def test_sync_downloaded_files_match_managed_pattern(album):
    album.run()
    for name in os.listdir(album.output_dir):
        assert sync._MANAGED_NAME_RE.search(name)


def test_sync_downloaded_file_sizes_match_manifest(album):
    album.run()
    for photo in album.stream["photos"]:
        best = sync.best_derivative_key(photo["derivatives"])
        deriv = photo["derivatives"][best]
        item = album.asset_urls["items"][deriv["checksum"]]
        apple_name = item["url_path"].split("/")[-1].split("?")[0]
        local_name = sync.local_filename(photo["photoGuid"], apple_name)
        actual = os.path.getsize(os.path.join(album.output_dir, local_name))
        assert actual == int(deriv["fileSize"])


def test_sync_is_idempotent(album):
    album.run()
    before = {
        n: os.path.getmtime(os.path.join(album.output_dir, n))
        for n in os.listdir(album.output_dir)
    }
    time.sleep(0.05)  # let mtime granularity clear
    album.run()
    after = {
        n: os.path.getmtime(os.path.join(album.output_dir, n))
        for n in os.listdir(album.output_dir)
    }
    assert before == after


def test_sync_redownloads_on_size_mismatch(album):
    album.run()
    target = os.path.join(album.output_dir, sorted(os.listdir(album.output_dir))[0])
    expected_size = os.path.getsize(target)
    with open(target, "wb") as f:
        f.write(b"\0")  # truncate
    assert os.path.getsize(target) != expected_size
    album.run()
    assert os.path.getsize(target) == expected_size


def test_sync_prunes_orphan_matching_pattern(album):
    album.run()
    orphan = os.path.join(album.output_dir, "GHOST__deadbeef.jpg")
    open(orphan, "wb").close()
    album.run()
    assert not os.path.exists(orphan)


def test_sync_leaves_unmanaged_file_alone(album):
    album.run()
    manual = os.path.join(album.output_dir, "notes.txt")
    with open(manual, "w") as f:
        f.write("hi")
    album.run()
    assert os.path.exists(manual)


def test_sync_prune_off_preserves_orphan(album):
    album.run()
    orphan = os.path.join(album.output_dir, "GHOST__deadbeef.jpg")
    open(orphan, "wb").close()
    album.run(prune=False)
    assert os.path.exists(orphan)


def test_sync_prunes_asset_removed_from_manifest(album):
    album.run()
    assert len(os.listdir(album.output_dir)) == 2
    smaller = dict(album.stream)
    smaller["photos"] = album.stream["photos"][:1]
    album.run(stream=smaller)
    assert len(os.listdir(album.output_dir)) == 1


def test_sync_empty_manifest_prunes_all_managed_files(album):
    album.run()
    assert len(os.listdir(album.output_dir)) == 2
    empty = dict(album.stream)
    empty["photos"] = []
    album.run(stream=empty)
    assert len(os.listdir(album.output_dir)) == 0


def test_sync_filename_collision_yields_distinct_local_files(album):
    stream = _stream_fixture()
    stream["photos"].append({
        "photoGuid": "GUID-PHOTO-DUP",
        "contributorFullName": "Sibling",
        "caption": None,
        "derivatives": {
            "2048": {"checksum": "cksum-photo-dup", "fileSize": "40000"},
        },
    })
    asset_urls = _asset_urls_fixture()
    asset_urls["items"]["cksum-photo-dup"] = {
        "url_location": "cdn.example.com",
        "url_path": "/x/IMG_0001.JPG?sig=zzz",  # same Apple filename as photo #1
    }
    album.run(stream=stream, asset_urls=asset_urls)
    files = os.listdir(album.output_dir)
    assert len(files) == 3
    img_variants = [f for f in files if "IMG_0001" in f]
    assert len(img_variants) == 2
    assert len(set(img_variants)) == 2


# ---------- _configure_logging ---------------------------------------------

@pytest.fixture
def restore_root_logger():
    """Snapshot root logger state so tests that mutate it don't leak."""
    root = logging.getLogger()
    level = root.level
    handlers = root.handlers[:]
    yield
    root.handlers = handlers
    root.level = level


@pytest.mark.parametrize("level_name,expected", [
    ("DEBUG", logging.DEBUG),
    ("INFO", logging.INFO),
    ("WARNING", logging.WARNING),
    ("debug", logging.DEBUG),  # case-insensitive
])
def test_configure_logging_sets_level(level_name, expected, restore_root_logger):
    sync._configure_logging(level_name)
    assert logging.getLogger().level == expected


def test_configure_logging_defaults_to_info_on_unknown(restore_root_logger):
    sync._configure_logging("NOT-A-LEVEL")
    assert logging.getLogger().level == logging.INFO


# ---------- _post_json -----------------------------------------------------

class _FakeResponse:
    """Context-manager wrapper matching what urlopen returns on success."""

    def __init__(self, status, headers, body_bytes):
        self.status = status
        self.headers = headers
        self._body = body_bytes

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def test_post_json_returns_status_headers_and_parsed_body(monkeypatch):
    body = b'{"result": "ok"}'
    captured = {}

    def fake_urlopen(req):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["origin"] = req.headers.get("Origin")
        captured["content_type"] = req.headers.get("Content-type")
        captured["body"] = req.data
        return _FakeResponse(status=200, headers={"X-Foo": "bar"}, body_bytes=body)

    monkeypatch.setattr(sync.urllib.request, "urlopen", fake_urlopen)
    status, headers, parsed = sync._post_json("https://example.com/foo", {"a": 1})

    assert status == 200
    assert headers.get("X-Foo") == "bar"
    assert parsed == {"result": "ok"}
    # Verify the request was shaped the way Apple expects.
    assert captured["url"] == "https://example.com/foo"
    assert captured["method"] == "POST"
    assert captured["origin"] == "https://www.icloud.com"
    assert captured["body"] == b'{"a": 1}'


def test_post_json_reads_body_from_http_error(monkeypatch):
    """Apple's shard-redirect returns 330 as an HTTPError; the body still
    carries the correct host, so _post_json must return the error path."""
    error_body = b'{"X-Apple-MMe-Host": "p42-sharedstreams.icloud.com"}'

    def fake_urlopen(_req):
        raise urllib.error.HTTPError(
            url="https://example.com",
            code=330,
            msg="Moved",
            hdrs={"X-Apple-MMe-Host": "p42-sharedstreams.icloud.com"},
            fp=io.BytesIO(error_body),
        )

    monkeypatch.setattr(sync.urllib.request, "urlopen", fake_urlopen)
    status, headers, parsed = sync._post_json("https://example.com", {})

    assert status == 330
    assert headers.get("X-Apple-MMe-Host") == "p42-sharedstreams.icloud.com"
    assert parsed["X-Apple-MMe-Host"] == "p42-sharedstreams.icloud.com"


# ---------- fetch_stream / fetch_asset_urls error paths --------------------

def test_fetch_stream_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(sync, "_post_json", lambda url, payload: (500, {}, {"e": "x"}))
    with pytest.raises(RuntimeError, match="webstream failed"):
        sync.fetch_stream("23", "TOKEN")


def test_fetch_asset_urls_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(sync, "_post_json", lambda url, payload: (500, {}, {"e": "x"}))
    with pytest.raises(RuntimeError, match="webasseturls failed"):
        sync.fetch_asset_urls("23", "TOKEN", ["guid1"])


# ---------- _prune_removed skips non-files ---------------------------------

def test_prune_skips_directories_matching_pattern(tmp_path):
    # A subdirectory whose name matches the managed pattern should NOT be
    # removed — _prune_removed only removes regular files.
    subdir = tmp_path / "SUBDIR__deadbeef.jpg"
    subdir.mkdir()
    removed = sync._prune_removed(str(tmp_path), set())
    assert removed == 0
    assert subdir.is_dir()


# ---------- download -------------------------------------------------------

def test_download_writes_response_bytes_to_disk(tmp_path, monkeypatch):
    payload = b"hello world" * 100

    def fake_urlopen(_url):
        return _FakeResponse(status=200, headers={}, body_bytes=payload)

    monkeypatch.setattr(sync.urllib.request, "urlopen", fake_urlopen)
    dest = str(tmp_path / "out.bin")
    written = sync.download("https://example.com/x", dest)

    assert written == len(payload)
    with open(dest, "rb") as f:
        assert f.read() == payload


# ---------- _env -----------------------------------------------------------

def test_env_returns_value_when_set(monkeypatch):
    monkeypatch.setenv("EXAMPLE_VAR", "hello")
    assert sync._env("EXAMPLE_VAR") == "hello"


def test_env_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("EXAMPLE_VAR", raising=False)
    assert sync._env("EXAMPLE_VAR", "fallback") == "fallback"


def test_env_exits_with_code_2_when_required_and_missing(monkeypatch):
    monkeypatch.delenv("EXAMPLE_VAR", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        sync._env("EXAMPLE_VAR", required=True)
    assert exc_info.value.code == 2


def test_env_treats_empty_string_as_missing_when_required(monkeypatch):
    monkeypatch.setenv("EXAMPLE_VAR", "")
    with pytest.raises(SystemExit) as exc_info:
        sync._env("EXAMPLE_VAR", required=True)
    assert exc_info.value.code == 2


# ---------- _install_signal_handlers ---------------------------------------

def test_install_signal_handlers_registers_sigterm_and_sigint(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sync.signal, "signal",
        lambda sig, handler: calls.append((sig, handler)),
    )
    sync._install_signal_handlers(threading.Event())
    registered_signals = {sig for sig, _ in calls}
    assert registered_signals == {sync.signal.SIGTERM, sync.signal.SIGINT}


def test_install_signal_handler_sets_stop_event_when_invoked(monkeypatch):
    captured_handlers = {}
    monkeypatch.setattr(
        sync.signal, "signal",
        lambda sig, handler: captured_handlers.__setitem__(sig, handler),
    )
    stop = threading.Event()
    sync._install_signal_handlers(stop)
    assert not stop.is_set()
    # Invoke the SIGTERM handler as if the OS delivered the signal.
    captured_handlers[sync.signal.SIGTERM](sync.signal.SIGTERM, None)
    assert stop.is_set()


# ---------- main() ---------------------------------------------------------

@pytest.fixture
def main_env(monkeypatch, tmp_path):
    """Baseline env for main() tests. Individual tests override as needed."""
    monkeypatch.setenv("SHARED_ALBUM_URL", "https://www.icloud.com/sharedalbum/#B2AJ")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("SYNC_INTERVAL_HOURS", "0")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.delenv("PRUNE_REMOVED", raising=False)
    return monkeypatch


def test_main_one_shot_calls_sync_album_once_and_returns_zero(main_env):
    calls = []
    main_env.setattr(sync, "sync_album",
                     lambda url, output_dir, prune=True: calls.append((url, output_dir, prune)))
    assert sync.main() == 0
    assert len(calls) == 1
    assert calls[0][2] is True  # prune defaults to True


def test_main_honours_prune_removed_false(main_env):
    main_env.setenv("PRUNE_REMOVED", "false")
    seen_prune = []
    main_env.setattr(sync, "sync_album",
                     lambda url, output_dir, prune=True: seen_prune.append(prune))
    sync.main()
    assert seen_prune == [False]


def test_main_exits_when_shared_album_url_missing(main_env):
    main_env.delenv("SHARED_ALBUM_URL", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        sync.main()
    assert exc_info.value.code == 2


def test_main_returns_2_when_interval_is_not_numeric(main_env):
    main_env.setenv("SYNC_INTERVAL_HOURS", "not-a-number")
    main_env.setattr(sync, "sync_album", lambda *a, **kw: None)
    assert sync.main() == 2


def test_main_catches_and_logs_sync_exception(main_env, caplog):
    def failing(url, output_dir, prune=True):
        raise RuntimeError("boom")
    main_env.setattr(sync, "sync_album", failing)
    # _configure_logging calls basicConfig(force=True), which strips caplog's
    # handler. Patch it out for this test so caplog can observe the record.
    main_env.setattr(sync, "_configure_logging", lambda _level: None)
    caplog.set_level(logging.ERROR, logger="sync")
    assert sync.main() == 0  # one-shot mode: exception logged, main returns cleanly
    assert any("sync failed" in r.message for r in caplog.records)


def test_main_daemon_loop_exits_when_stop_wait_returns_true(main_env):
    """SYNC_INTERVAL_HOURS > 0 enters the daemon loop. If the stop event's
    wait() returns True (as it would after a signal during sleep), the loop
    logs 'stop requested during sleep' and exits after one sync."""
    main_env.setenv("SYNC_INTERVAL_HOURS", "12")

    class ImmediateStopEvent(threading.Event):
        def wait(self, timeout=None):
            return True  # pretend a signal fired during sleep

    main_env.setattr(sync.threading, "Event", ImmediateStopEvent)

    call_count = 0
    def fake_sync(url, output_dir, prune=True):
        nonlocal call_count
        call_count += 1
    main_env.setattr(sync, "sync_album", fake_sync)

    assert sync.main() == 0
    assert call_count == 1  # exactly one iteration before the wait bailed out


def test_main_daemon_loop_exits_when_stop_set_before_next_iteration(main_env):
    """If the stop event is set while sync_album is running, the loop
    should notice on the next `not stop.is_set()` check and exit before
    hitting the sleep."""
    main_env.setenv("SYNC_INTERVAL_HOURS", "12")

    stop_holder = {}
    original_event_cls = sync.threading.Event

    class CapturingEvent(original_event_cls):
        def __init__(self):
            super().__init__()
            stop_holder["event"] = self

    main_env.setattr(sync.threading, "Event", CapturingEvent)

    call_count = 0
    def fake_sync(url, output_dir, prune=True):
        nonlocal call_count
        call_count += 1
        stop_holder["event"].set()  # signal during sync
    main_env.setattr(sync, "sync_album", fake_sync)

    assert sync.main() == 0
    assert call_count == 1


def test_main_daemon_loop_iterates_when_wait_returns_false(main_env):
    """Normal daemon-mode wake: stop.wait() returns False (timeout elapsed
    without a signal), the loop runs another sync, then wait() returns True
    and we exit. Exercises the 'False path' of the wait() branch."""
    main_env.setenv("SYNC_INTERVAL_HOURS", "12")

    class LoopThenStopEvent(threading.Event):
        def __init__(self):
            super().__init__()
            self._waits = 0

        def wait(self, timeout=None):
            self._waits += 1
            # First wait: pretend the sleep elapsed normally, keep looping.
            # Second wait: pretend a signal fired, exit the loop.
            return self._waits >= 2

    main_env.setattr(sync.threading, "Event", LoopThenStopEvent)

    call_count = 0
    def fake_sync(url, output_dir, prune=True):
        nonlocal call_count
        call_count += 1
    main_env.setattr(sync, "sync_album", fake_sync)

    assert sync.main() == 0
    assert call_count == 2  # ran twice: initial + one wake-up cycle


def test_main_daemon_loop_exits_via_while_condition(main_env):
    """Defensive branch: if stop is set between wait() returning False and
    the next `while not stop.is_set()` check (e.g. a signal handler fires
    during that gap), the loop exits via the while condition rather than
    via break. Contrived race, but the guard exists so we test it."""
    main_env.setenv("SYNC_INTERVAL_HOURS", "12")

    class WaitSetsAndReturnsFalse(threading.Event):
        def wait(self, timeout=None):
            self.set()      # simulate: signal fires in the sleep gap
            return False    # but wait() itself reports 'timed out'

    main_env.setattr(sync.threading, "Event", WaitSetsAndReturnsFalse)

    call_count = 0
    def fake_sync(url, output_dir, prune=True):
        nonlocal call_count
        call_count += 1
    main_env.setattr(sync, "sync_album", fake_sync)

    assert sync.main() == 0
    assert call_count == 1  # ran once, then while-check saw is_set()=True
