#!/usr/bin/env python3
"""Unit tests for the adaptive poll cadence — CADENCE-01.

The daemon polls the (free) OAuth usage endpoint every POLL_INTERVAL_ACTIVE
seconds while usage is rising, POLL_INTERVAL when idle, and holds the idle
cadence for RATE_LIMIT_COOLDOWN after the endpoint returns a 429.

Run: python -m pytest daemon/tests/test_windows_cadence.py -x -q
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from daemon.claude_usage_daemon_windows import RateLimited, connect_and_run, poll_interval


# ---------------------------------------------------------------------------
# Helpers (same shapes as test_windows_reconnect.py)
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_device(address="AA:BB:CC:DD:EE:FF"):
    device = MagicMock()
    device.address = address
    return device


async def _make_event(set_):
    ev = asyncio.Event()
    if set_:
        ev.set()
    return ev


# ---------------------------------------------------------------------------
# poll_interval() — the cadence choice itself
# ---------------------------------------------------------------------------

def test_idle_uses_slow_interval():
    import daemon.claude_usage_daemon_windows as mod
    assert poll_interval(False, 1000.0, 0.0) == mod.POLL_INTERVAL


def test_active_uses_fast_interval():
    import daemon.claude_usage_daemon_windows as mod
    assert poll_interval(True, 1000.0, 0.0) == mod.POLL_INTERVAL_ACTIVE
    assert mod.POLL_INTERVAL_ACTIVE < mod.POLL_INTERVAL


def test_rate_limit_cooldown_overrides_active():
    """During the 429 cooldown, even active usage polls at the idle cadence."""
    import daemon.claude_usage_daemon_windows as mod
    now = 1000.0
    assert poll_interval(True, now, now + 100) == mod.POLL_INTERVAL


def test_cooldown_expiry_restores_fast_interval():
    """Once the cooldown has passed, active usage returns to the fast cadence."""
    import daemon.claude_usage_daemon_windows as mod
    now = 1000.0
    assert poll_interval(True, now, now - 1) == mod.POLL_INTERVAL_ACTIVE


def test_macos_daemon_cadence_matches_windows():
    """Both Python daemons implement the same cadence rules."""
    import daemon.claude_usage_daemon as mac
    import daemon.claude_usage_daemon_windows as win
    assert mac.POLL_INTERVAL == win.POLL_INTERVAL
    assert mac.POLL_INTERVAL_ACTIVE == win.POLL_INTERVAL_ACTIVE
    assert mac.RATE_LIMIT_COOLDOWN == win.RATE_LIMIT_COOLDOWN
    for active in (False, True):
        for until in (0.0, 2000.0):
            assert mac.poll_interval(active, 1000.0, until) == win.poll_interval(
                active, 1000.0, until
            )


# ---------------------------------------------------------------------------
# Loop-level: a 429 must NOT be retried every tick
# ---------------------------------------------------------------------------

def test_rate_limited_poll_is_not_hammered(monkeypatch):
    """A RateLimited poll counts as a full interval: with POLL_INTERVAL large,
    the loop must attempt exactly ONE poll and then sit quietly through many
    ticks (the old behavior for failed polls was retry-every-TICK, which would
    hammer a rate-limited endpoint)."""
    import daemon.claude_usage_daemon_windows as mod

    device = _make_device()
    stop_event = _run(_make_event(False))

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock(return_value=None)
    mock_client.is_connected = True
    mock_client.disconnect = AsyncMock()
    mock_client.start_notify = AsyncMock()

    fake_session = AsyncMock()
    fake_session.refresh_requested = MagicMock()
    fake_session.refresh_requested.is_set = MagicMock(return_value=False)
    fake_session.refresh_requested.clear = MagicMock()

    monkeypatch.setattr(mod, "POLL_INTERVAL", 1000)

    poll_calls = [0]

    async def fake_poll(_token):
        poll_calls[0] += 1
        raise RateLimited()

    tick_count = [0]

    async def fake_wait_first(*_args, **_kwargs):
        tick_count[0] += 1
        if tick_count[0] >= 5:
            stop_event.set()

    with patch("daemon.claude_usage_daemon_windows.BleakClient", return_value=mock_client), \
         patch("daemon.claude_usage_daemon_windows.Session", return_value=fake_session), \
         patch("daemon.claude_usage_daemon_windows.read_token", return_value="fake-token"), \
         patch("daemon.claude_usage_daemon_windows.poll_api", new=fake_poll), \
         patch("daemon.claude_usage_daemon_windows._wait_first", new=fake_wait_first):
        _run(connect_and_run(device, stop_event))

    assert poll_calls[0] == 1, f"Expected 1 poll attempt, got {poll_calls[0]}"
    assert tick_count[0] >= 5  # the loop kept ticking without re-polling


def test_rate_limited_does_not_set_error_state(monkeypatch):
    """RateLimited is transient — it must not fire the 'token expired' toast."""
    import daemon.claude_usage_daemon_windows as mod

    device = _make_device()
    stop_event = _run(_make_event(False))

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock(return_value=None)
    mock_client.is_connected = True
    mock_client.disconnect = AsyncMock()
    mock_client.start_notify = AsyncMock()

    fake_session = AsyncMock()
    fake_session.refresh_requested = MagicMock()
    fake_session.refresh_requested.is_set = MagicMock(return_value=False)
    fake_session.refresh_requested.clear = MagicMock()

    monkeypatch.setattr(mod, "POLL_INTERVAL", 1000)

    async def fake_poll(_token):
        raise RateLimited()

    async def fake_wait_first(*_args, **_kwargs):
        stop_event.set()

    tray_state = MagicMock()

    with patch("daemon.claude_usage_daemon_windows.BleakClient", return_value=mock_client), \
         patch("daemon.claude_usage_daemon_windows.Session", return_value=fake_session), \
         patch("daemon.claude_usage_daemon_windows.read_token", return_value="fake-token"), \
         patch("daemon.claude_usage_daemon_windows.poll_api", new=fake_poll), \
         patch("daemon.claude_usage_daemon_windows._wait_first", new=fake_wait_first):
        _run(connect_and_run(device, stop_event, tray_state=tray_state))

    tray_state.set_error.assert_not_called()
