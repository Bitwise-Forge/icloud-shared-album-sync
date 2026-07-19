"""Process-level concerns: env parsing, signal handling, main loop.

The entry point (`main`) reads env, validates config, installs signal
handlers, and drives the sync loop. Everything downstream is in
`orchestrator.sync_album`.
"""

import logging
import os
import signal
import sys
import threading

from .orchestrator import sync_album

log = logging.getLogger("sync")


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        force=True,
    )
    # Silence httpx's per-request INFO chatter. Two reasons: it's noisy,
    # and it logs signed CDN URLs that stay valid for ~3 hours — bad thing
    # to leak into shared logs. Users who want the detail can override.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _env(name: str, default=None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        log.error("missing required env var: %s", name)
        sys.exit(2)
    return val


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def _handler(signum, _frame):
        log.info("received signal %d, will stop after current run", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main() -> int:
    _configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

    url = _env("SHARED_ALBUM_URL", required=True)
    output_dir = _env("OUTPUT_DIR", "/photos")
    interval_raw = _env("SYNC_INTERVAL_HOURS", "0")
    try:
        interval_hours = float(interval_raw)
    except ValueError:
        log.error("SYNC_INTERVAL_HOURS must be numeric, got %r", interval_raw)
        return 2

    buffer_raw = _env("STORAGE_BUFFER_PERCENT", "10")
    try:
        # Two-decimal cap keeps operator inputs like "7.25" honest and avoids
        # meaningless precision propagating into log lines.
        buffer_percent = round(float(buffer_raw), 2)
    except ValueError:
        log.error("STORAGE_BUFFER_PERCENT must be numeric, got %r", buffer_raw)
        return 2
    if not 0 <= buffer_percent < 100:
        log.error("STORAGE_BUFFER_PERCENT must be in [0, 100), got %s", buffer_percent)
        return 2

    autoprune_raw = _env("AUTOPRUNE_ON_LOW_STORAGE", "false").strip().lower()
    if autoprune_raw not in ("true", "false"):
        log.error(
            "AUTOPRUNE_ON_LOW_STORAGE must be 'true' or 'false', got %r",
            autoprune_raw,
        )
        return 2
    autoprune_on_low_storage = autoprune_raw == "true"

    log.info(
        "config buffer_percent=%s autoprune_on_low_storage=%s",
        buffer_percent,
        autoprune_on_low_storage,
    )

    stop = threading.Event()
    _install_signal_handlers(stop)

    while not stop.is_set():
        try:
            sync_album(url, output_dir, buffer_percent, autoprune_on_low_storage)
        except Exception:
            log.exception("sync failed")
        if interval_hours <= 0:
            break
        interval_seconds = interval_hours * 3600
        log.info("sleeping %.2fh until next sync", interval_hours)
        if stop.wait(interval_seconds):
            log.info("stop requested during sleep, exiting")
            break

    return 0
