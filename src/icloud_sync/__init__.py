"""icloud-shared-album-sync — pull public iCloud shared albums to disk.

Public API is `main`, invoked as `python -m icloud_sync`. Submodules
(`apple_api`, `manifest`, `storage`, `orchestrator`, `cli`) are treated
as implementation detail; import them directly when you need to.
"""

from .cli import main
from .orchestrator import sync_album

__all__ = ["main", "sync_album"]
