#!/usr/bin/env python3
"""
wsprwatch_udp.py — WSJT-X UDP Decode watcher for WSPR + GPIO LED + CSV log

- Separate from FT8 watcher (own YAML).
- Listens to WSJT-X UDP packets (default 2237).
- Extracts Decode text lines.
- Best-effort extracts: callsign, Maidenhead grid (4/6), SNR (approx).
- "DX/interesting" decision:
    - If require_grid=true: needs a valid grid + distance >= min_distance_km
    - If require_grid=false: callsign-only decodes can be treated as DX (distance unknown)
  Optional prefix blacklist applied first.

LED:
  - ON solid while within dx_hold_minutes from last DX
  - otherwise OFF + heartbeat blink

CSV:
  timestamp_utc, freq_hz, callsign, grid, distance_km, snr, raw_line

Run:
  sudo apt install -y python3-yaml python3-gpiod
  sudo python3 wsprwatch_udp.py /path/to/config_wspr.yaml
"""

from __future__ import annotations

import atexit
import csv
import math
import os
import re
import socket
import string
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import yaml

try:
    import gpiod  # type: ignore
except Exception:
    gpiod = None


# =============================================================================
# Config
# =============================================================================

@dataclass
class WsprCfg:
    home_grid: str = "JN97"
    min_distance_km: float = 800.0
    require_grid: bool = True
    blacklist_prefixes: List[str] = None

    def __post_init__(self) -> None:
        if self.blacklist_prefixes is None:
            self.blacklist_prefixes = []


@dataclass
class AlertCfg:
    dx_hold_minutes: int = 20
    heartbeat_every_seconds: float = 5.0
    heartbeat_on_seconds: float = 0.2
    csv_path: str = "/home/orangepi/WSPR/wspr_dx_log.csv"


@dataclass
class GpioCfg:
    chip: int = 1
    port: str = "PI6"
    active_high: bool = True

    def resolved_line(self) -> int:
        m = re.fullmatch(r"P([A-Z])(\d{1,2})", self.port.strip().upper())
        if not m:
            raise ValueError(f"Invalid gpio port '{self.port}'. Expected like 'PI6'.")
        bank = ord(m.group(1)) - ord("A")
        pin = int(m.group(2))
        if not (0 <= pin <= 31):
            raise ValueError(f"Invalid gpio pin number in '{self.port}'. Must be 0..31.")
        return bank * 32 + pin


@dataclass
class UdpCfg:
    bind_ip: str = "0.0.0.0"
    port: int = 2237


def load_cfg(path: str) -> Tuple[WsprCfg, AlertCfg, GpioCfg, UdpCfg]:
    with open(path, "r", encoding="utf-8") as f:
        cfg: Dict[str, Any] = yaml.safe_load(f) or {}

    w = cfg.get("wspr", {}) or {}
    blacklist = [str(x).upper().strip() for x in (w.get("blacklist_prefixes", []) or []) if str(x).strip()]

    wspr = WsprCfg(
        home_grid=str(w.get("home_grid", "JN97")).upper().strip(),
        min_distance_km=float(w.get("min_distance_km", 800.0)),
        require_grid=bool(w.get("require_grid", True)),
        blacklist_prefixes=blacklist,
    )

    a = cfg.get("alert", {}) or {}
    alert = AlertCfg(
        dx_hold_minutes=int(a.get("dx_hold_minutes", 20)),
        heartbeat_every_seconds=float(a.get("heartbeat_every_seconds", 5.0)),
        heartbeat_on_seconds=float(a.get("heartbeat_on_seconds", 0.2)),
        csv_path=str(a.get("csv_path", "/home/orangepi/WSPR/wspr_dx_log.csv")),
    )

    g = cfg.get("gpio", {}) or {}
    gpio_cfg = GpioCfg(
        chip=int(g.get("chip", 1)),
        port=str(g.get("port", "PI6")),
        active_high=bool(g.get("active_high", True)),
    )

    u = cfg.get("wsjtx_udp", {}) or {}
    udp = UdpCfg(
        bind_ip=str(u.get("bind_ip", "0.0.0.0")),
        port=int(u.get("port", 2237)),
    )

    return wspr, alert, gpio_cfg, udp


# =============================================================================
# GPIO LED (libgpiod v1)
# =============================================================================

class LedGpiod:
    def __init__(self, cfg: GpioCfg):
        if gpiod is None:
            raise RuntimeError("python3-gpiod is not installed. Install: sudo apt install -y python3-gpiod")
        self.cfg = cfg
        self._chip = gpiod.Chip(f"/dev/gpiochip{cfg.chip}")
        self._line_num = cfg.resolved_line()
        self._line = self._chip.get_line(self._line_num)
        self._line.request(
            consumer="wsprwatch_udp",
            type=gpiod.LINE_REQ_DIR_OUT,
            default_val=self._off_value(),
        )
        atexit.register(self.off)
        atexit.register(self.close)

    def _on_value(self) -> int:
        return 1 if self.cfg.active_high else 0

    def _off_value(self) -> int:
        return 0 if self.cfg.active_high else 1

    def on(self) -> None:
        try:
            self._line.set_value(self._on_value())
        except Exception:
            pass

    def off(self) -> None:
        try:
            self._line.set_value(self._off_value())
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._chip.close()
        except Exception:
            pass


# =============================================================================
# CSV logging
# =============================================================================

CSV_HEADER = ["timestamp_utc", "freq_hz", "callsign", "grid", "distance_km", "snr", "raw_line"]

def ensure_csv_header(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(CSV_HEADER)

def append_csv(path: str, row: List[Any]) -> None:
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)

def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


# =============================================================================
# WSJT-X UDP parsing (minimal)
# =============================================================================

MAGIC = 0xADBCCBDA
MSG_STATUS = 1
MSG_DECODE = 2

class _QtStream:
    def __init__(self, data: bytes, off: int = 0):
        self.data = data
        self.off = off

    def _need(self, n: int) -> None:
        if self.off + n > len(self.data):
            raise ValueError("Truncated WSJT-X UDP packet")

    def i32(self) -> int:
        self._need(4)
        v = int.from_bytes(self.data[self.off : self.off + 4], "big", signed=True)
        self.off += 4
        return v

    def u64(self) -> int:
        self._need(8)
        v = int.from_bytes(self.data[self.off : self.off + 8], "big", signed=False)
        self.off += 8
        return v

    def qstring(self) -> str:
        byte_len = self.i32()
        if byte_len == -1:
            return ""
        if byte_len < 0:
            raise ValueError("Invalid QString length")
        self._need(byte_len)
        raw = self.data[self.off : self.off + byte_len]
        self.off += byte_len
        return raw.decode("utf-16-be", errors="ignore")


_PRINTABLE = set(bytes(string.printable, "ascii"))

def _extract_ascii_runs(payload: bytes, minlen: int = 6) -> List[str]:
    runs: List[str] = []
    cur = bytearray()
    for b in payload:
        if b in _PRINTABLE and b != 0x00:
            cur.append(b)
        else:
            if len(cur) >= minlen:
                runs.append(cur.decode("ascii", errors="ignore").strip())
            cur.clear()
    if len(cur) >= minlen:
        runs.append(cur.decode("ascii", errors="ignore").strip())
    return [s for s in runs if s]

def parse_wsjtx_packet(data: bytes) -> Tuple[Optional[int], Optional[str]]:
    if len(data) < 12:
        return None, None

    magic = int.from_bytes(data[0:4], "big", signed=False)
    mtype = int.from_bytes(data[8:12], "big", signed=False)
    if magic != MAGIC:
        return None, None

    if mtype == MSG_STATUS:
        try:
            s = _QtStream(data, 12)
            _app_id = s.qstring()      # "WSJT-X"
            dial = s.u64()             # dial frequency Hz
            if 100_000 <= dial <= 10_000_000_000:
                return int(dial), None
        except Exception:
            pass
        return None, None

    if mtype == MSG_DECODE:
        runs = _extract_ascii_runs(data, minlen=6)
        if len(runs) >= 2 and runs[0] == "WSJT-X":
            decoded = runs[-1].strip()
            return None, decoded or None
        # fallback
        for s in runs:
            if len(s.split()) >= 2:
                return None, s
        return None, None

    return None, None


# =============================================================================
# WSPR decode parsing + grid distance
# =============================================================================

CALL_RE = re.compile(r"\b([A-Z0-9]{1,3}\d[A-Z0-9]{1,4})(?:/[A-Z0-9]{1,4})?\b", re.IGNORECASE)
GRID_EXTRACT_RE = re.compile(r"\b([A-R]{2}\d{2}(?:[A-X]{2})?)\b", re.IGNORECASE)

GRID4_RE = re.compile(r"^[A-R]{2}\d{2}$", re.IGNORECASE)
GRID6_RE = re.compile(r"^[A-R]{2}\d{2}[A-X]{2}$", re.IGNORECASE)

def is_blacklisted_callsign(sender: str, blacklist_prefixes: List[str]) -> bool:
    u = sender.upper().split("/")[0]
    for p in blacklist_prefixes:
        p = p.upper().strip()
        if p and u.startswith(p):
            return True
    return False

def normalize_and_validate_grid(grid: Optional[str]) -> Optional[str]:
    if not grid:
        return None
    g = re.sub(r"[^A-Z0-9]", "", grid.upper())
    if len(g) > 6:
        g = g[:6]
    if GRID6_RE.match(g):
        return g
    if GRID4_RE.match(g):
        return g
    return None

def parse_callsign_grid_snr(text: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    up = text.upper().replace("<", "").replace(">", "").replace(";", " ")

    m_call = CALL_RE.search(up)
    m_grid = GRID_EXTRACT_RE.search(up)

    call = m_call.group(1).upper() if m_call else None
    grid = m_grid.group(1).upper() if m_grid else None
    grid = normalize_and_validate_grid(grid)

    # Best-effort SNR: first integer within a plausible range
    snr = None
    for m in re.finditer(r"(?<!\d)(-?\d{1,3})(?!\d)", up):
        try:
            v = int(m.group(1))
        except ValueError:
            continue
        if -60 <= v <= 30:
            snr = v
            break

    return call, grid, snr

def maiden_to_latlon(grid: str) -> Tuple[float, float]:
    """
    Maidenhead (4 or 6 chars) -> (lat, lon) in degrees (center of square).
    """
    g = grid.strip().upper()
    if len(g) not in (4, 6):
        raise ValueError(f"Grid must be 4 or 6 chars: {grid}")

    lon = -180.0 + (ord(g[0]) - ord("A")) * 20.0
    lat = -90.0 + (ord(g[1]) - ord("A")) * 10.0

    lon += int(g[2]) * 2.0
    lat += int(g[3]) * 1.0

    # center of 4-char square
    lon += 1.0
    lat += 0.5

    if len(g) == 6:
        lon -= 1.0
        lat -= 0.5
        lon += (ord(g[4]) - ord("A")) * (5.0 / 60.0)
        lat += (ord(g[5]) - ord("A")) * (2.5 / 60.0)
        # center of subsquare
        lon += (2.5 / 60.0)
        lat += (1.25 / 60.0)

    return lat, lon

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

def distance_km(home_grid: str, dx_grid: str) -> float:
    lat1, lon1 = maiden_to_latlon(normalize_and_validate_grid(home_grid) or home_grid)
    lat2, lon2 = maiden_to_latlon(dx_grid)
    return haversine_km(lat1, lon1, lat2, lon2)


# =============================================================================
# Console formatting
# =============================================================================

GREEN = "\x1b[32m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"

def fmt_line(freq_hz: Optional[int], msg: str, is_dx: bool) -> str:
    f = "" if freq_hz is None else f"{freq_hz}Hz"
    core = f"{f} {msg}".strip()
    return f"{GREEN}{core}{RESET}" if is_dx else f"{DIM}{core}{RESET}"


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: sudo {sys.argv[0]} /path/to/config_wspr.yaml", file=sys.stderr)
        return 2

    cfg_path = sys.argv[1]
    wspr, alert, gpio_cfg, udp = load_cfg(cfg_path)

    # Validate home grid early if require_grid is true
    if wspr.require_grid:
        hg = normalize_and_validate_grid(wspr.home_grid)
        if hg is None:
            raise ValueError(f"wspr.home_grid '{wspr.home_grid}' must be a valid 4/6 char Maidenhead grid when require_grid=true")
        wspr.home_grid = hg

    print(f"[INFO] cfg={cfg_path}", flush=True)
    print(f"[INFO] wspr: home_grid={wspr.home_grid} min_distance_km={wspr.min_distance_km} require_grid={wspr.require_grid} blacklist_prefixes={wspr.blacklist_prefixes}", flush=True)
    print(f"[INFO] alert: dx_hold_minutes={alert.dx_hold_minutes} heartbeat_every_seconds={alert.heartbeat_every_seconds} heartbeat_on_seconds={alert.heartbeat_on_seconds}", flush=True)
    print(f"[INFO] csv: {alert.csv_path}", flush=True)
    print(f"[INFO] gpio: chip={gpio_cfg.chip} port={gpio_cfg.port} line={gpio_cfg.resolved_line()} active_high={gpio_cfg.active_high}", flush=True)
    print(f"[INFO] wsjtx_udp: bind={udp.bind_ip}:{udp.port}", flush=True)

    ensure_csv_header(alert.csv_path)

    led = LedGpiod(gpio_cfg)
    led.off()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind((udp.bind_ip, udp.port))
    sock.settimeout(0.5)

    dial_freq_hz: Optional[int] = None
    last_dx_mono: Optional[float] = None

    hold_seconds = max(0, int(alert.dx_hold_minutes)) * 60
    pulse_off_at: Optional[float] = None
    next_heartbeat_at: float = time.monotonic() + alert.heartbeat_every_seconds

    def led_tick(now: float) -> None:
        nonlocal pulse_off_at, next_heartbeat_at, last_dx_mono
        dx_active = (last_dx_mono is not None) and ((now - last_dx_mono) < hold_seconds)
        if dx_active:
            led.on()
            pulse_off_at = None
            return

        if pulse_off_at is not None:
            if now >= pulse_off_at:
                led.off()
                pulse_off_at = None
            return

        if now >= next_heartbeat_at:
            led.on()
            pulse_off_at = now + float(alert.heartbeat_on_seconds)
            next_heartbeat_at = now + float(alert.heartbeat_every_seconds)

    while True:
        led_tick(time.monotonic())

        try:
            data, _addr = sock.recvfrom(65535)
        except socket.timeout:
            continue

        status_dial, decoded_text = parse_wsjtx_packet(data)

        if status_dial is not None:
            dial_freq_hz = status_dial
            continue

        if decoded_text is None:
            continue

        call, grid, snr = parse_callsign_grid_snr(decoded_text)

        dist_km: Optional[float] = None
        is_dx = False

        # 1) blacklist first
        if call and is_blacklisted_callsign(call, wspr.blacklist_prefixes):
            is_dx = False
        else:
            # 2) grid-based if available
            if call and grid:
                try:
                    dist_km = distance_km(wspr.home_grid, grid)
                    is_dx = dist_km >= float(wspr.min_distance_km)
                except Exception:
                    dist_km = None
                    is_dx = False
            # 3) optional callsign-only DX mode
            elif call and (not wspr.require_grid):
                is_dx = True

        print(fmt_line(dial_freq_hz, decoded_text, is_dx), flush=True)

        if is_dx and call:
            last_dx_mono = time.monotonic()
            row = [
                now_utc_str(),
                "" if dial_freq_hz is None else int(dial_freq_hz),
                call,
                "" if grid is None else grid,
                "" if dist_km is None else f"{dist_km:.1f}",
                "" if snr is None else int(snr),
                decoded_text,
            ]
            append_csv(alert.csv_path, row)

    # unreachable
    # return 0

if __name__ == "__main__":
    raise SystemExit(main())
