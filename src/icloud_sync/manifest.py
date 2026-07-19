"""Pure-function reasoning over Apple's manifest structure.

URL / token parsing, best-derivative selection, per-photo sort keys.
No I/O and no side effects — every function here can be exercised with
plain dict fixtures.
"""

import re
from urllib.parse import urlparse

# Apple's shared-album tokens are base62 (0-9A-Za-z). Any single character
# in that set is a legal first character — the first char just seeds the
# shard-partition calculation on Apple's side, it doesn't have a fixed
# prefix. Validating against the charset rejects obvious mis-pastes without
# tying us to any specific length.
_TOKEN_RE = re.compile(r"^[0-9A-Za-z]+$")


def extract_token(url: str) -> str:
    """Extract the album token from a public iCloud shared album URL.

    Two shapes Apple hands out:
    - https://www.icloud.com/sharedalbum/#TOKEN  — classic form, token in fragment
    - https://share.icloud.com/photos/TOKEN     — Apple's newer short-link form
    Anything else — bare tokens, other hosts, other paths — is rejected."""
    parsed = urlparse(url)
    if parsed.fragment:
        candidate = parsed.fragment
    elif parsed.netloc == "share.icloud.com" and parsed.path.startswith("/photos/"):
        candidate = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    else:
        raise ValueError(f"URL does not look like an iCloud shared album URL: {url}")
    if not _TOKEN_RE.match(candidate):
        raise ValueError(f"URL does not look like an iCloud shared album URL: {url}")
    return candidate


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


def _photo_sort_key(photo: dict) -> tuple:
    # Primary: when this batch was uploaded to the album (so a fresh drop
    # of vintage photos stays on disk). Secondary: capture time within a
    # batch. Tertiary: photoGuid — deterministic tiebreaker when a batch
    # contains multiple photos captured in the same second. Direction on
    # the guid is arbitrary; consistency across runs is what matters.
    return (photo["batchDateCreated"], photo["dateCreated"], photo["photoGuid"])
