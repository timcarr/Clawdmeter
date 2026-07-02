#!/usr/bin/env python3
"""Claude Usage Tracker Daemon (BLE) — macOS port of claude-usage-daemon.sh.

Polls Claude API rate-limit headers and writes a JSON payload to the
ESP32 "Clawdmeter" peripheral over a custom GATT service. Uses
bleak (CoreBluetooth backend on macOS).
"""

import asyncio
import datetime
import getpass
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

import logging

_log_path = Path(__file__).parent / "daemon.log"
logging.basicConfig(
    filename=str(_log_path),
    level=logging.INFO,
    format="%(message)s",
)

def log(msg):
    ts = time.strftime("[%H:%M:%S]")
    line = f"{ts} {msg}"
    logging.info(line)
    print(line, flush=True)

DEVICE_NAME = "Clawdmeter"
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"

HOSTNAME = socket.gethostname()

POLL_INTERVAL = 60          # idle cadence: usage isn't rising
POLL_INTERVAL_ACTIVE = 15   # fast cadence: usage rose on the last poll
RATE_LIMIT_COOLDOWN = 900   # after a 429, hold the idle cadence this long (self-heals)
TICK = 5
SCAN_TIMEOUT = 8.0

# Minimum per-poll rise in 5h utilization (0–1) to count as real usage.
# The usage endpoint reports whole percents, so any real rise is ≥0.01
# (and the daemon's own polls are free — no self-noise to filter anymore).
ACTIVE_THRESHOLD = 0.001

# macOS: token lives in Keychain (service "Claude Code-credentials").
# Linux: token lives in ~/.claude/.credentials.json.
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
SAVED_ADDR_FILE = Path.home() / ".config" / "claude-usage-monitor" / "ble-address"

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
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # Raw token (no JSON wrapper) — must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _read_token_keychain() -> str | None:
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                getpass.getuser(),
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        log(f"Keychain read failed (rc={e.returncode}): {e.stderr.strip()}")
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f"Keychain access error: {e}")
        return None
    return _extract_access_token(out.stdout)


def _read_token_file() -> str | None:
    try:
        raw = CREDENTIALS_PATH.read_text()
    except OSError as e:
        log(f"Error reading credentials: {e}")
        return None
    return _extract_access_token(raw)


def read_token() -> str | None:
    if sys.platform == "darwin":
        return _read_token_keychain()
    return _read_token_file()


def load_cached_address() -> str | None:
    if not SAVED_ADDR_FILE.exists():
        return None
    addr = SAVED_ADDR_FILE.read_text().strip()
    # Accept both Linux MAC (AA:BB:CC:DD:EE:FF) and macOS CoreBluetooth UUID
    # (E621E1F8-C36C-495A-93FC-0C247A3E6E5F).
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", addr) or re.fullmatch(
        r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}", addr
    ):
        return addr
    log("Cached address malformed, discarding")
    SAVED_ADDR_FILE.unlink(missing_ok=True)
    return None


def save_address(addr: str) -> None:
    SAVED_ADDR_FILE.parent.mkdir(parents=True, exist_ok=True)
    SAVED_ADDR_FILE.write_text(addr)


async def scan_for_device() -> str | None:
    log(f"Scanning for '{DEVICE_NAME}' ({SCAN_TIMEOUT}s)...")
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT)
    for d in devices:
        if d.name == DEVICE_NAME:
            log(f"Found: {d.address}")
            return d.address
    return None


# --- macOS: recover a device the OS already holds as an HID keyboard --------
#
# The firmware advertises as a BLE HID keyboard so its buttons type into the
# Mac. macOS auto-connects to that HID, and CoreBluetooth then EXCLUDES the
# peripheral from BleakScanner.discover() results (already-connected devices
# never appear in scans). bleak's connect-by-address path also scans
# internally, so a cached address can't help either. The documented escape
# hatch is retrieveConnectedPeripheralsWithServices_, which returns
# peripherals the system is already connected to. We wrap the result in a
# BLEDevice carrying the live (peripheral, manager) details so BleakClient
# connects to it directly without scanning. CoreBluetooth shares the single
# physical link, so this rides the existing HID connection — the keyboard
# keeps working.
_cb_manager = None  # reused CentralManagerDelegate (CoreBluetooth)


async def _get_cb_manager():
    """Lazily create and ready a shared CoreBluetooth central manager."""
    global _cb_manager
    if _cb_manager is None:
        from bleak.backends.corebluetooth.CentralManagerDelegate import (
            CentralManagerDelegate,
        )

        mgr = CentralManagerDelegate()
        await mgr.wait_until_ready()  # raises if Bluetooth is unauthorized/off
        _cb_manager = mgr
    return _cb_manager


async def retrieve_connected_macos(skip_addr: str | None = None):
    """Return a BLEDevice for a system-connected 'Claude Controller', or None.

    Two-step lookup, strongest signal first:

    1. Peripherals connected under our CUSTOM service UUID. Membership in
       that service is unambiguous (no other device exposes it), so we accept
       by service alone — the peripheral's name can be None on macOS.
    2. Fall back to the generic HID service 0x1812, but ONLY trust a
       peripheral whose name matches DEVICE_NAME. 0x1812 also matches
       unrelated keyboards/mice, so picking blindly here could grab the
       wrong device.

    ``skip_addr`` skips a peripheral whose UUID just failed to connect, so a
    stale CoreBluetooth handle can't trap us into never trying a fresh scan.
    """
    from CoreBluetooth import CBUUID
    from bleak.backends.device import BLEDevice

    try:
        manager = await _get_cb_manager()
    except Exception as e:  # BleakBluetoothNotAvailableError etc.
        log(f"CoreBluetooth unavailable: {e}")
        return None

    cm = manager.central_manager

    def _wrap(p):
        addr = p.identifier().UUIDString()
        log(f"Found system-connected peripheral: {p.name()!r} [{addr}]")
        return BLEDevice(addr, p.name(), (p, manager))

    def _ok(p) -> bool:
        return not (skip_addr and p.identifier().UUIDString() == skip_addr)

    # 1. Custom service — accept by service membership alone.
    custom = cm.retrieveConnectedPeripheralsWithServices_(
        [CBUUID.UUIDWithString_(SERVICE_UUID)]
    )
    for p in custom or []:
        if _ok(p):
            return _wrap(p)

    # 2. Generic HID service — require an exact name match.
    hid = cm.retrieveConnectedPeripheralsWithServices_(
        [CBUUID.UUIDWithString_("1812")]
    )
    for p in hid or []:
        if _ok(p) and p.name() == DEVICE_NAME:
            return _wrap(p)

    return None


async def discover_target(skip_addr: str | None = None):
    """Return a connectable target, or None.

    macOS: prefer the system-connected peripheral (HID-grabbed devices are
    invisible to scans); fall back to a normal scan that yields a BLEDevice
    so the subsequent connect doesn't have to re-scan. ``skip_addr`` is
    forwarded so a just-failed peripheral is skipped, making the scan
    fallback reachable.

    Other platforms: keep the original cached-address / scan-by-name flow.
    A freshly scanned address is cached here (the only place it's saved).
    """
    if sys.platform == "darwin":
        dev = await retrieve_connected_macos(skip_addr=skip_addr)
        if dev is not None:
            return dev
        log(f"Not held by OS; scanning for '{DEVICE_NAME}' ({SCAN_TIMEOUT}s)...")
        dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=SCAN_TIMEOUT)
        if dev:
            log(f"Found: {dev.address}")
        return dev

    address = load_cached_address()
    if not address:
        address = await scan_for_device()
        if address:
            save_address(address)  # cache only freshly-scanned addresses
    return address


class RateLimited(Exception):
    """Raised by poll_api on a 429 — the usage endpoint is rate-limiting this
    token. The poll loop reacts by holding the idle cadence (POLL_INTERVAL) for
    RATE_LIMIT_COOLDOWN instead of retrying every tick."""


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
        log(f"API call failed: {e}")
        return None
    if resp.status_code == 429:
        # The usage endpoint has its own per-token rate limit — back off.
        log(f"API HTTP 429 (rate limited): {resp.text[:200]}")
        raise RateLimited()
    if resp.status_code >= 400:
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


class Session:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.refresh_requested = asyncio.Event()

    def _on_refresh(self, _char, _data: bytearray) -> None:
        log("Refresh requested by device")
        self.refresh_requested.set()

    async def setup_refresh_subscription(self) -> None:
        try:
            await self.client.start_notify(REQ_CHAR_UUID, self._on_refresh)
        except (BleakError, ValueError) as e:
            log(f"Refresh subscription unavailable: {e}")

    async def write_payload(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode()
        log(f"Sending: {data.decode()}")
        try:
            await self.client.write_gatt_char(RX_CHAR_UUID, data, response=False)
            return True
        except BleakError as e:
            log(f"Write failed: {e}")
            return False


async def connect_and_run(target, stop_event: asyncio.Event) -> bool:
    """Connect to a target and poll until disconnected or stopped.

    ``target`` is either an address string (Linux) or a BLEDevice carrying
    live CoreBluetooth details (macOS). Returns True if the connection was
    used successfully (so the caller keeps the cached address), False if the
    connection failed and the cache should be invalidated.
    """
    display = target if isinstance(target, str) else target.address
    log(f"Connecting to {display}...")
    client = BleakClient(target)
    try:
        await client.connect()
    except (BleakError, asyncio.TimeoutError) as e:
        log(f"Connection failed: {e}")
        return False

    if not client.is_connected:
        log("Connection failed (no error but not connected)")
        return False

    log("Connected")
    session = Session(client)
    await session.setup_refresh_subscription()

    last_poll = 0.0
    last_raw_util = -1.0
    active = False             # last poll's verdict — picks the cadence below
    rate_limited_until = 0.0   # 429 backoff: hold the idle cadence until then
    used_successfully = False
    try:
        while client.is_connected and not stop_event.is_set():
            now = time.time()
            elapsed = now - last_poll
            interval = poll_interval(active, now, rate_limited_until)
            if session.refresh_requested.is_set() or elapsed >= interval:
                session.refresh_requested.clear()
                token = read_token()
                if not token:
                    log("No token; skipping poll")
                else:
                    try:
                        payload = await poll_api(token)
                    except RateLimited:
                        # Back off to the idle cadence and don't re-attempt every
                        # tick (a failed poll normally retries at TICK) — count
                        # this attempt as a full interval.
                        rate_limited_until = time.time() + RATE_LIMIT_COOLDOWN
                        active = False
                        last_poll = time.time()
                        log(f"Holding {POLL_INTERVAL}s cadence for {RATE_LIMIT_COOLDOWN // 60} min")
                        payload = None
                    if payload is not None:
                        raw_util = payload.pop("_raw_util", 0.0)
                        active = (last_raw_util >= 0 and raw_util - last_raw_util > ACTIVE_THRESHOLD)
                        last_raw_util = raw_util
                        payload["active"] = active
                        if await session.write_payload(payload):
                            last_poll = time.time()
                            used_successfully = True

            try:
                await asyncio.wait_for(session.refresh_requested.wait(), timeout=TICK)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await client.disconnect()
        except BleakError:
            pass

    log("Device disconnected" if not stop_event.is_set() else "Stopping")
    return used_successfully


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop(*_args: object) -> None:
        log("Daemon stopping")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, _stop)

    log("=== Claude Usage Tracker Daemon (BLE, macOS) ===")
    log(f"Poll interval: {POLL_INTERVAL}s idle / {POLL_INTERVAL_ACTIVE}s active")

    backoff = 1
    skip_addr: str | None = None  # macOS: a peripheral to skip for one cycle
    while not stop_event.is_set():
        # Apply any pending skip exactly once, then clear it so the next
        # cycle re-tries retrieveConnected (the device may have recovered).
        target = await discover_target(skip_addr=skip_addr)
        skip_addr = None
        if not target:
            log(f"Device not found, retrying in {backoff}s...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
            continue

        addr = target if isinstance(target, str) else target.address
        ok = await connect_and_run(target, stop_event)
        if not ok:
            if sys.platform == "darwin":
                # No string cache to drop; instead skip this stale handle on
                # the next retrieveConnected so the scan fallback is reachable.
                skip_addr = addr
            else:
                log("Invalidating cached address")
                SAVED_ADDR_FILE.unlink(missing_ok=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
        else:
            backoff = 1


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
