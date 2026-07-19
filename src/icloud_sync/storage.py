"""Local filesystem bookkeeping.

Filename conventions, orphan pruning, disk-budget math, keep-set
selection. Nothing here talks to Apple — everything is expressible in
terms of the OS and the manifest data the orchestrator hands us.
"""

import hashlib
import logging
import os
import re
import shutil
from collections.abc import Mapping
from typing import TypeVar, cast

# Anything the greedy walk can consume: a mapping carrying a "size" key.
# Bound to `Mapping[str, object]` so both plain dicts and TypedDict-shaped
# items are accepted, and the callsite's specific type is preserved
# through the walk — whatever goes in comes back out.
_KeepItemT = TypeVar("_KeepItemT", bound=Mapping[str, object])

# Local filenames get an 8-hex suffix derived from photoGuid — makes them
# collision-proof across contributors and marks them as ours for pruning.
# Extension optional: local_filename() drops the extension when the source
# filename has none, and the regex has to still match what we produced.
_MANAGED_NAME_RE = re.compile(r"__[0-9a-f]{8}(?:\.[^./]+)?$")

log = logging.getLogger("sync")


def local_filename(photo_guid: str, apple_filename: str) -> str:
    base, ext = os.path.splitext(apple_filename)
    suffix = hashlib.sha1(photo_guid.encode()).hexdigest()[:8]
    return f"{base}__{suffix}{ext}"


def _prune_removed(output_dir: str, expected_names: set) -> int:
    removed = 0
    for entry in os.listdir(output_dir):
        path = os.path.join(output_dir, entry)
        if not os.path.isfile(path):
            continue
        if not _MANAGED_NAME_RE.search(entry):
            continue
        if entry in expected_names:
            continue
        os.remove(path)
        removed += 1
        log.info("prune %s", entry)
    return removed


def _managed_bytes_on_disk(output_dir: str) -> int:
    total = 0
    for entry in os.listdir(output_dir):
        path = os.path.join(output_dir, entry)
        if os.path.isfile(path) and _MANAGED_NAME_RE.search(entry):
            total += os.path.getsize(path)
    return total


def _disk_budget(output_dir: str, buffer_percent: float) -> tuple[int, int, int]:
    """Return (usable_for_our_album, current_managed_bytes, reserved_bytes).

    usable = existing_managed_bytes + max(0, free - reserved)
    reserved = total_capacity * buffer_percent / 100  — a floor on `free`
    the sync will never drop below, even when pruning to fit.
    """
    usage = shutil.disk_usage(output_dir)
    reserved = int(usage.total * buffer_percent / 100)
    free_headroom = max(0, usage.free - reserved)
    current = _managed_bytes_on_disk(output_dir)
    return current + free_headroom, current, reserved


def _choose_keep_set(candidates: list[_KeepItemT], budget_bytes: int) -> list[_KeepItemT]:
    """Greedy newest-first walk. Callers must pre-sort `candidates` so
    higher-priority (newer) entries appear first. Each candidate must
    carry a `size` key. Returns the prefix whose cumulative size fits
    under `budget_bytes`."""
    keep: list[_KeepItemT] = []
    used = 0
    for c in candidates:
        # Bound `Mapping[str, object]` widens `c["size"]` to `object`; every
        # caller writes size as an int (see the candidate builder in
        # orchestrator, and the fixture literals in tests), so casting here
        # is a pure type-level assertion, not a runtime coercion.
        size = cast(int, c["size"])
        if used + size > budget_bytes:
            break
        keep.append(c)
        used += size
    return keep
