#!/usr/bin/env python3
"""Unit tests for the adaptive poll cadence — CADENCE-01.

The daemon polls the (free) OAuth usage endpoint every POLL_INTERVAL_ACTIVE
seconds while usage is rising, POLL_INTERVAL when idle, and holds the idle
cadence for RATE_LIMIT_COOLDOWN after the endpoint returns a 429.

Run: python -m pytest daemon/tests/test_windows_cadence.py -x -q
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daemon.claude_usage_daemon_windows import (
    AuthError,
    RateLimited,
    connect_and_run,
    poll_interval,
    rate_limit_cooldown,
)


@pytest.fixture(autouse=True)
def _expiry_unknown(monkeypatch):
    """connect_and_run pre-flights token expiry via _read_expiry_ts(), which reads
    the REAL credentials file — patch it to 'unknown' so loop tests stay hermetic
    (an expired token on the dev box must not skip the mocked poll_api). Tests
    that exercise the pre-flight check re-patch it explicitly."""
    import daemon.claude_usage_daemon_windows as mod
    monkeypatch.setattr(mod, "_read_expiry_ts", lambda: None)


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


# ---------------------------------------------------------------------------
# Loop-level: a 401/403 must NOT be retried every tick either
# ---------------------------------------------------------------------------

def test_auth_error_poll_is_not_hammered(monkeypatch):
    """An AuthError poll counts as a full interval. The 2026-07-07 field log
    showed the old behavior (retry every TICK) firing six 401s in 30s, which
    drained the usage endpoint's rate-limit bucket and locked the daemon into
    a 401-burst -> 429-cooldown cycle for ~17 minutes."""
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
        raise AuthError(401)

    tick_count = [0]

    async def fake_wait_first(*_args, **_kwargs):
        tick_count[0] += 1
        if tick_count[0] >= 5:
            stop_event.set()

    tray_state = MagicMock()

    with patch("daemon.claude_usage_daemon_windows.BleakClient", return_value=mock_client), \
         patch("daemon.claude_usage_daemon_windows.Session", return_value=fake_session), \
         patch("daemon.claude_usage_daemon_windows.read_token", return_value="fake-token"), \
         patch("daemon.claude_usage_daemon_windows.poll_api", new=fake_poll), \
         patch("daemon.claude_usage_daemon_windows._wait_first", new=fake_wait_first):
        _run(connect_and_run(device, stop_event, tray_state=tray_state))

    assert poll_calls[0] == 1, f"Expected 1 poll attempt, got {poll_calls[0]}"
    assert tick_count[0] >= 5  # the loop kept ticking without re-polling
    tray_state.set_error.assert_called()  # 401 IS the actionable toast


# ---------------------------------------------------------------------------
# rate_limit_cooldown() — escalation + Retry-After
# ---------------------------------------------------------------------------

def test_cooldown_escalates_on_consecutive_429s():
    """No Retry-After: consecutive 429s double the base cooldown, capped."""
    import daemon.claude_usage_daemon_windows as mod
    assert rate_limit_cooldown(1, None) == mod.RATE_LIMIT_COOLDOWN
    assert rate_limit_cooldown(2, None) == mod.RATE_LIMIT_COOLDOWN * 2
    assert rate_limit_cooldown(3, None) == mod.RATE_LIMIT_COOLDOWN_MAX
    assert rate_limit_cooldown(7, None) == mod.RATE_LIMIT_COOLDOWN_MAX


def test_cooldown_honors_retry_after():
    """A server-sent Retry-After overrides the escalation schedule."""
    assert rate_limit_cooldown(1, 120.0) == 120.0
    assert rate_limit_cooldown(5, 42.0) == 42.0


# ---------------------------------------------------------------------------
# Pre-flight expiry check: an already-expired token never reaches the network
# ---------------------------------------------------------------------------

def test_expired_token_skips_poll_entirely(monkeypatch):
    """When the credentials file says the token is already expired, polling is a
    guaranteed 401 that still burns a request from the endpoint's rate-limit
    bucket — the loop must skip poll_api, toast the error once, and re-check at
    the idle cadence (not every TICK)."""
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
    monkeypatch.setattr(mod, "_read_expiry_ts", lambda: time.time() - 60)

    poll_mock = AsyncMock()

    tick_count = [0]

    async def fake_wait_first(*_args, **_kwargs):
        tick_count[0] += 1
        if tick_count[0] >= 5:
            stop_event.set()

    tray_state = MagicMock()

    with patch("daemon.claude_usage_daemon_windows.BleakClient", return_value=mock_client), \
         patch("daemon.claude_usage_daemon_windows.Session", return_value=fake_session), \
         patch("daemon.claude_usage_daemon_windows.read_token", return_value="fake-token"), \
         patch("daemon.claude_usage_daemon_windows.poll_api", new=poll_mock), \
         patch("daemon.claude_usage_daemon_windows._wait_first", new=fake_wait_first):
        _run(connect_and_run(device, stop_event, tray_state=tray_state))

    poll_mock.assert_not_awaited()
    assert tray_state.set_error.call_count == 1  # once per interval, not per tick


# ---------------------------------------------------------------------------
# Degraded payload: auth/429 outages mark the device's data stale (ok:false)
# ---------------------------------------------------------------------------

def test_stale_payload_sent_on_429_after_good_poll(monkeypatch):
    """After at least one good poll, an auth/429 failure resends the last-known
    values with ok:false so the device can show a STALE banner instead of
    sitting silently frozen on old numbers for the whole outage."""
    import daemon.claude_usage_daemon_windows as mod

    device = _make_device()
    stop_event = _run(_make_event(False))

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock(return_value=None)
    mock_client.is_connected = True
    mock_client.disconnect = AsyncMock()
    mock_client.start_notify = AsyncMock()

    writes = []

    fake_session = AsyncMock()
    fake_session.refresh_requested = MagicMock()
    fake_session.refresh_requested.is_set = MagicMock(return_value=False)
    fake_session.refresh_requested.clear = MagicMock()

    async def fake_write(payload):
        writes.append(dict(payload))
        return True

    fake_session.write_payload = fake_write

    # Poll on every tick so the second (rate-limited) poll happens immediately.
    monkeypatch.setattr(mod, "POLL_INTERVAL", 0)
    monkeypatch.setattr(mod, "POLL_INTERVAL_ACTIVE", 0)

    poll_calls = [0]

    async def fake_poll(_token):
        poll_calls[0] += 1
        if poll_calls[0] == 1:
            return {"s": 42, "sr": 10, "w": 5, "wr": 100, "st": "allowed",
                    "ok": True, "host": "T", "_raw_util": 0.42}
        raise RateLimited()

    async def fake_wait_first(*_args, **_kwargs):
        if len(writes) >= 2 or poll_calls[0] >= 3:
            stop_event.set()

    with patch("daemon.claude_usage_daemon_windows.BleakClient", return_value=mock_client), \
         patch("daemon.claude_usage_daemon_windows.Session", return_value=fake_session), \
         patch("daemon.claude_usage_daemon_windows.read_token", return_value="fake-token"), \
         patch("daemon.claude_usage_daemon_windows.poll_api", new=fake_poll), \
         patch("daemon.claude_usage_daemon_windows._wait_first", new=fake_wait_first):
        _run(connect_and_run(device, stop_event))

    assert len(writes) == 2, f"Expected good + stale writes, got {writes}"
    good, stale = writes
    assert good["ok"] is True
    assert stale["ok"] is False
    assert stale["active"] is False
    assert stale["s"] == good["s"]  # last-known values preserved
    assert stale["host"] == good["host"]


def test_no_stale_payload_without_prior_good_poll(monkeypatch):
    """With no last-known-good data, an auth failure sends nothing — zeros with
    ok:false would be worse than the device's own 'no data' screen."""
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
        raise AuthError(401)

    async def fake_wait_first(*_args, **_kwargs):
        stop_event.set()

    with patch("daemon.claude_usage_daemon_windows.BleakClient", return_value=mock_client), \
         patch("daemon.claude_usage_daemon_windows.Session", return_value=fake_session), \
         patch("daemon.claude_usage_daemon_windows.read_token", return_value="fake-token"), \
         patch("daemon.claude_usage_daemon_windows.poll_api", new=fake_poll), \
         patch("daemon.claude_usage_daemon_windows._wait_first", new=fake_wait_first):
        _run(connect_and_run(device, stop_event))

    fake_session.write_payload.assert_not_called()
