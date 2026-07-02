#!/usr/bin/env python3
"""Unit tests for poll_api / pct / reset_minutes / JSON-shape — POLL-01.

poll_api GETs the OAuth usage endpoint (https://api.anthropic.com/api/oauth/usage
— the same endpoint Claude Code's /usage command calls, zero token cost) and maps
the JSON body onto the compact wire payload the ESP32 expects.
All tests mock httpx so no real network calls are made.

Run: python -m pytest daemon/tests/test_windows_poll.py -x -q
"""
import asyncio
import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daemon.claude_usage_daemon_windows import AuthError, RateLimited, poll_api


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso_in(seconds: float) -> str:
    """ISO 8601 UTC timestamp `seconds` from now — the endpoint's resets_at format."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=seconds)
    ).isoformat()


def _usage_body(
    five_util=42.0,
    five_reset_s=3600,
    seven_util=10.0,
    seven_reset_s=86400,
):
    """A realistic /api/oauth/usage response body (utilization is 0-100)."""
    return {
        "five_hour": {
            "utilization": five_util,
            "resets_at": _iso_in(five_reset_s) if five_reset_s is not None else None,
        },
        "seven_day": {
            "utilization": seven_util,
            "resets_at": _iso_in(seven_reset_s) if seven_reset_s is not None else None,
        },
        "seven_day_opus": None,
        "extra_usage": {"is_enabled": True},
    }


def _make_mock_response(status_code=200, body=None, text="mocked"):
    """Build a mock httpx.Response-like object with a controllable JSON body.

    body=None simulates a non-JSON response (resp.json() raises ValueError).
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if body is None:
        resp.json = MagicMock(side_effect=ValueError("not json"))
    else:
        resp.json = MagicMock(return_value=body)
    return resp


def _client_for(resp=None, side_effect=None):
    """Mock httpx.AsyncClient whose .get() returns resp (or raises side_effect)."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    if side_effect is not None:
        mock_client.get = AsyncMock(side_effect=side_effect)
    else:
        mock_client.get = AsyncMock(return_value=resp)
    return mock_client


def _run(coro):
    """Run a coroutine synchronously for synchronous test functions."""
    return asyncio.run(coro)


def _poll_with(resp=None, side_effect=None, token="fake-token"):
    with patch("httpx.AsyncClient", return_value=_client_for(resp, side_effect)):
        return _run(poll_api(token))


# ---------------------------------------------------------------------------
# Test: full poll_api with a realistic usage body
# ---------------------------------------------------------------------------

def test_poll_api_nominal():
    """poll_api with a 200 usage body produces the correct wire payload."""
    resp = _make_mock_response(body=_usage_body())
    payload = _poll_with(resp)

    assert payload is not None
    assert payload["s"] == 42
    assert payload["w"] == 10
    assert payload["st"] == "allowed"
    assert payload["ok"] is True
    # reset_minutes allows ±1 minute tolerance
    assert abs(payload["sr"] - 60) <= 1, f"Expected ~60, got {payload['sr']}"
    assert abs(payload["wr"] - 1440) <= 1, f"Expected ~1440, got {payload['wr']}"


def test_poll_api_uses_get_not_post():
    """The usage endpoint is a GET — a POST would 404 (and the old POST body
    was a billed Haiku message; this guards against regressing to it)."""
    resp = _make_mock_response(body=_usage_body())
    client = _client_for(resp)
    with patch("httpx.AsyncClient", return_value=client):
        _run(poll_api("fake-token"))
    client.get.assert_awaited_once()
    assert not client.post.called


# ---------------------------------------------------------------------------
# Test: pct() correctness — utilization is already a 0-100 percentage
# ---------------------------------------------------------------------------

def test_pct_42_percent():
    """utilization 42.0 -> s == 42 (no re-scaling of the 0-100 value)."""
    payload = _poll_with(_make_mock_response(body=_usage_body(five_util=42.0)))
    assert payload["s"] == 42


def test_pct_100_percent_flips_status_to_limited():
    """utilization 100 -> s == 100 and st == 'limited'."""
    payload = _poll_with(
        _make_mock_response(body=_usage_body(five_util=100.0, seven_util=100.0))
    )
    assert payload["s"] == 100
    assert payload["w"] == 100
    assert payload["st"] == "limited"


def test_pct_null_utilization_defaults_to_zero():
    """utilization null/missing -> 0."""
    payload = _poll_with(
        _make_mock_response(body=_usage_body(five_util=None, seven_util=None))
    )
    assert payload["s"] == 0
    assert payload["w"] == 0


def test_raw_util_is_0_to_1_fraction():
    """_raw_util must stay on the 0-1 scale ACTIVE_THRESHOLD was tuned for."""
    payload = _poll_with(_make_mock_response(body=_usage_body(five_util=42.0)))
    assert payload["_raw_util"] == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# Test: reset_minutes() — ISO 8601 resets_at
# ---------------------------------------------------------------------------

def test_reset_minutes_60_minutes():
    """resets_at 3600s from now -> ~60."""
    payload = _poll_with(_make_mock_response(body=_usage_body(five_reset_s=3600)))
    assert abs(payload["sr"] - 60) <= 1, f"Expected ~60, got {payload['sr']}"


def test_reset_minutes_negative_clamps_to_zero():
    """resets_at in the past clamps to 0."""
    payload = _poll_with(
        _make_mock_response(body=_usage_body(five_reset_s=-100, seven_reset_s=-100))
    )
    assert payload["sr"] == 0
    assert payload["wr"] == 0


def test_reset_minutes_invalid_string_returns_zero():
    """resets_at 'notatimestamp' -> 0 (ValueError-safe)."""
    body = _usage_body()
    body["five_hour"]["resets_at"] = "notatimestamp"
    body["seven_day"]["resets_at"] = "notatimestamp"
    payload = _poll_with(_make_mock_response(body=body))
    assert payload["sr"] == 0
    assert payload["wr"] == 0


def test_reset_minutes_accepts_zulu_suffix():
    """resets_at with a 'Z' suffix parses (fromisoformat pre-3.11 compat shim)."""
    body = _usage_body()
    body["five_hour"]["resets_at"] = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=3600)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = _poll_with(_make_mock_response(body=body))
    assert abs(payload["sr"] - 60) <= 1, f"Expected ~60, got {payload['sr']}"


# ---------------------------------------------------------------------------
# Test: missing/odd windows default gracefully
# ---------------------------------------------------------------------------

def test_missing_windows_default_to_zero():
    """A body without five_hour/seven_day objects produces zeros, not a crash."""
    payload = _poll_with(_make_mock_response(body={"five_hour": None}))
    assert payload["s"] == 0
    assert payload["w"] == 0
    assert payload["sr"] == 0
    assert payload["wr"] == 0
    assert payload["st"] == "allowed"


def test_non_json_body_returns_none():
    """A 200 with a non-JSON body (proxy error page etc.) is transient -> None."""
    resp = _make_mock_response(body=None, text="<html>gateway error</html>")
    assert _poll_with(resp) is None


def test_non_dict_json_returns_none():
    """A 200 whose JSON is not an object is transient -> None."""
    resp = _make_mock_response(body=["unexpected", "array"])
    assert _poll_with(resp) is None


# ---------------------------------------------------------------------------
# Test: poll_api returns None on HTTP >= 400
# ---------------------------------------------------------------------------

def test_poll_api_returns_none_on_4xx():
    """poll_api returns None when response status code is >= 400 (except 401/403/429)."""
    assert _poll_with(_make_mock_response(status_code=404)) is None


def test_poll_api_returns_none_on_5xx():
    """poll_api returns None when response status code is >= 500."""
    assert _poll_with(_make_mock_response(status_code=500)) is None


# ---------------------------------------------------------------------------
# Test: poll_api raises AuthError ONLY on a genuine 401/403 (SC#5 fix)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
def test_poll_api_raises_autherror_on_401_403(status):
    """A real auth rejection must raise AuthError — the only signal that warrants
    the actionable 'token expired — run claude login' toast. Transient failures
    (5xx, 429, network) return None instead and must NOT trigger that toast."""
    with pytest.raises(AuthError):
        _poll_with(_make_mock_response(status_code=status))


def test_poll_api_raises_ratelimited_on_429():
    """A 429 raises RateLimited so the poll loop can back off to the idle
    cadence — and it must NOT be an AuthError (no 'token expired' toast)."""
    with pytest.raises(RateLimited):
        _poll_with(_make_mock_response(status_code=429))
    assert not issubclass(RateLimited, AuthError)


# ---------------------------------------------------------------------------
# Test: poll_api returns None on httpx.HTTPError
# ---------------------------------------------------------------------------

def test_poll_api_returns_none_on_http_error():
    """poll_api returns None when httpx.HTTPError is raised (network failure)."""
    import httpx

    assert _poll_with(side_effect=httpx.ConnectError("Connection refused")) is None


# ---------------------------------------------------------------------------
# Test: compact JSON wire shape (no spaces after ':' or ',')
# ---------------------------------------------------------------------------

def test_wire_bytes_compact_json_shape():
    """The JSON-encoded payload uses compact separators (',':') — no spaces."""
    payload = _poll_with(_make_mock_response(body=_usage_body()))

    assert payload is not None
    # Encode exactly as the wire layer will (Session.write_payload uses this form)
    wire_bytes = json.dumps(payload, separators=(",", ":")).encode()
    wire_str = wire_bytes.decode()

    # Compact form: no space after ':' or ','
    assert ": " not in wire_str, f"Non-compact JSON detected: {wire_str!r}"
    assert ", " not in wire_str, f"Non-compact JSON detected: {wire_str!r}"

    # Must start with '{' and contain all required keys
    assert wire_str.startswith("{")
    for key in ("s", "sr", "w", "wr", "st", "ok"):
        assert f'"{key}"' in wire_str, f"Missing key {key!r} in wire bytes: {wire_str!r}"


# ---------------------------------------------------------------------------
# Test: token is NOT logged (T-02-01 threat mitigation)
# ---------------------------------------------------------------------------

def test_poll_api_does_not_log_token(capsys):
    """poll_api must not print the bearer token (T-02-01: token never logged)."""
    secret_token = "sk-ant-secret-token-12345"
    _poll_with(_make_mock_response(body=_usage_body()), token=secret_token)

    captured = capsys.readouterr()
    assert secret_token not in captured.out, "Token leaked to stdout (T-02-01 violation)"
    assert secret_token not in captured.err, "Token leaked to stderr (T-02-01 violation)"
