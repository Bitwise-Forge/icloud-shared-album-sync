"""Tests for icloud_sync.cli — process-level concerns:
_configure_logging, _env, _install_signal_handlers, the main() loop,
and STORAGE_BUFFER_PERCENT / AUTOPRUNE_ON_LOW_STORAGE parsing."""

import logging
import threading
from typing import ClassVar

import pytest

from icloud_sync import cli

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


@pytest.mark.parametrize(
    "level_name,expected",
    [
        ("DEBUG", logging.DEBUG),
        ("INFO", logging.INFO),
        ("WARNING", logging.WARNING),
        ("debug", logging.DEBUG),  # case-insensitive
    ],
)
def test_configure_logging_sets_level(level_name, expected, restore_root_logger):
    cli._configure_logging(level_name)
    assert logging.getLogger().level == expected


def test_configure_logging_defaults_to_info_on_unknown(restore_root_logger):
    cli._configure_logging("NOT-A-LEVEL")
    assert logging.getLogger().level == logging.INFO


# ---------- _env -----------------------------------------------------------


def test_env_returns_value_when_set(monkeypatch):
    monkeypatch.setenv("EXAMPLE_VAR", "hello")
    assert cli._env("EXAMPLE_VAR") == "hello"


def test_env_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("EXAMPLE_VAR", raising=False)
    assert cli._env("EXAMPLE_VAR", "fallback") == "fallback"


def test_env_exits_with_code_2_when_required_and_missing(monkeypatch):
    monkeypatch.delenv("EXAMPLE_VAR", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        cli._env("EXAMPLE_VAR", required=True)
    assert exc_info.value.code == 2


def test_env_treats_empty_string_as_missing_when_required(monkeypatch):
    monkeypatch.setenv("EXAMPLE_VAR", "")
    with pytest.raises(SystemExit) as exc_info:
        cli._env("EXAMPLE_VAR", required=True)
    assert exc_info.value.code == 2


# ---------- _install_signal_handlers ---------------------------------------


def test_install_signal_handlers_registers_sigterm_and_sigint(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli.signal,
        "signal",
        lambda sig, handler: calls.append((sig, handler)),
    )
    cli._install_signal_handlers(threading.Event())
    registered_signals = {sig for sig, _ in calls}
    assert registered_signals == {cli.signal.SIGTERM, cli.signal.SIGINT}


def test_install_signal_handler_sets_stop_event_when_invoked(monkeypatch):
    captured_handlers = {}
    monkeypatch.setattr(
        cli.signal,
        "signal",
        lambda sig, handler: captured_handlers.__setitem__(sig, handler),
    )
    stop = threading.Event()
    cli._install_signal_handlers(stop)
    assert not stop.is_set()
    # Invoke the SIGTERM handler as if the OS delivered the signal.
    captured_handlers[cli.signal.SIGTERM](cli.signal.SIGTERM, None)
    assert stop.is_set()


# ---------- main() ---------------------------------------------------------


def test_main_one_shot_calls_sync_album_once_and_returns_zero(main_env):
    calls = []
    main_env.setattr(
        cli,
        "sync_album",
        lambda url, output_dir, buffer_percent, autoprune_on_low_storage: calls.append(
            (url, output_dir, buffer_percent, autoprune_on_low_storage)
        ),
    )
    assert cli.main() == 0
    assert len(calls) == 1
    # main() should have parsed defaults: buffer=10, autoprune=False
    assert calls[0][2] == 10.0
    assert calls[0][3] is False


def test_main_exits_when_shared_album_url_missing(main_env):
    main_env.delenv("SHARED_ALBUM_URL", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 2


def test_main_returns_2_when_interval_is_not_numeric(main_env):
    main_env.setenv("SYNC_INTERVAL_HOURS", "not-a-number")
    main_env.setattr(cli, "sync_album", lambda *a, **kw: None)
    assert cli.main() == 2


def test_main_catches_and_logs_sync_exception(main_env, caplog):
    def failing(url, output_dir, buffer_percent, autoprune_on_low_storage):
        raise RuntimeError("boom")

    main_env.setattr(cli, "sync_album", failing)
    # _configure_logging calls basicConfig(force=True), which strips caplog's
    # handler. Patch it out for this test so caplog can observe the record.
    main_env.setattr(cli, "_configure_logging", lambda _level: None)
    caplog.set_level(logging.ERROR, logger="sync")
    assert cli.main() == 0  # one-shot mode: exception logged, main returns cleanly
    assert any("sync failed" in r.message for r in caplog.records)


def test_main_daemon_loop_exits_when_stop_wait_returns_true(main_env):
    """SYNC_INTERVAL_HOURS > 0 enters the daemon loop. If the stop event's
    wait() returns True (as it would after a signal during sleep), the loop
    logs 'stop requested during sleep' and exits after one sync."""
    main_env.setenv("SYNC_INTERVAL_HOURS", "12")

    class ImmediateStopEvent(threading.Event):
        def wait(self, timeout=None):
            return True  # pretend a signal fired during sleep

    main_env.setattr(cli.threading, "Event", ImmediateStopEvent)

    call_count = 0

    def fake_sync(url, output_dir, buffer_percent, autoprune_on_low_storage):
        nonlocal call_count
        call_count += 1

    main_env.setattr(cli, "sync_album", fake_sync)

    assert cli.main() == 0
    assert call_count == 1  # exactly one iteration before the wait bailed out


def test_main_daemon_loop_exits_when_stop_set_before_next_iteration(main_env):
    """If the stop event is set while sync_album is running, the loop
    should notice on the next `not stop.is_set()` check and exit before
    hitting the sleep."""
    main_env.setenv("SYNC_INTERVAL_HOURS", "12")

    stop_holder = {}
    original_event_cls = cli.threading.Event

    class CapturingEvent(original_event_cls):
        def __init__(self):
            super().__init__()
            stop_holder["event"] = self

    main_env.setattr(cli.threading, "Event", CapturingEvent)

    call_count = 0

    def fake_sync(url, output_dir, buffer_percent, autoprune_on_low_storage):
        nonlocal call_count
        call_count += 1
        stop_holder["event"].set()  # signal during sync

    main_env.setattr(cli, "sync_album", fake_sync)

    assert cli.main() == 0
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

    main_env.setattr(cli.threading, "Event", LoopThenStopEvent)

    call_count = 0

    def fake_sync(url, output_dir, buffer_percent, autoprune_on_low_storage):
        nonlocal call_count
        call_count += 1

    main_env.setattr(cli, "sync_album", fake_sync)

    assert cli.main() == 0
    assert call_count == 2  # ran twice: initial + one wake-up cycle


def test_main_daemon_loop_exits_via_while_condition(main_env):
    """Defensive branch: if stop is set between wait() returning False and
    the next `while not stop.is_set()` check (e.g. a signal handler fires
    during that gap), the loop exits via the while condition rather than
    via break. Contrived race, but the guard exists so we test it."""
    main_env.setenv("SYNC_INTERVAL_HOURS", "12")

    class WaitSetsAndReturnsFalse(threading.Event):
        def wait(self, timeout=None):
            self.set()  # simulate: signal fires in the sleep gap
            return False  # but wait() itself reports 'timed out'

    main_env.setattr(cli.threading, "Event", WaitSetsAndReturnsFalse)

    call_count = 0

    def fake_sync(url, output_dir, buffer_percent, autoprune_on_low_storage):
        nonlocal call_count
        call_count += 1

    main_env.setattr(cli, "sync_album", fake_sync)

    assert cli.main() == 0
    assert call_count == 1  # ran once, then while-check saw is_set()=True


# ---------- STORAGE_BUFFER_PERCENT / AUTOPRUNE_ON_LOW_STORAGE parsing ------


def test_main_parses_buffer_percent_default_10(main_env):
    seen = {}
    main_env.setattr(
        cli,
        "sync_album",
        lambda url, output_dir, bp, ap: seen.update(bp=bp, ap=ap),
    )
    cli.main()
    assert seen == {"bp": 10.0, "ap": False}


def test_main_accepts_float_buffer_and_rounds_to_two_dp(main_env):
    main_env.setenv("STORAGE_BUFFER_PERCENT", "7.2567")
    seen = {}
    main_env.setattr(
        cli,
        "sync_album",
        lambda url, output_dir, bp, ap: seen.update(bp=bp, ap=ap),
    )
    cli.main()
    assert seen["bp"] == 7.26


def test_main_rejects_non_numeric_buffer(main_env):
    main_env.setenv("STORAGE_BUFFER_PERCENT", "banana")
    main_env.setattr(cli, "sync_album", lambda *a, **kw: None)
    assert cli.main() == 2


@pytest.mark.parametrize("bad", ["100", "150", "-1"])
def test_main_rejects_out_of_range_buffer(main_env, bad):
    main_env.setenv("STORAGE_BUFFER_PERCENT", bad)
    main_env.setattr(cli, "sync_album", lambda *a, **kw: None)
    assert cli.main() == 2


@pytest.mark.parametrize(
    "raw,expected",
    [("true", True), ("TRUE", True), ("false", False), ("False", False)],
)
def test_main_accepts_case_insensitive_autoprune(main_env, raw, expected):
    main_env.setenv("AUTOPRUNE_ON_LOW_STORAGE", raw)
    seen = {}
    main_env.setattr(
        cli,
        "sync_album",
        lambda url, output_dir, bp, ap: seen.update(bp=bp, ap=ap),
    )
    cli.main()
    assert seen["ap"] is expected


@pytest.mark.parametrize("bad", ["yes", "no", "1", "0", "on"])
def test_main_rejects_non_boolean_autoprune(main_env, bad):
    main_env.setenv("AUTOPRUNE_ON_LOW_STORAGE", bad)
    main_env.setattr(cli, "sync_album", lambda *a, **kw: None)
    assert cli.main() == 2


# ---------- timeout / retry env parsing ------------------------------------


def test_main_configures_apple_api_with_defaults(main_env):
    seen = {}
    main_env.setattr(
        cli.apple_api,
        "configure",
        lambda api, dl: seen.update(api=api, dl=dl),
    )
    main_env.setattr(cli, "sync_album", lambda *a, **kw: None)
    cli.main()
    assert seen["api"] == cli.apple_api.DEFAULT_API_TIMEOUT_SECONDS
    assert seen["dl"] == cli.apple_api.DEFAULT_DOWNLOAD_TIMEOUT_SECONDS


def test_main_passes_overridden_timeouts_through(main_env):
    main_env.setenv("APPLE_API_TIMEOUT_SECONDS", "300")
    main_env.setenv("DOWNLOAD_TIMEOUT_SECONDS", "90")
    seen = {}
    main_env.setattr(
        cli.apple_api,
        "configure",
        lambda api, dl: seen.update(api=api, dl=dl),
    )
    main_env.setattr(cli, "sync_album", lambda *a, **kw: None)
    cli.main()
    assert seen == {"api": 300.0, "dl": 90.0}


@pytest.mark.parametrize(
    "var",
    ["APPLE_API_TIMEOUT_SECONDS", "DOWNLOAD_TIMEOUT_SECONDS", "RETRY_SLEEP_MINUTES"],
)
@pytest.mark.parametrize("bad", ["banana", "0", "-5"])
def test_main_rejects_bad_timeout_values(main_env, var, bad):
    main_env.setenv(var, bad)
    main_env.setattr(cli, "sync_album", lambda *a, **kw: None)
    assert cli.main() == 2


def test_positive_float_returns_parsed_value(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TIMEOUT", "12.5")
    assert cli._positive_float("EXAMPLE_TIMEOUT", "1") == 12.5


def test_positive_float_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("EXAMPLE_TIMEOUT", raising=False)
    assert cli._positive_float("EXAMPLE_TIMEOUT", "8") == 8.0


# ---------- failed-run backoff ---------------------------------------------


class _RecordingWaitEvent(threading.Event):
    """Captures every sleep duration the loop asks for, then stops the
    loop after a fixed number of cycles. State is class-level because
    cli.main constructs the Event itself — the test never holds the
    instance and can only reach the recording through the class."""

    waits: ClassVar[list] = []
    stop_after: ClassVar[int] = 1

    def __init__(self):
        super().__init__()
        type(self).waits = []

    def wait(self, timeout=None):
        type(self).waits.append(timeout)
        return len(type(self).waits) >= type(self).stop_after


def test_failed_run_sleeps_the_retry_interval_not_the_full_one(main_env, caplog):
    """An hourly frame shouldn't lose a full hour to one bad cycle."""
    main_env.setenv("SYNC_INTERVAL_HOURS", "1")
    main_env.setenv("RETRY_SLEEP_MINUTES", "5")
    _RecordingWaitEvent.stop_after = 1
    main_env.setattr(cli.threading, "Event", _RecordingWaitEvent)
    main_env.setattr(cli, "_configure_logging", lambda _level: None)

    def failing(*_a, **_kw):
        raise RuntimeError("boom")

    main_env.setattr(cli, "sync_album", failing)
    caplog.set_level(logging.WARNING, logger="sync")

    assert cli.main() == 0
    assert _RecordingWaitEvent.waits == [300.0]  # 5 minutes, not 3600s
    assert any("retrying in 5.0min (failure 1)" in r.getMessage() for r in caplog.records)


def test_consecutive_failures_back_off_exponentially_and_cap_at_the_interval(main_env):
    """Backoff has to stop hammering Apple on a sustained outage, and must
    never wait longer than the normal interval would have."""
    main_env.setenv("SYNC_INTERVAL_HOURS", "1")  # 3600s cap
    main_env.setenv("RETRY_SLEEP_MINUTES", "5")
    _RecordingWaitEvent.stop_after = 5
    main_env.setattr(cli.threading, "Event", _RecordingWaitEvent)
    main_env.setattr(cli, "_configure_logging", lambda _level: None)

    def failing(*_a, **_kw):
        raise RuntimeError("boom")

    main_env.setattr(cli, "sync_album", failing)
    assert cli.main() == 0
    # 5, 10, 20, 40 minutes... then capped by the 1h interval.
    assert _RecordingWaitEvent.waits == [300.0, 600.0, 1200.0, 2400.0, 3600.0]


def test_a_successful_run_resets_the_failure_counter(main_env):
    """Recovery should restore the normal cadence immediately rather than
    leaving the frame stuck on a long backoff."""
    main_env.setenv("SYNC_INTERVAL_HOURS", "1")
    main_env.setenv("RETRY_SLEEP_MINUTES", "5")
    _RecordingWaitEvent.stop_after = 3
    main_env.setattr(cli.threading, "Event", _RecordingWaitEvent)
    main_env.setattr(cli, "_configure_logging", lambda _level: None)

    calls = {"n": 0}

    def sometimes_failing(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] in (1, 3):
            raise RuntimeError("boom")

    main_env.setattr(cli, "sync_album", sometimes_failing)
    assert cli.main() == 0
    # fail -> 5min, succeed -> full hour, fail again -> back to 5min (not 10).
    assert _RecordingWaitEvent.waits == [300.0, 3600.0, 300.0]
