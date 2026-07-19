"""Tests for icloud_sync.storage — filenames, managed-name regex,
pruning safety, disk-budget math, keep-set selection."""

import re

import pytest

from icloud_sync import storage

# ---------- local_filename -------------------------------------------------


def test_local_filename_is_deterministic():
    assert storage.local_filename("GUID-X", "IMG_5744.JPG") == storage.local_filename(
        "GUID-X", "IMG_5744.JPG"
    )


def test_local_filename_differs_by_guid():
    assert storage.local_filename("GUID-A", "IMG_5744.JPG") != storage.local_filename(
        "GUID-B", "IMG_5744.JPG"
    )


def test_local_filename_differs_by_source_name():
    assert storage.local_filename("GUID-X", "IMG_5744.JPG") != storage.local_filename(
        "GUID-X", "IMG_5745.JPG"
    )


@pytest.mark.parametrize(
    "src,expected_ext",
    [
        ("IMG.JPG", ".JPG"),
        ("clip.mp4", ".mp4"),
        ("archive.tar.gz", ".gz"),
    ],
)
def test_local_filename_preserves_extension(src, expected_ext):
    assert storage.local_filename("GUID-X", src).endswith(expected_ext)


def test_local_filename_no_extension_leaves_none():
    name = storage.local_filename("GUID-X", "raw")
    assert re.match(r"^raw__[0-9a-f]{8}$", name)


def test_local_filename_multi_dot_splits_at_last_dot():
    name = storage.local_filename("GUID-X", "archive.tar.gz")
    assert name.startswith("archive.tar__")
    assert name.endswith(".gz")


@pytest.mark.parametrize("src", ["IMG.JPG", "clip.mp4", "raw", "foo.tar.gz"])
def test_local_filename_output_matches_managed_pattern(src):
    # Round-trip: every filename this function produces must be recognized
    # as ours by the pruning regex — otherwise we'd fail to clean up our own
    # downloads.
    assert storage._MANAGED_NAME_RE.search(storage.local_filename("GUID-X", src))


# ---------- managed-name regex ---------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        # Matches
        ("IMG_5744__a1b2c3d4.JPG", True),
        ("clip__deadbeef.mp4", True),
        ("archive__a1b2c3d4.gz", True),
        ("raw__a1b2c3d4", True),  # no-extension case
        # Rejections
        ("IMG_5744.JPG", False),  # no suffix
        ("IMG_5744__abc.JPG", False),  # hash too short
        ("IMG_5744__a1b2c3d4e5.JPG", False),  # hash too long
        ("IMG_5744__A1B2C3D4.JPG", False),  # uppercase hex — hashlib never emits this
    ],
)
def test_managed_name_regex(name, expected):
    assert bool(storage._MANAGED_NAME_RE.search(name)) is expected


# ---------- _prune_removed skips non-files ---------------------------------


def test_prune_skips_directories_matching_pattern(tmp_path):
    # A subdirectory whose name matches the managed pattern should NOT be
    # removed — _prune_removed only removes regular files.
    subdir = tmp_path / "SUBDIR__deadbeef.jpg"
    subdir.mkdir()
    removed = storage._prune_removed(str(tmp_path), set())
    assert removed == 0
    assert subdir.is_dir()


# ---------- _disk_budget ---------------------------------------------------


def test_disk_budget_counts_managed_files_only(tmp_path):
    (tmp_path / "IMG_1__deadbeef.JPG").write_bytes(b"\0" * 500)
    (tmp_path / "IMG_2__cafef00d.JPG").write_bytes(b"\0" * 1500)
    (tmp_path / "notes.txt").write_bytes(b"\0" * 9999)  # not managed
    usable, current, _reserved = storage._disk_budget(str(tmp_path), buffer_percent=0.0)
    assert current == 2000
    # With 0% buffer, usable >= current (we could keep everything and add more).
    assert usable >= current


def test_disk_budget_reserved_grows_with_buffer(tmp_path):
    (tmp_path / "IMG_1__deadbeef.JPG").write_bytes(b"\0" * 100)
    _u0, _c, res0 = storage._disk_budget(str(tmp_path), buffer_percent=0.0)
    _u1, _c, res10 = storage._disk_budget(str(tmp_path), buffer_percent=10.0)
    assert res0 == 0
    assert res10 > 0


def test_disk_budget_high_buffer_can_drive_usable_to_current(tmp_path):
    # With a very high buffer, free_headroom clamps to 0 and usable == current.
    (tmp_path / "IMG_1__deadbeef.JPG").write_bytes(b"\0" * 100)
    usable, current, _ = storage._disk_budget(str(tmp_path), buffer_percent=99.99)
    assert usable == current == 100


# ---------- _choose_keep_set -----------------------------------------------


def test_choose_keep_set_takes_everything_when_budget_generous():
    candidates = [{"size": 100}, {"size": 200}, {"size": 300}]
    assert storage._choose_keep_set(candidates, budget_bytes=10_000) == candidates


def test_choose_keep_set_returns_prefix_at_boundary():
    candidates = [{"size": 100}, {"size": 200}, {"size": 300}]
    # Budget 300 fits first two (sum=300). Third would push to 600.
    kept = storage._choose_keep_set(candidates, budget_bytes=300)
    assert [c["size"] for c in kept] == [100, 200]


def test_choose_keep_set_empty_when_first_candidate_exceeds_budget():
    candidates = [{"size": 1000}, {"size": 10}]
    # Newest-first is priority; if the top can't fit, we don't skip it to
    # smuggle in a smaller-but-older one.
    assert storage._choose_keep_set(candidates, budget_bytes=100) == []


def test_choose_keep_set_empty_input_returns_empty():
    assert storage._choose_keep_set([], budget_bytes=1000) == []
