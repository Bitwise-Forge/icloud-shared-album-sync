"""Tests for icloud_sync.manifest — token parsing, derivative selection,
sort keys. Everything here is pure-function, no I/O."""

import pytest

from icloud_sync import manifest

# ---------- extract_token --------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        # Classic fragment form — the shape Apple's Photos.app hands out.
        (
            "https://www.icloud.com/sharedalbum/#B2AJ0DiRHGf731D",
            "B2AJ0DiRHGf731D",
        ),
        # Any base62 first character is legal — 'B' was one example among
        # many, never a required prefix. Cover A/a/Z/z/digit to lock this in.
        ("https://www.icloud.com/sharedalbum/#A0z5qAGN1JIFd3y", "A0z5qAGN1JIFd3y"),
        ("https://www.icloud.com/sharedalbum/#a0z5qAGN1JIFd3y", "a0z5qAGN1JIFd3y"),
        ("https://www.icloud.com/sharedalbum/#Zabcdef123", "Zabcdef123"),
        ("https://www.icloud.com/sharedalbum/#9zzzz111", "9zzzz111"),
        # Short-link form — share.icloud.com/photos/TOKEN. Same token,
        # different host + path shape.
        (
            "https://share.icloud.com/photos/B2AJ0DiRHGf731D",
            "B2AJ0DiRHGf731D",
        ),
        (
            "https://share.icloud.com/photos/A0z5qAGN1JIFd3y",
            "A0z5qAGN1JIFd3y",
        ),
    ],
)
def test_extract_token_valid(url, expected):
    assert manifest.extract_token(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        # No fragment, wrong host → rejected.
        "https://www.icloud.com/sharedalbum/",
        # Empty fragment → rejected.
        "https://www.icloud.com/sharedalbum/#",
        # Fragment contains a non-base62 char (dash) — real Apple tokens
        # never contain punctuation.
        "https://www.icloud.com/sharedalbum/#has-dash",
        # share.icloud.com but wrong path prefix.
        "https://share.icloud.com/notphotos/ABC123",
        # Bare token with no URL wrapper — not accepted; users must paste
        # the URL Apple gave them.
        "B2AJ0DiRHGf731D",
        # Completely unrelated URL.
        "https://example.com/foo",
        # Empty string.
        "",
    ],
)
def test_extract_token_rejects_invalid(url):
    with pytest.raises(ValueError):
        manifest.extract_token(url)


# ---------- best_derivative_key --------------------------------------------


@pytest.mark.parametrize(
    "derivatives,expected",
    [
        ({"342": {}, "1024": {}, "2048": {}}, "2048"),
        ({"342": {}}, "342"),
        ({"360p": {}, "720p": {}, "PosterFrame": {}}, "720p"),
        ({"360p": {}, "PosterFrame": {}}, "360p"),
        ({"480p": {}, "PosterFrame": {}}, "480p"),
    ],
)
def test_best_derivative_key(derivatives, expected):
    assert manifest.best_derivative_key(derivatives) == expected


def test_best_derivative_key_unrecognized_shape_raises():
    with pytest.raises(ValueError):
        manifest.best_derivative_key({"PosterFrame": {}, "Thumbnail": {}})


# ---------- _photo_sort_key ------------------------------------------------


def _photo(guid, batch, date):
    return {
        "photoGuid": guid,
        "batchDateCreated": batch,
        "dateCreated": date,
        "derivatives": {},
    }


def test_photo_sort_key_batch_is_primary():
    older_batch = _photo("g1", "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
    newer_batch = _photo("g2", "2026-06-01T00:00:00Z", "2020-01-01T00:00:00Z")
    ordered = sorted([older_batch, newer_batch], key=manifest._photo_sort_key, reverse=True)
    # Even though older_batch has a newer dateCreated, newer batchDateCreated wins.
    assert ordered[0]["photoGuid"] == "g2"


def test_photo_sort_key_date_is_secondary_within_batch():
    older_capture = _photo("g1", "2026-06-01T00:00:00Z", "2026-01-01T00:00:00Z")
    newer_capture = _photo("g2", "2026-06-01T00:00:00Z", "2026-05-01T00:00:00Z")
    ordered = sorted([older_capture, newer_capture], key=manifest._photo_sort_key, reverse=True)
    assert ordered[0]["photoGuid"] == "g2"


def test_photo_sort_key_guid_is_deterministic_tiebreaker():
    a = _photo("guid-a", "2026-06-01T00:00:00Z", "2026-06-01T00:00:00Z")
    b = _photo("guid-b", "2026-06-01T00:00:00Z", "2026-06-01T00:00:00Z")
    # Repeat the sort — result must be identical, regardless of input order.
    from_ab = sorted([a, b], key=manifest._photo_sort_key, reverse=True)
    from_ba = sorted([b, a], key=manifest._photo_sort_key, reverse=True)
    assert [p["photoGuid"] for p in from_ab] == [p["photoGuid"] for p in from_ba]
