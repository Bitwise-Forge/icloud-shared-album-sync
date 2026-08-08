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

from . import apple_api
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


def _positive_float(name: str, default: str) -> float | None:
    """Parse an env var that must be a number greater than zero.

    Returns None on invalid input, having already logged the reason —
    callers turn that into exit code 2. Zero is rejected rather than
    treated as 'disabled' because every knob using this would mean
    'never wait at all', which is never what an operator wants.
    """
    raw = _env(name, default)
    try:
        val = float(raw)
    except ValueError:
        log.error("%s must be numeric, got %r", name, raw)
        return None
    if val <= 0:
        log.error("%s must be greater than 0, got %s", name, val)
        return None
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

    api_timeout = _positive_float(
        "APPLE_API_TIMEOUT_SECONDS", str(apple_api.DEFAULT_API_TIMEOUT_SECONDS)
    )
    if api_timeout is None:
        return 2

    download_timeout = _positive_float(
        "DOWNLOAD_TIMEOUT_SECONDS", str(apple_api.DEFAULT_DOWNLOAD_TIMEOUT_SECONDS)
    )
    if download_timeout is None:
        return 2

    retry_sleep_minutes = _positive_float("RETRY_SLEEP_MINUTES", "5")
    if retry_sleep_minutes is None:
        return 2

    apple_api.configure(api_timeout, download_timeout)

    log.info(
        "config buffer_percent=%s autoprune_on_low_storage=%s "
        "api_timeout=%.1fs download_timeout=%.1fs retry_sleep=%.1fmin",
        buffer_percent,
        autoprune_on_low_storage,
        api_timeout,
        download_timeout,
        retry_sleep_minutes,
    )

    stop = threading.Event()
    _install_signal_handlers(stop)

    consecutive_failures = 0
    while not stop.is_set():
        try:
            sync_album(url, output_dir, buffer_percent, autoprune_on_low_storage)
            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            log.exception("sync failed (consecutive failures: %d)", consecutive_failures)
        if interval_hours <= 0:
            break
        interval_seconds = interval_hours * 3600
        if consecutive_failures:
            # A failed cycle shouldn't cost a full interval — on an hourly
            # frame that turns a transient blip into an hour of stale
            # photos. Back off exponentially so a persistent outage stops
            # hammering Apple, and never wait longer than the configured
            # interval would have.
            sleep_seconds = min(
                retry_sleep_minutes * 60 * 2 ** (consecutive_failures - 1),
                interval_seconds,
            )
            log.warning(
                "retrying in %.1fmin (failure %d) instead of the full %.2fh interval",
                sleep_seconds / 60,
                consecutive_failures,
                interval_hours,
            )
        else:
            sleep_seconds = interval_seconds
            log.info("sleeping %.2fh until next sync", interval_hours)
        if stop.wait(sleep_seconds):
            log.info("stop requested during sleep, exiting")
            break

    return 0
