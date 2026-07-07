#!/usr/bin/env python3
"""Claude Usage Tracker Daemon — Windows (Phase 2).

Reads the Claude OAuth token from the native-Windows credentials path and
polls the Anthropic API for rate-limit utilization data. BLE glue added in
later plans.
"""

import asyncio
import datetime
import email.utils
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

import socket

import httpx
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

DEVICE_NAME = "Clawdmeter"
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"

HOSTNAME = socket.gethostname()

POLL_INTERVAL = 60          # idle cadence: usage isn't rising
POLL_INTERVAL_ACTIVE = 45   # fast cadence: usage rose on the last poll. Measured: the
                            # endpoint's limiter is ~a bucket of 8 requests refilling over
                            # 4-5 min (~1.6 req/min sustained), so 30s (2/min) trips it
                            # under continuous use; 45s (1.33/min) stays under with
                            # headroom. The value only steps in whole percents ~1x/min,
                            # so 45s still catches every step.
RATE_LIMIT_COOLDOWN = 300   # resume fast polling this long after the LAST 429 — a 429
                            # during cooldown (on a 60s poll) re-arms it, so short is safe.
                            # Measured penalty: locked at T+3 min, clear by T+5.
RATE_LIMIT_COOLDOWN_MAX = 900  # cap for the escalating cooldown: consecutive 429s double
                               # the base cooldown (300 -> 600 -> 900) so a persistent
                               # drain (whatever the cause) stops re-draining the bucket.
TICK = 5
SCAN_TIMEOUT = 8.0

ACTIVE_THRESHOLD = 0.001   # min per-poll rise in 5h utilization (0-1) to count as active.
                           # The usage endpoint reports whole percents, so any real rise is ≥0.01.
CONNECT_RETRIES = 3        # D-01: attempts before giving up on a device
CONNECT_RETRY_DELAY = 2.0  # D-01: seconds between failed connect attempts
ZOMBIE_BREAK_LIMIT = 1     # D-03: consecutive write failures before abandoning a half-open link
                           # N=1: breaks at T=60s, leaves ~60s headroom for reconnect+poll inside 120s SLA
                           # N=2 would bust the 120s budget before reconnect even begins
RECONNECT_BACKOFF_CAP = 8  # D-05: fast-reconnect cap (seconds); keeps stacked retries inside 120s SLA
                           # ~5–10s band per CONTEXT.md Claude's Discretion; 8 chosen as middle ground

# The OAuth usage endpoint (what Claude Code's /usage command calls). Returns
# utilization/reset data directly and consumes ZERO tokens — unlike the old
# approach of sending a billed 1-token Haiku message just to scrape the
# rate-limit headers off the response.
API_URL = "https://api.anthropic.com/api/oauth/usage"
API_HEADERS_TEMPLATE = {
    "anthropic-beta": "oauth-2025-04-20",
    # Required: without a claude-code User-Agent this endpoint lands in an
    # aggressively rate-limited bucket and returns persistent 429s.
    "User-Agent": "claude-code/2.1.5",
}


def _build_file_logger() -> logging.Logger | None:
    """Create a rotating file logger for field diagnostics, or None.

    Autostart launches the tray under pythonw.exe, which has no console — stdout
    is discarded (and is in fact None, making print() unsafe). A rotating file is
    then the ONLY trail when the daemon stalls in the field. Windows-only: on the
    Linux dev box / CI the console print() suffices, and gating to win32 keeps the
    pure-helper unit tests from writing stray log files.
    """
    if sys.platform != "win32":
        return None
    logger = logging.getLogger("clawdmeter.daemon")
    if logger.handlers:
        return logger  # idempotent across re-import (tray imports this module)
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / "Clawdmeter" / "daemon.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
        )
    except OSError:
        return None  # best-effort — logging setup must never stop the daemon
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


_FILE_LOGGER = _build_file_logger()


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    # Under pythonw sys.stdout is None and print() would raise — guard it so a
    # missing console can never crash the daemon thread (the silent-freeze mode).
    try:
        print(line, flush=True)
    except (OSError, ValueError, AttributeError, RuntimeError):
        pass
    if _FILE_LOGGER is not None:
        _FILE_LOGGER.info(msg)


class AuthError(Exception):
    """Raised by poll_api on a genuine 401/403 — the token really is expired or
    invalid and the user must re-run `claude login`. Distinct from a None return,
    which means a TRANSIENT failure (network/DNS, timeout, 5xx) that must NOT be
    mislabeled as a token problem (SC#5: a boot-time `getaddrinfo failed` DNS
    blip wrongly fired the 'token expired' toast)."""


class RateLimited(Exception):
    """Raised by poll_api on a 429 — the usage endpoint is rate-limiting this
    token. The poll loop reacts by holding the idle cadence (POLL_INTERVAL) for
    a cooldown instead of retrying every tick. Carries the server's Retry-After
    (seconds) when the response included one, else None."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after


def _parse_retry_after(value) -> float | None:
    """Parse a Retry-After header value: delta-seconds or HTTP-date.

    Returns positive seconds, or None when the header is absent/expired/bogus.
    Capped at 1h so a bogus header can't stall polling for hours.
    """
    if not value:
        return None
    try:
        secs = float(value)
    except (TypeError, ValueError):
        try:
            dt = email.utils.parsedate_to_datetime(str(value))
        except (TypeError, ValueError):
            return None
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        secs = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    if secs <= 0:
        return None
    return min(secs, 3600.0)


def rate_limit_cooldown(strikes: int, retry_after: float | None) -> float:
    """Cooldown after the Nth CONSECUTIVE 429 (strikes >= 1).

    Honor the server's Retry-After when it sent one; otherwise escalate the
    base cooldown (300 -> 600 -> 900 cap). The fixed 5-min re-arm let a
    persistent drain re-drain the bucket forever (2026-07-07 field log: 17 min
    locked out). Pure helper — unit-testable without driving the loop.
    """
    if retry_after is not None:
        return retry_after
    return float(min(RATE_LIMIT_COOLDOWN * (2 ** max(strikes - 1, 0)), RATE_LIMIT_COOLDOWN_MAX))


def poll_interval(active: bool, now: float, rate_limited_until: float) -> float:
    """Adaptive cadence: fast while usage is rising, idle otherwise — and idle
    unconditionally during a 429 cooldown."""
    if active and now >= rate_limited_until:
        return POLL_INTERVAL_ACTIVE
    return POLL_INTERVAL


async def poll_api(token: str) -> dict | None:
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.get(API_URL, headers=headers)
    except httpx.HTTPError as e:
        # Network/DNS/timeout — transient. Return None (no toast), retry next tick.
        log(f"API call failed: {e}")
        return None
    if resp.status_code in (401, 403):
        # Genuine auth rejection — the ONLY case that warrants the actionable
        # "run claude login" toast.
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        raise AuthError(resp.status_code)
    if resp.status_code == 429:
        # The usage endpoint has its own per-token rate limit — back off,
        # honoring the server's Retry-After if it sent one.
        retry_after = _parse_retry_after(resp.headers.get("retry-after"))
        log(f"API HTTP 429 (rate limited): {resp.text[:200]}")
        raise RateLimited(retry_after)
    if resp.status_code >= 400:
        # Other 4xx/5xx (server error etc.) — transient, not a token issue.
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    try:
        data = resp.json()
    except ValueError:
        log(f"API returned non-JSON body: {resp.text[:200]}")
        return None
    if not isinstance(data, dict):
        log(f"API returned unexpected JSON shape: {str(data)[:200]}")
        return None

    def window(name: str) -> dict:
        w = data.get(name)
        return w if isinstance(w, dict) else {}

    # utilization is already a 0-100 percentage on this endpoint
    def pct(util) -> int:
        try:
            return int(round(float(util)))
        except (TypeError, ValueError):
            return 0

    def raw(util) -> float:
        # 0-1 fraction, matching the scale ACTIVE_THRESHOLD was tuned for
        try:
            return float(util) / 100.0
        except (TypeError, ValueError):
            return 0.0

    def reset_minutes(reset_ts) -> int:
        # resets_at is ISO 8601 with UTC offset (e.g. "2026-07-02T19:10:00+00:00")
        try:
            r = datetime.datetime.fromisoformat(str(reset_ts).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return 0
        if r.tzinfo is None:
            r = r.replace(tzinfo=datetime.timezone.utc)
        mins = (r - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 60.0
        return int(round(mins)) if mins > 0 else 0

    five = window("five_hour")
    seven = window("seven_day")
    s_pct = pct(five.get("utilization"))
    payload = {
        "s": s_pct,
        "sr": reset_minutes(five.get("resets_at")),
        "w": pct(seven.get("utilization")),
        "wr": reset_minutes(seven.get("resets_at")),
        "st": "limited" if s_pct >= 100 else "allowed",
        "ok": True,
        "host": HOSTNAME,
        "_raw_util": raw(five.get("utilization")),
    }
    return payload


async def scan_for_device():
    """Scan for DEVICE_NAME and return the BLEDevice, or None."""
    log(f"Scanning for '{DEVICE_NAME}' ({SCAN_TIMEOUT}s)...")
    device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=SCAN_TIMEOUT)
    if device:
        log(f"Found: {device.address}")
    return device  # BLEDevice or None — NOT an address string


class Session:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.refresh_requested = asyncio.Event()

    def _on_refresh(self, _char, _data: bytearray) -> None:
        log("Refresh requested by device")
        self.refresh_requested.set()

    async def setup_refresh_subscription(self) -> None:
        # The refresh subscription is optional — the 60s poll loop works without it.
        # WinRT's start_notify() CCCD write can raise a raw OSError/WinError (not
        # wrapped as BleakError) when the peer GATT server is transiently unavailable,
        # e.g. a just-power-cycled ESP32 whose server is not yet ready (G-03-01, SC#3).
        # Degrade gracefully instead of crashing the daemon so it stays single-process
        # across a power-cycle reconnect (SC#4, no restart).
        try:
            await self.client.start_notify(REQ_CHAR_UUID, self._on_refresh)
        except (BleakError, ValueError, OSError) as e:
            log(f"Refresh subscription unavailable: {e}")

    async def write_payload(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode()
        log(f"Sending: {data.decode()}")
        try:
            await self.client.write_gatt_char(RX_CHAR_UUID, data, response=False)
            return True
        except (BleakError, OSError) as e:
            # WinRT can raise a raw OSError/WinError (NOT wrapped as BleakError)
            # when the peer GATT server goes transiently unavailable mid-write —
            # the same failure class setup_refresh_subscription() guards against.
            # Returning False trips the zombie-link break -> clean reconnect,
            # rather than an uncaught exception killing the daemon thread (the
            # silent-freeze failure mode, SC#2 field report).
            log(f"Write failed: {e}")
            return False


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Claude Code stores credentials as a JSON object; the blob may also be
    nested ({"claudeAiOauth": {"accessToken": "..."}}). Fall back to a
    regex match so unexpected shapes still work, and finally treat the
    blob as a raw token if nothing else matches.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # direct: {"accessToken": "..."}
        tok = data.get("accessToken")
        if isinstance(tok, str) and tok.strip():
            return tok
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict):
                tok = v.get("accessToken")
                if isinstance(tok, str) and tok.strip():
                    return tok
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # Raw token (no JSON wrapper) — must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _windows_credential_candidates() -> list[Path]:
    """Return the ordered list of credential file paths to probe (first hit wins).

    Priority:
    1. CLAUDE_CREDENTIALS_PATH env override (D-03, project-specific)
    2. CLAUDE_CONFIG_DIR env override (official Claude override)
    3. D-02 candidate list: home/.claude, LOCALAPPDATA/Claude, APPDATA/Claude
    """
    # Priority 1: project-specific env override (D-03)
    if override := os.environ.get("CLAUDE_CREDENTIALS_PATH"):
        return [Path(override)]
    # Priority 2: official CLAUDE_CONFIG_DIR env override
    if config_dir := os.environ.get("CLAUDE_CONFIG_DIR"):
        return [Path(config_dir) / ".credentials.json"]
    # Priority 3: D-02 candidate list — first hit wins
    home = Path.home()
    local_appdata = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    return [
        home / ".claude" / ".credentials.json",          # primary (confirmed by docs)
        local_appdata / "Claude" / ".credentials.json",  # fallback 2
        appdata / "Claude" / ".credentials.json",        # fallback 3
    ]


def read_token() -> str | None:
    """Read the Claude OAuth access token from the first available credential file."""
    for path in _windows_credential_candidates():
        try:
            return _extract_access_token(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return None


def _read_expiry_ts() -> float | None:
    """Return claudeAiOauth.expiresAt from the first-hit credentials file as
    epoch SECONDS, or None when unknown.

    CRITICAL: expiresAt is JS-convention epoch milliseconds; divide by 1000
    (Python expects seconds — raw value -> year ~57000). Feeds both the
    human-readable expiry string and the poll loop's pre-flight expiry check.
    """
    for path in _windows_credential_candidates():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = json.loads(raw)
            expires_ms = data.get("claudeAiOauth", {}).get("expiresAt")
            if expires_ms is None:
                return None
            return float(expires_ms) / 1000.0
        except (TypeError, ValueError, OSError, AttributeError, json.JSONDecodeError):
            return None
    return None


def _read_expiry() -> str:
    """Human-readable claudeAiOauth.expiresAt, or 'expiry unknown'."""
    ts = _read_expiry_ts()
    if ts is None:
        return "expiry unknown"
    try:
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "expiry unknown"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


async def _wait_first(*events: asyncio.Event, timeout: float) -> None:
    """Return when any of `events` is set, or after `timeout` seconds.

    Lets the poll loop's TICK wait wake immediately on a stop signal (clean,
    responsive Quit) without losing the refresh-request wakeup — instead of
    waiting only on refresh_requested and re-checking stop_event up to TICK
    later. Cancels and drains the loser tasks so they don't warn.
    """
    tasks = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def connect_and_run(device, stop_event: asyncio.Event, tray_state=None) -> bool:
    """Connect to device and poll until disconnected or stopped.

    Returns True if at least one successful write occurred.
    """
    log(f"Connecting to {device.address}...")
    # D-01: retry wrapper — defeats WinRT post-wake failure modes
    # (Could not get GATT services: Unreachable, stale is_connected).
    # Rebuild a fresh BleakClient each attempt (locked D-05 recipe).
    client = None
    for attempt in range(CONNECT_RETRIES):
        # D-05: pass BLEDevice (not address string), address_type="random" (NimBLE
        # static-random), use_cached_services=False (DIY firmware — WinRT GATT cache
        # may be stale after firmware reflash).
        client = BleakClient(
            device,
            address_type="random",
            use_cached_services=False,
        )
        try:
            await client.connect()
        except (BleakError, asyncio.TimeoutError) as e:
            log(f"Connection attempt {attempt + 1}/{CONNECT_RETRIES} failed: {e}")
            try:
                await client.disconnect()
            except BleakError:
                pass
            if attempt < CONNECT_RETRIES - 1:
                await asyncio.sleep(CONNECT_RETRY_DELAY)
            continue

        if not client.is_connected:
            log(f"Connection attempt {attempt + 1}/{CONNECT_RETRIES} failed (not connected)")
            try:
                await client.disconnect()
            except BleakError:
                pass
            if attempt < CONNECT_RETRIES - 1:
                await asyncio.sleep(CONNECT_RETRY_DELAY)
            continue

        # Connected successfully
        break
    else:
        log(f"Connection failed after {CONNECT_RETRIES} attempts")
        return False

    log("Connected")
    session = Session(client)
    await session.setup_refresh_subscription()

    last_poll = 0.0  # D-03: poll immediately on first connect
    last_raw_util = -1.0
    active = False             # last poll's verdict — picks the cadence below
    rate_limited_until = 0.0   # 429 backoff: hold the idle cadence until then
    rate_limit_strikes = 0     # consecutive 429s — escalates the cooldown
    last_good_payload = None   # last successful wire payload — resent with ok:false
                               # so the device shows "stale" during 401/429 stretches
    used_successfully = False
    consecutive_failures = 0  # D-03: zombie-link break counter
    try:
        while client.is_connected and not stop_event.is_set():
            now = time.time()
            elapsed = now - last_poll
            interval = poll_interval(active, now, rate_limited_until)
            if session.refresh_requested.is_set() or elapsed >= interval:
                session.refresh_requested.clear()
                token = read_token()  # D-09: fresh each cycle
                payload = None
                degraded = False  # auth/429: mark the device's data stale below
                expiry_ts = _read_expiry_ts()
                if not token:
                    log("No token; skipping poll")
                    if tray_state:
                        tray_state.set_error("token expired — run claude login")
                elif expiry_ts is not None and time.time() >= expiry_ts:
                    # Pre-flight: the credentials file says the token is already
                    # expired — polling is a guaranteed 401 that still burns a
                    # request from the endpoint's rate-limit bucket. Skip the
                    # network; read_token()/expiry re-read the file each cycle,
                    # so recovery is automatic once the token is refreshed.
                    log(
                        f"Token expired ({_read_expiry()}); skipping poll — "
                        "open Claude Code in VSCode (or run `claude login`) to re-auth"
                    )
                    if tray_state:
                        tray_state.set_error("token expired — run claude login")
                    last_poll = time.time()  # re-check at the idle cadence, not every tick
                    degraded = True
                else:
                    try:
                        payload = await poll_api(token)
                    except AuthError:
                        # Real 401/403 — the token needs a refresh. Count this as a
                        # full interval: an expired token won't fix itself in one
                        # TICK, and retrying at 5s is what drained the endpoint's
                        # rate-limit bucket (2026-07-07 field log: 401 bursts ->
                        # 429 lockout cycle).
                        log(
                            "Auth rejected; retrying at idle cadence — "
                            "open Claude Code in VSCode (or run `claude login`) to re-auth"
                        )
                        if tray_state:
                            tray_state.set_error("token expired — run claude login")
                        last_poll = time.time()
                        degraded = True
                    except RateLimited as e:
                        # Back off to the idle cadence and don't re-attempt every
                        # tick — count this attempt as a full interval. Honor
                        # Retry-After when sent; escalate on consecutive 429s.
                        rate_limit_strikes += 1
                        cooldown = rate_limit_cooldown(rate_limit_strikes, e.retry_after)
                        rate_limited_until = time.time() + cooldown
                        active = False
                        last_poll = time.time()
                        src = ("Retry-After" if e.retry_after is not None
                               else f"429 strike {rate_limit_strikes}")
                        log(f"Holding {POLL_INTERVAL}s cadence for {int(cooldown)}s ({src})")
                        degraded = True

                to_send = None
                if payload is not None:
                    rate_limit_strikes = 0
                    raw_util = payload.pop("_raw_util", 0.0)
                    active = (last_raw_util >= 0 and raw_util - last_raw_util > ACTIVE_THRESHOLD)
                    last_raw_util = raw_util
                    payload["active"] = active
                    last_good_payload = dict(payload)
                    to_send = payload
                elif degraded and last_good_payload is not None:
                    # Auth/429: tell the device its numbers are stale (ok:false,
                    # last-known values) instead of leaving it silently frozen on
                    # old data for the whole outage.
                    to_send = {**last_good_payload, "ok": False, "active": False}

                if to_send is not None:
                    if await session.write_payload(to_send):
                        last_poll = time.time()
                        used_successfully = True
                        consecutive_failures = 0  # D-03: reset on success
                        # Only a GOOD payload clears the tray state — a stale
                        # marker delivered fine must not hide the error toast.
                        if payload is not None and tray_state:
                            tray_state.set_connected(time.time())
                    else:
                        consecutive_failures += 1
                        if consecutive_failures >= ZOMBIE_BREAK_LIMIT:
                            log(
                                f"Zombie link detected ({consecutive_failures} consecutive"
                                f" write failures); abandoning connection"
                            )
                            break
                # else: payload is None from a TRANSIENT failure (network/DNS,
                # timeout, 5xx). poll_api already logged it; do NOT toast "token
                # expired" — that mislabeled a boot-time DNS blip as an auth
                # problem (SC#5). Leave tray state unchanged; the next tick
                # retries and set_connected() recovers it.

            # Wake on a refresh request OR a stop, whichever comes first. Waking
            # promptly on stop_event is what lets the finally below run
            # client.disconnect() before the process exits, so the peer gets a
            # clean GATT disconnect (returns to its waiting screen) instead of
            # being left frozen on stale data after Quit (SC#3 graceful shutdown).
            await _wait_first(session.refresh_requested, stop_event, timeout=TICK)
    finally:
        # Clean GATT disconnect on the way out — this is what tells the peripheral
        # the link is gone. WinRT can surface a raw OSError (not BleakError) here,
        # so swallow both; the link tears down regardless once we exit.
        try:
            await client.disconnect()
        except (BleakError, OSError):
            pass

    log("Device disconnected" if not stop_event.is_set() else "Stopping")
    return used_successfully


def _next_backoff(current: int, cap: int) -> int:
    """D-05: double current backoff value, clamped to cap.

    Pure helper — unit-testable without driving the main loop.
    Used by both slow-search (cap=60) and fast-reconnect (cap=RECONNECT_BACKOFF_CAP) regimes.
    """
    return min(current * 2, cap)


async def main(tray_state=None) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Populate the shared state object so the tray can route Quit through
    # loop.call_soon_threadsafe (RESEARCH Pitfall 2).  Additive — the existing
    # stop_event = asyncio.Event() line above is unchanged.
    if tray_state is not None:
        tray_state.loop = loop
        tray_state.stop_event = stop_event

    def _stop(*_args: object) -> None:
        log("Daemon stopping")
        stop_event.set()

    # OS signal handlers can only be installed from the main thread, and
    # loop.add_signal_handler is unsupported on Windows. When running under the
    # tray (04-03) the loop lives in a background thread and the tray owns clean
    # shutdown via stop_event (loop.call_soon_threadsafe), so skip silently there.
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:
                # Windows: add_signal_handler not supported; fall back to signal.signal
                try:
                    signal.signal(sig, _stop)
                except ValueError:
                    # Not the main thread of the main interpreter — tray owns shutdown.
                    pass

    log("=== Claude Usage Tracker Daemon (BLE, Windows) ===")
    log(f"Poll interval: {POLL_INTERVAL}s idle / {POLL_INTERVAL_ACTIVE}s active")

    # D-05: two distinct backoff regimes — slow-search (device absent) vs fast-reconnect (link dropped)
    search_backoff = 1     # caps at 60s — gentle, for a device that is genuinely absent/off
    reconnect_backoff = 1  # caps at RECONNECT_BACKOFF_CAP — fast, to clear the 120s SLA after a drop
    while not stop_event.is_set():
        device = await scan_for_device()
        if not device:
            # Slow-search regime: device was not found by scan — back off gently
            if tray_state:
                tray_state.set_scanning()
            log(f"Device not found, retrying in {search_backoff}s...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=search_backoff)
            except asyncio.TimeoutError:
                pass
            search_backoff = _next_backoff(search_backoff, 60)
            continue

        ok = await connect_and_run(device, stop_event, tray_state)
        if not ok:
            # Fast-reconnect regime: had/attempted a link that dropped — retry quickly
            if tray_state:
                tray_state.set_scanning()
            log(f"Connection lost, reconnecting in {reconnect_backoff}s...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=reconnect_backoff)
            except asyncio.TimeoutError:
                pass
            reconnect_backoff = _next_backoff(reconnect_backoff, RECONNECT_BACKOFF_CAP)
        else:
            # Successful session — reset reconnect counter to floor; search_backoff also reset
            reconnect_backoff = 1
            search_backoff = 1


if __name__ == "__main__":
    if sys.platform != "win32":
        print(
            "Warning: running under Linux/WSL — WinRT BLE will not be available.",
            file=sys.stderr,
        )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
