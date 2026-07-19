"""The per-run sync flow.

Ties `apple_api` (network), `manifest` (data reasoning), and `storage`
(filesystem) into one idempotent cycle. Handles the preflight budget
check and the two behaviors — skip vs. keep-newest-that-fit — around
the AUTOPRUNE_ON_LOW_STORAGE flag.
"""

import logging
import os
from typing import TypedDict

from . import apple_api, manifest, storage

log = logging.getLogger("sync")


class _Candidate(TypedDict):
    """Per-photo download plan built once, upfront. Kept as a TypedDict
    so `storage._choose_keep_set` can generic-preserve the shape through
    the budget walk and the per-field types stay checkable at every
    access site downstream."""

    photo: dict
    best_key: str
    filename: str
    download_url: str
    size: int


def sync_album(
    url: str,
    output_dir: str,
    buffer_percent: float,
    autoprune_on_low_storage: bool,
) -> None:
    token = manifest.extract_token(url)
    shard = apple_api.resolve_shard(token)
    stream = apple_api.fetch_stream(shard, token)

    album_name = stream.get("streamName", "(unnamed)")
    owner = f"{stream.get('userFirstName', '?')} {stream.get('userLastName', '?')}"
    log.info("album=%r owner=%r assets=%d", album_name, owner, len(stream["photos"]))

    os.makedirs(output_dir, exist_ok=True)

    photo_guids = [p["photoGuid"] for p in stream["photos"]]
    asset_response = apple_api.fetch_asset_urls(shard, token, photo_guids)
    items = asset_response["items"]
    locations = asset_response["locations"]

    # Materialize the full download plan once, newest-first. Sorting here
    # (before any budget check) lets both the fits-cleanly path and the
    # autoprune path share the same iteration order.
    sorted_photos = sorted(stream["photos"], key=manifest._photo_sort_key, reverse=True)
    candidates: list[_Candidate] = []
    for photo in sorted_photos:
        best_key = manifest.best_derivative_key(photo["derivatives"])
        deriv = photo["derivatives"][best_key]
        item = items[deriv["checksum"]]
        apple_filename = item["url_path"].split("/")[-1].split("?")[0]
        candidates.append(
            {
                "photo": photo,
                "best_key": best_key,
                "filename": storage.local_filename(photo["photoGuid"], apple_filename),
                "download_url": apple_api.build_download_url(item, locations),
                "size": int(deriv.get("fileSize", 0)),
            }
        )

    total_needed = sum(c["size"] for c in candidates)
    usable, current_managed, reserved = storage._disk_budget(output_dir, buffer_percent)

    if total_needed <= usable:
        keep_set = candidates
    elif autoprune_on_low_storage:
        keep_set = storage._choose_keep_set(candidates, usable)
        if not keep_set:
            log.error(
                "autoprune refused: no asset fits under budget of %d bytes; "
                "leaving %d managed bytes on disk untouched",
                usable,
                current_managed,
            )
            return
        log.warning(
            "autoprune active: album %d bytes exceeds budget %d bytes; "
            "keeping newest %d of %d assets (excluding %d oldest)",
            total_needed,
            usable,
            len(keep_set),
            len(candidates),
            len(candidates) - len(keep_set),
        )
    else:
        log.error(
            "album size %d bytes exceeds budget %d bytes "
            "(reserved=%d managed_on_disk=%d); "
            "set AUTOPRUNE_ON_LOW_STORAGE=true to evict oldest-first, "
            "raise the buffer, or free space; skipping this run",
            total_needed,
            usable,
            reserved,
            current_managed,
        )
        return

    downloaded = 0
    skipped = 0
    expected_names = set()
    for c in keep_set:
        filename = c["filename"]
        expected_names.add(filename)
        dest = os.path.join(output_dir, filename)
        expected_size = c["size"]

        if os.path.exists(dest) and expected_size and os.path.getsize(dest) == expected_size:
            log.debug("skip %s (already %d bytes)", filename, expected_size)
            skipped += 1
            continue

        bytes_written = apple_api.download(c["download_url"], dest)
        downloaded += 1
        photo = c["photo"]
        contrib = photo.get("contributorFullName", "?")
        caption = photo.get("caption") or ""
        log.info(
            "pull %s (%s, %s bytes) by %s%s",
            filename,
            c["best_key"],
            f"{bytes_written:,}",
            contrib,
            f" — {caption!r}" if caption else "",
        )

    pruned = storage._prune_removed(output_dir, expected_names)
    log.info(
        "done downloaded=%d skipped=%d pruned=%d output=%s",
        downloaded,
        skipped,
        pruned,
        output_dir,
    )
