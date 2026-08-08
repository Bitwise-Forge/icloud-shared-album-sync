"""Tests for icloud_sync.orchestrator.sync_album — the per-run cycle.

End-to-end runs use the `album` fixture (from conftest.py) with
apple_api._post_json and apple_api.download mocked. Budget-behavior
tests build larger fixtures and monkey-patch storage._disk_budget
directly to force specific over/under scenarios.
"""

import logging
import os
import time

import pytest
from conftest import (
    _asset_urls_fixture,
    _large_album_stream,
    _large_asset_urls,
    _MockPostJson,
    _stream_fixture,
)

from icloud_sync import apple_api, orchestrator, storage

# ---------- sync_album end-to-end ------------------------------------------


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
        assert storage._MANAGED_NAME_RE.search(name)


def test_sync_downloaded_file_sizes_match_manifest(album):
    from icloud_sync import manifest

    album.run()
    for photo in album.stream["photos"]:
        best = manifest.best_derivative_key(photo["derivatives"])
        deriv = photo["derivatives"][best]
        item = album.asset_urls["items"][deriv["checksum"]]
        apple_name = item["url_path"].split("/")[-1].split("?")[0]
        local_name = storage.local_filename(photo["photoGuid"], apple_name)
        actual = os.path.getsize(os.path.join(album.output_dir, local_name))
        assert actual == int(deriv["fileSize"])


def test_sync_is_idempotent(album):
    album.run()
    before = {
        n: os.path.getmtime(os.path.join(album.output_dir, n)) for n in os.listdir(album.output_dir)
    }
    time.sleep(0.05)  # let mtime granularity clear
    album.run()
    after = {
        n: os.path.getmtime(os.path.join(album.output_dir, n)) for n in os.listdir(album.output_dir)
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


def test_sync_empty_manifest_logs_prune_only_completion(album, caplog):
    """The empty-album path must complete cleanly and log the standard
    done= line. Regression guard: prior versions called Apple's
    webasseturls with an empty photoGuids list, hit 400, and exited via
    RuntimeError before the prune step ran."""
    album.run()
    assert len(os.listdir(album.output_dir)) == 2
    empty = dict(album.stream)
    empty["photos"] = []
    caplog.set_level(logging.INFO, logger="sync")
    album.run(stream=empty)
    assert any("downloaded=0" in r.message and "pruned=2" in r.message for r in caplog.records)


def test_sync_first_run_empty_manifest_no_files_to_prune(album, caplog):
    """Fresh sync against an already-empty album: no prior files on disk,
    no downloads, no prunes, no crash. Documents that the empty-album
    branch handles pruned=0 identically to pruned=N — real production
    scenario when a recipient boots a new frame before adding any photos."""
    empty = dict(album.stream)
    empty["photos"] = []
    caplog.set_level(logging.INFO, logger="sync")
    album.run(stream=empty)
    assert os.listdir(album.output_dir) == []
    assert any("downloaded=0" in r.message and "pruned=0" in r.message for r in caplog.records)


def test_sync_filename_collision_yields_distinct_local_files(album):
    stream = _stream_fixture()
    stream["photos"].append(
        {
            "photoGuid": "GUID-PHOTO-DUP",
            "contributorFullName": "Sibling",
            "caption": None,
            "batchDateCreated": "2026-07-18T21:00:00Z",
            "dateCreated": "2026-07-06T12:00:00Z",
            "derivatives": {
                "2048": {"checksum": "cksum-photo-dup", "fileSize": "40000"},
            },
        }
    )
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


# ---------- sync_album budget / autoprune behavior -------------------------


def test_sync_over_budget_autoprune_off_skips_run(tmp_path, monkeypatch, caplog):
    """When the album exceeds budget and autoprune is off, nothing on disk
    should change — no downloads, no prunes."""
    stream = _large_album_stream([100, 200, 300])
    asset_urls = _large_asset_urls(3)
    output_dir = str(tmp_path / "photos")
    os.makedirs(output_dir)
    # Pre-existing managed file to prove we don't prune it away.
    canary = os.path.join(output_dir, "CANARY__deadbeef.JPG")
    with open(canary, "wb") as f:
        f.write(b"x")

    # Budget of 50 bytes can't hold any single photo (100/200/300).
    monkeypatch.setattr(storage, "_disk_budget", lambda _dir, _buf: (50, 1, 0))
    fake = _MockPostJson(stream, asset_urls)
    monkeypatch.setattr(apple_api, "_post_json", fake)

    def refuse_download(_url, _dest):
        raise AssertionError("download must not be called when over budget and autoprune off")

    monkeypatch.setattr(apple_api, "download", refuse_download)
    caplog.set_level(logging.ERROR, logger="sync")

    orchestrator.sync_album("https://www.icloud.com/sharedalbum/#BTEST", output_dir, 10.0, False)

    assert os.path.exists(canary)
    assert any("exceeds budget" in r.message for r in caplog.records)


def test_sync_over_budget_autoprune_on_keeps_newest_that_fit(tmp_path, monkeypatch):
    """With autoprune on, the newest slice that fits under budget is kept.
    Older photos are neither downloaded nor left orphaned on disk."""
    # Photos are sorted newest-first by dateCreated DESC. Sizes: newest=300,
    # middle=200, oldest=100. Budget 400 fits newest (300) + can't add middle
    # (would total 500). Result: keep only the newest.
    stream = _large_album_stream([100, 200, 300])  # oldest first in input
    asset_urls = _large_asset_urls(3)
    output_dir = str(tmp_path / "photos")
    monkeypatch.setattr(storage, "_disk_budget", lambda _dir, _buf: (400, 0, 0))
    monkeypatch.setattr(apple_api, "_post_json", _MockPostJson(stream, asset_urls))

    downloaded_urls = []

    def fake_download(url, dest_path):
        downloaded_urls.append(url)
        # Recover the intended size from the manifest via the URL suffix.
        for photo in stream["photos"]:
            item = asset_urls["items"][photo["derivatives"]["2048"]["checksum"]]
            expected_url = apple_api.build_download_url(item, asset_urls["locations"])
            if url == expected_url:
                size = int(photo["derivatives"]["2048"]["fileSize"])
                with open(dest_path, "wb") as f:
                    f.write(b"\0" * size)
                return size
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(apple_api, "download", fake_download)

    orchestrator.sync_album("https://www.icloud.com/sharedalbum/#BTEST", output_dir, 10.0, True)

    # Exactly one file on disk: the newest (photo index 2, size 300).
    files = os.listdir(output_dir)
    assert len(files) == 1
    assert os.path.getsize(os.path.join(output_dir, files[0])) == 300
    assert len(downloaded_urls) == 1


def test_sync_over_budget_autoprune_on_no_asset_fits_leaves_disk_alone(
    tmp_path, monkeypatch, caplog
):
    """If not even the newest photo fits under budget, refuse to wipe out
    everything on disk trying — log and skip."""
    stream = _large_album_stream([1000, 2000, 3000])
    asset_urls = _large_asset_urls(3)
    output_dir = str(tmp_path / "photos")
    os.makedirs(output_dir)
    canary = os.path.join(output_dir, "CANARY__deadbeef.JPG")
    with open(canary, "wb") as f:
        f.write(b"\0" * 999)  # will be counted by _managed_bytes_on_disk

    monkeypatch.setattr(storage, "_disk_budget", lambda _dir, _buf: (500, 999, 0))
    monkeypatch.setattr(apple_api, "_post_json", _MockPostJson(stream, asset_urls))

    def refuse_download(*_a, **_kw):
        raise AssertionError("no downloads expected when autoprune refuses")

    monkeypatch.setattr(apple_api, "download", refuse_download)
    caplog.set_level(logging.ERROR, logger="sync")

    orchestrator.sync_album("https://www.icloud.com/sharedalbum/#BTEST", output_dir, 10.0, True)

    assert os.path.exists(canary)
    assert any("autoprune refused" in r.message for r in caplog.records)


def test_sync_autoprune_grows_by_two_evicts_two_oldest(tmp_path, monkeypatch):
    """The canonical scenario: album at capacity, contributor adds 2 new
    photos; next run downloads the 2 new and prunes the 2 oldest, netting
    zero disk-usage change. Verifies the invariant holds cross-run."""
    # Initial album: 5 photos of 100 bytes each. Budget 500. All fit.
    initial = _large_album_stream([100] * 5)
    asset_urls = _large_asset_urls(7)  # allow up to 7 photos
    output_dir = str(tmp_path / "photos")
    monkeypatch.setattr(storage, "_disk_budget", lambda _dir, _buf: (500, 0, 0))
    monkeypatch.setattr(apple_api, "_post_json", _MockPostJson(initial, asset_urls))

    def fake_download(url, dest_path):
        for photo in initial["photos"]:
            item = asset_urls["items"][photo["derivatives"]["2048"]["checksum"]]
            if apple_api.build_download_url(item, asset_urls["locations"]) == url:
                with open(dest_path, "wb") as f:
                    f.write(b"\0" * int(photo["derivatives"]["2048"]["fileSize"]))
                return int(photo["derivatives"]["2048"]["fileSize"])
        raise AssertionError(f"url not in initial: {url}")

    monkeypatch.setattr(apple_api, "download", fake_download)
    orchestrator.sync_album("https://www.icloud.com/sharedalbum/#BTEST", output_dir, 10.0, True)
    assert len(os.listdir(output_dir)) == 5
    initial_names = set(os.listdir(output_dir))

    # Next run: same 5 photos plus 2 new ones (indices 5 and 6, newest by
    # dateCreated). Budget still 500. Keep set = newest 5 = indices 2..6.
    expanded = _large_album_stream([100] * 7)
    monkeypatch.setattr(apple_api, "_post_json", _MockPostJson(expanded, asset_urls))

    def fake_download_expanded(url, dest_path):
        for photo in expanded["photos"]:
            item = asset_urls["items"][photo["derivatives"]["2048"]["checksum"]]
            if apple_api.build_download_url(item, asset_urls["locations"]) == url:
                with open(dest_path, "wb") as f:
                    f.write(b"\0" * int(photo["derivatives"]["2048"]["fileSize"]))
                return int(photo["derivatives"]["2048"]["fileSize"])
        raise AssertionError(f"url not in expanded: {url}")

    monkeypatch.setattr(apple_api, "download", fake_download_expanded)
    orchestrator.sync_album("https://www.icloud.com/sharedalbum/#BTEST", output_dir, 10.0, True)

    final_names = set(os.listdir(output_dir))
    assert len(final_names) == 5
    # Two files came off (the oldest, indices 0 and 1) and two came on (5, 6).
    assert len(final_names - initial_names) == 2
    assert len(initial_names - final_names) == 2


# ---------- per-asset download resilience ----------------------------------


def _two_photo_setup(tmp_path, monkeypatch):
    """Two same-size assets under a comfortable budget, with a knob for
    making specific downloads fail. Returns (output_dir, urls, install)."""
    stream = _large_album_stream([100, 100])
    asset_urls = _large_asset_urls(2)
    output_dir = str(tmp_path / "photos")
    monkeypatch.setattr(storage, "_disk_budget", lambda _dir, _buf: (10_000, 0, 0))
    monkeypatch.setattr(apple_api, "_post_json", _MockPostJson(stream, asset_urls))

    urls = {}
    for i, photo in enumerate(stream["photos"]):
        item = asset_urls["items"][photo["derivatives"]["2048"]["checksum"]]
        urls[i] = apple_api.build_download_url(item, asset_urls["locations"])

    def install(download_fn):
        monkeypatch.setattr(apple_api, "download", download_fn)

    return output_dir, urls, install


def test_one_failed_download_does_not_abort_the_rest(tmp_path, monkeypatch, caplog):
    """The whole point: an asset that 500s or times out costs that asset,
    not the entire cycle. Previously any raise aborted the loop and skipped
    the prune step entirely."""
    output_dir, urls, install = _two_photo_setup(tmp_path, monkeypatch)

    def flaky(url, dest_path):
        if url == urls[0]:
            raise RuntimeError("CDN said no")
        with open(dest_path, "wb") as f:
            f.write(b"\0" * 100)
        return 100

    install(flaky)
    caplog.set_level(logging.INFO, logger="sync")
    orchestrator.sync_album("https://www.icloud.com/sharedalbum/#BTEST", output_dir, 0.0, False)

    assert len(os.listdir(output_dir)) == 1  # the healthy one still landed
    assert any("downloaded=1 skipped=0 failed=1" in r.getMessage() for r in caplog.records)


def test_failed_download_removes_the_partial_file(tmp_path, monkeypatch):
    """A truncated asset would render as garbage on a slideshow and could
    linger if its size happened to match on a later run. It gets deleted."""
    output_dir, urls, install = _two_photo_setup(tmp_path, monkeypatch)

    def partial_then_fail(url, dest_path):
        with open(dest_path, "wb") as f:
            f.write(b"\0" * 37)  # partial write lands on disk
        if url == urls[0]:
            raise RuntimeError("connection reset mid-stream")
        with open(dest_path, "wb") as f:
            f.write(b"\0" * 100)
        return 100

    install(partial_then_fail)
    orchestrator.sync_album("https://www.icloud.com/sharedalbum/#BTEST", output_dir, 0.0, False)

    remaining = os.listdir(output_dir)
    assert len(remaining) == 1
    assert os.path.getsize(os.path.join(output_dir, remaining[0])) == 100


def test_failed_download_with_no_partial_file_is_handled(tmp_path, monkeypatch):
    """Failure before any bytes are written (DNS, connect timeout) leaves
    no file to clean up — the removal must not assume one exists."""
    output_dir, urls, install = _two_photo_setup(tmp_path, monkeypatch)

    def fail_before_writing(url, dest_path):
        if url == urls[0]:
            raise RuntimeError("could not connect")
        with open(dest_path, "wb") as f:
            f.write(b"\0" * 100)
        return 100

    install(fail_before_writing)
    orchestrator.sync_album("https://www.icloud.com/sharedalbum/#BTEST", output_dir, 0.0, False)
    assert len(os.listdir(output_dir)) == 1


def test_all_downloads_failing_raises_as_systemic(tmp_path, monkeypatch):
    """Every attempt failing means the link is down, not that one asset is
    bad. Raising is what gives cli's fast-retry backoff its trigger."""
    output_dir, _urls, install = _two_photo_setup(tmp_path, monkeypatch)
    install(_raise_always)

    with pytest.raises(RuntimeError, match="all 2 attempted downloads failed"):
        orchestrator.sync_album("https://www.icloud.com/sharedalbum/#BTEST", output_dir, 0.0, False)


def _raise_always(_url, _dest_path):
    raise RuntimeError("network unreachable")


def test_partial_failure_still_prunes_removed_assets(tmp_path, monkeypatch):
    """Prune has to run even on a partially failed cycle, otherwise an
    asset deleted from the album sticks around until a flawless run."""
    output_dir, urls, install = _two_photo_setup(tmp_path, monkeypatch)
    os.makedirs(output_dir, exist_ok=True)
    orphan = os.path.join(output_dir, "IMG_9999__deadbeef.JPG")
    with open(orphan, "wb") as f:
        f.write(b"\0" * 10)

    def flaky(url, dest_path):
        if url == urls[0]:
            raise RuntimeError("CDN said no")
        with open(dest_path, "wb") as f:
            f.write(b"\0" * 100)
        return 100

    install(flaky)
    orchestrator.sync_album("https://www.icloud.com/sharedalbum/#BTEST", output_dir, 0.0, False)
    assert not os.path.exists(orphan)
