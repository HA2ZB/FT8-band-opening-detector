#!/usr/bin/env python3
"""
ft8watch_udp.py — WSJT-X UDP Decode watcher + GPIO LED + CSV log

This script listens to WSJT-X UDP multicast/unicast packets (typically on localhost),
extracts "Decode" text lines, classifies them as "DX" or "not DX" using a simple
prefix blacklist, and then drives a GPIO LED:

  - When a DX decode is seen: LED turns ON and stays ON for dx_hold_minutes.
  - When no DX is seen recently: LED is OFF, but it "heartbeats" (brief blink)
    every heartbeat_every_seconds for heartbeat_on_seconds.

The script also logs DX events into a CSV file:
  timestamp_utc, freq_hz, sender_callsign, grid, snr, raw_line

Notes:
- Frequency comes from WSJT-X "Status" packets (dial frequency in Hz). If WSJT-X UI
  is on a different band than your actual RF chain, the logged dial frequency will
  reflect the UI setting.
- "snr" is left empty in CSV in this version (field reserved for future parsing).

Dependencies:
  sudo apt install -y python3-yaml python3-gpiod

Run:
  sudo python3 ft8watch_udp.py /home/orangepi/FT8/config.yaml
"""

from __future__ import annotations

import atexit
import csv
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
    # libgpiod Python bindings (v1 style, works with /dev/gpiochipN)
    import gpiod  # type: ignore
except Exception:
    gpiod = None


# =============================================================================
# Configuration model
# =============================================================================

@dataclass
class Ft8Cfg:
    """
    FT8/DX classification options.

    only_cq:
      - True  -> consider only CQ messages as candidates for DX
      - False -> consider any decoded line as candidate (not only CQ)

    blacklist_prefixes:
      - list of prefix strings. If a decoded sender callsign starts with any of
        these prefixes, it is treated as "NOT DX" (i.e., nearby / ignored).
      - matching is a simple string prefix match: callsign.startswith(prefix)
    """
    only_cq: bool = True
    blacklist_prefixes: List[str] = None

    def __post_init__(self) -> None:
        if self.blacklist_prefixes is None:
            self.blacklist_prefixes = []


@dataclass
class AlertCfg:
    """
    LED timing and CSV output.

    dx_hold_minutes:
      How long the LED stays ON after the last DX decode.

    heartbeat_every_seconds / heartbeat_on_seconds:
      When no DX is active, blink LED for heartbeat_on_seconds every
      heartbeat_every_seconds.

    csv_path:
      Where DX events are appended.
    """
    dx_hold_minutes: int = 20
    heartbeat_every_seconds: float = 5.0
    heartbeat_on_seconds: float = 0.2
    csv_path: str = "/home/orangepi/FT8/dx_log.csv"


@dataclass
class GpioCfg:
    """
    GPIO control for the LED.

    chip:
      gpiochip index (e.g. /dev/gpiochip1 -> chip=1)

    port:
      Allwinner-style port string such as "PI6". Converted into a gpiod "line"
      number: bank*32 + pin

    active_high:
      True  -> drive line HIGH to turn LED ON
      False -> drive line LOW to turn LED ON
    """
    chip: int = 1
    port: str = "PI6"  # Example: "PI6"
    active_high: bool = True

    def resolved_line(self) -> int:
        """
        Convert an Allwinner port name like "PI6" into a gpiod line index.

        Banks are letters A..Z, each bank has 32 pins (0..31). The resulting line
        number is:

            line = (bank_index * 32) + pin

        Example:
          PI6 -> bank 'I' => index 8 (A=0, B=1, ... I=8)
                pin 6
                line = 8*32 + 6 = 262
        """
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
    """
    WSJT-X UDP input.

    bind_ip:
      Typically "0.0.0.0" to accept any interface or "127.0.0.1" for loopback-only.

    port:
      WSJT-X default is commonly 2237, but can be changed in WSJT-X settings.
    """
    bind_ip: str = "0.0.0.0"
    port: int = 2237


def load_cfg(path: str) -> Tuple[Ft8Cfg, AlertCfg, GpioCfg, UdpCfg]:
    """
    Load YAML config and map it into typed dataclasses.

    Expected YAML keys:
      ft8:
        only_cq: bool
        blacklist_prefixes: [ ... ]
      alert:
        dx_hold_minutes: int
        heartbeat_every_seconds: float
        heartbeat_on_seconds: float
        csv_path: str
      gpio:
        chip: int
        port: str (e.g. "PI6")
        active_high: bool
      wsjtx_udp:
        bind_ip: str
        port: int
    """
    with open(path, "r", encoding="utf-8") as f:
        cfg: Dict[str, Any] = yaml.safe_load(f) or {}

    fcfg = cfg.get("ft8", {}) or {}
    blacklist = [str(x).upper() for x in (fcfg.get("blacklist_prefixes", []) or [])]
    ft8 = Ft8Cfg(
        only_cq=bool(fcfg.get("only_cq", True)),
        blacklist_prefixes=blacklist,
    )

    acfg = cfg.get("alert", {}) or {}
    alert = AlertCfg(
        dx_hold_minutes=int(acfg.get("dx_hold_minutes", 20)),
        heartbeat_every_seconds=float(acfg.get("heartbeat_every_seconds", 5.0)),
        heartbeat_on_seconds=float(acfg.get("heartbeat_on_seconds", 0.2)),
        csv_path=str(acfg.get("csv_path", "/home/orangepi/FT8/dx_log.csv")),
    )

    gcfg = cfg.get("gpio", {}) or {}
    gpio_cfg = GpioCfg(
        chip=int(gcfg.get("chip", 1)),
        port=str(gcfg.get("port", "PI6")),
        active_high=bool(gcfg.get("active_high", True)),
    )

    ucfg = cfg.get("wsjtx_udp", {}) or {}
    udp = UdpCfg(
        bind_ip=str(ucfg.get("bind_ip", "0.0.0.0")),
        port=int(ucfg.get("port", 2237)),
    )

    return ft8, alert, gpio_cfg, udp


# =============================================================================
# GPIO LED implementation (libgpiod v1 Python bindings)
# =============================================================================

class LedGpiod:
    """
    Simple GPIO LED wrapper.

    - Requests a single output line from /dev/gpiochipN.
    - Provides on/off methods.
    - Registers atexit handlers to try to turn the LED off and close the chip.
    """

    def __init__(self, cfg: GpioCfg):
        if gpiod is None:
            raise RuntimeError(
                "python3-gpiod is not installed. Install: sudo apt install -y python3-gpiod"
            )
        self.cfg = cfg
        self._chip = gpiod.Chip(f"/dev/gpiochip{cfg.chip}")
        self._line_num = cfg.resolved_line()
        self._line = self._chip.get_line(self._line_num)

        # Request output direction. default_val ensures known LED state on start.
        self._line.request(
            consumer="ft8watch_udp",
            type=gpiod.LINE_REQ_DIR_OUT,
            default_val=self._off_value(),
        )

        # Ensure clean shutdown. We call off() before close() to avoid errors
        # if the application exits abruptly while LED is ON.
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
            # On exit paths or rare kernel issues, ignore to keep script robust.
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

# "snr" column is reserved; this script currently writes it as empty.
CSV_HEADER = ["timestamp_utc", "freq_hz", "sender_callsign", "grid", "snr", "raw_line"]


def ensure_csv_header(path: str) -> None:
    """
    Create the CSV file and write the header if the file does not exist or is empty.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(CSV_HEADER)


def append_csv(
    path: str,
    ts: str,
    freq_hz: Optional[int],
    sender: str,
    grid: Optional[str],
    raw_line: str,
) -> None:
    """
    Append one DX event line to the CSV.

    freq_hz can be None if no Status packet has been received yet (unlikely, but possible).
    """
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(
            [
                ts,
                "" if freq_hz is None else int(freq_hz),
                sender,
                grid or "",
                "",  # snr placeholder
                raw_line,
            ]
        )


# =============================================================================
# WSJT-X UDP parsing (Qt QDataStream)
# =============================================================================
#
# WSJT-X uses a Qt QDataStream format. Packets start with a 32-bit magic value
# 0xADBCCBDA, followed by:
#
#   u32  schema_version
#   u32  message_type
#   ...  message payload depends on message_type
#
# We care about:
#   - STATUS messages (type 1): contains dial frequency in Hz (u64)
#   - DECODE messages (type 2): contains a decoded message text line
#
# Some WSJT-X messages contain proper QString fields. Others can be recovered
# reliably by scanning for ASCII runs, which is what we do for DECODE.
#
# Endianness: QDataStream default is BigEndian, so we parse integers as big-endian.

MAGIC = 0xADBCCBDA

MSG_HEARTBEAT = 0
MSG_STATUS = 1
MSG_DECODE = 2


class _QtStream:
    """
    Minimal Qt QDataStream reader for fields we need.

    - Reads big-endian integers.
    - Reads Qt QString: i32 byte_len, then UTF-16BE payload of that length.
    """

    def __init__(self, data: bytes, off: int = 0):
        self.data = data
        self.off = off

    def _need(self, n: int) -> None:
        if self.off + n > len(self.data):
            raise ValueError("Truncated WSJT-X UDP packet")

    def u32(self) -> int:
        self._need(4)
        v = int.from_bytes(self.data[self.off : self.off + 4], "big", signed=False)
        self.off += 4
        return v

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
        # Qt QString format: i32 byte_len, then UTF-16BE data (byte_len bytes)
        byte_len = self.i32()
        if byte_len == -1:
            # -1 means "null string"
            return ""
        if byte_len < 0:
            raise ValueError("Invalid QString length")
        self._need(byte_len)
        raw = self.data[self.off : self.off + byte_len]
        self.off += byte_len
        return raw.decode("utf-16-be", errors="ignore")


# Precompute "printable ASCII" bytes to find likely decode lines in raw packets.
_PRINTABLE = set(bytes(string.printable, "ascii"))

# A pragmatic callsign regex that matches typical ham callsigns including optional suffix /P etc.
_RE_CALL = re.compile(r"^(?:[A-Z]{1,2}\d{1,4}[A-Z]{1,4}|[A-Z0-9]{3,})(?:/[A-Z0-9]{1,4})?$")
_RE_GRID = re.compile(r"^[A-R]{2}\d{2}[A-X]{0,2}$")
_RE_RPT = re.compile(r"^(?:R?[+-]\d{1,2})$")
_RE_FINAL = re.compile(r"^(?:RR73|RRR|73)$")


def _looks_like_decode_line(s: str) -> bool:
    """
    Heuristic: decide whether an ASCII string looks like an FT8 decode line.

    WSJT-X "decoded text" is typically one of:
      - CQ <CALL> <GRID>
      - <CALL> <CALL> <RPT>    (e.g. -11 or R-11 or +05)
      - <CALL> <CALL> RR73 / 73
      - <CALL> <CALL> <GRID>

    This heuristic is intentionally permissive. The goal is: capture decode strings
    while avoiding random garbage ASCII runs.
    """
    s = s.strip()
    if not s:
        return False

    parts = s.split()
    if len(parts) < 2:
        return False

    if parts[0] == "CQ":
        call_idx = None
        for i in range(1, len(parts)):
            if _RE_CALL.match(parts[i]):
                call_idx = i
                break
        if call_idx is None:
            return False

        # Some CQ formats insert a token (e.g. "CQ DX ..."), but we keep it simple.
        if call_idx + 1 < len(parts):
            msg = parts[call_idx + 1]
            if _RE_GRID.match(msg) or _RE_RPT.match(msg) or _RE_FINAL.match(msg):
                return True
        return True

    # Non-CQ: typically "<CALL1> <CALL2> <...>"
    if _RE_CALL.match(parts[0]) and _RE_CALL.match(parts[1]):
        if len(parts) == 2:
            return True
        msg = parts[2]
        if _RE_GRID.match(msg) or _RE_RPT.match(msg) or _RE_FINAL.match(msg):
            return True
        return True

    return False


def _extract_ascii_runs(payload: bytes, minlen: int = 6) -> List[str]:
    """
    Extract contiguous runs of printable ASCII from an arbitrary binary payload.

    WSJT-X packets often contain useful ASCII substrings (program id and decode line)
    even if the entire packet isn't a simple string.
    """
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
    """
    Parse WSJT-X UDP packet.

    Returns:
      (status_dial_hz, decoded_text)

    Exactly one of them is typically non-None:
      - For STATUS packets: (dial_hz, None)
      - For DECODE packets: (None, decoded_text)
      - For others / parse failures: (None, None)
    """
    if len(data) < 12:
        return None, None

    magic = int.from_bytes(data[0:4], "big", signed=False)
    _schema = int.from_bytes(data[4:8], "big", signed=False)
    mtype = int.from_bytes(data[8:12], "big", signed=False)

    if magic != MAGIC:
        return None, None

    if mtype == MSG_STATUS:
        # STATUS payload includes (among other fields) the "dial frequency" as u64 (Hz).
        try:
            s = _QtStream(data, 12)
            _id = s.qstring()  # Usually "WSJT-X"
            dial = s.u64()     # Dial frequency in Hz
            # Sanity range: 100 kHz .. 10 GHz
            if 100_000 <= dial <= 10_000_000_000:
                return int(dial), None
            return None, None
        except Exception:
            return None, None

    if mtype == MSG_DECODE:
        # We primarily recover decode text via ASCII-run scanning.
        runs = _extract_ascii_runs(data, minlen=6)

        # Common pattern: ["WSJT-X", "<decode line>"]
        if len(runs) >= 2 and runs[0] == "WSJT-X":
            decoded = runs[-1].strip()
            if decoded:
                return None, decoded

        # Fallback: pick the first run that looks like a decode line.
        for s in runs:
            if _looks_like_decode_line(s):
                return None, s

        return None, None

    return None, None


# =============================================================================
# FT8 line classification (sender callsign + optional grid extraction)
# =============================================================================

# Maidenhead locator extraction. This is optional and not required for DX classification here.
MAIDEN_RE = re.compile(r"\b[A-R]{2}\d{2}([A-X]{2})?\b", re.IGNORECASE)

# Callsign token extraction inside a message string.
CALL_RE = re.compile(r"\b([A-Z0-9]{1,3}[0-9][A-Z0-9]{1,4})(?:/[A-Z0-9]{1,4})?\b", re.IGNORECASE)


def is_blacklisted_callsign(sender: str, blacklist_prefixes: List[str]) -> bool:
    """
    Return True if the callsign should be considered "nearby / not DX".

    This is a simple prefix match. If blacklist contains "OM", then OM1ABC matches.
    If blacklist contains "I", then *any* Italian prefix starting with "I" matches.
    """
    u = sender.upper()
    for p in blacklist_prefixes:
        p = p.upper().strip()
        if p and u.startswith(p):
            return True
    return False


def parse_sender_and_grid_from_text(msg: str) -> Tuple[Optional[str], Optional[str], bool]:
    """
    Attempt to extract:
      - sender callsign (best effort)
      - a Maidenhead grid if present
      - whether the line is a CQ message

    For CQ lines, the "sender" is usually the CQ-calling station.
    For non-CQ lines, we pick the first callsign token.
    """
    up = msg.strip().upper()
    is_cq = up.startswith("CQ")
    tokens = up.split()

    grid = None
    m = MAIDEN_RE.search(up)
    if m:
        grid = m.group(0).upper()

    sender = None
    if is_cq:
        # CQ <CALL> <GRID> ...
        for t in tokens[1:4]:
            if CALL_RE.fullmatch(t):
                sender = t
                break
    else:
        # <CALL1> <CALL2> ...
        for t in tokens[:3]:
            if CALL_RE.fullmatch(t):
                sender = t
                break

    return sender, grid, is_cq


def now_utc_str() -> str:
    """Timestamp format used for CSV log."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


# =============================================================================
# Console coloring
# =============================================================================

GREEN = "\x1b[32m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"


def fmt_line(freq_hz: Optional[int], msg: str, is_dx: bool) -> str:
    """
    Build a console line. We include dial frequency (if known) at the beginning.

    Color scheme:
      - DX: green
      - non-DX: dim
    """
    f = "" if freq_hz is None else f"{freq_hz}Hz"
    core = f"{f} {msg}".strip()
    return f"{GREEN}{core}{RESET}" if is_dx else f"{DIM}{core}{RESET}"


# =============================================================================
# Main loop: UDP receive + LED state machine
# =============================================================================

def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: sudo {sys.argv[0]} /path/to/config.yaml", file=sys.stderr)
        return 2

    cfg_path = sys.argv[1]
    ft8, alert, gpio_cfg, udp = load_cfg(cfg_path)

    print(f"[INFO] cfg={cfg_path}", flush=True)
    print(f"[INFO] ft8: only_cq={ft8.only_cq} blacklist_prefixes={ft8.blacklist_prefixes}", flush=True)
    print(
        "[INFO] alert: "
        f"dx_hold_minutes={alert.dx_hold_minutes} "
        f"heartbeat_every_seconds={alert.heartbeat_every_seconds} "
        f"heartbeat_on_seconds={alert.heartbeat_on_seconds}",
        flush=True,
    )
    print(f"[INFO] csv: {alert.csv_path}", flush=True)
    print(
        f"[INFO] gpio: chip={gpio_cfg.chip} port={gpio_cfg.port} "
        f"line={gpio_cfg.resolved_line()} active_high={gpio_cfg.active_high}",
        flush=True,
    )
    print(f"[INFO] wsjtx_udp: bind={udp.bind_ip}:{udp.port}", flush=True)

    ensure_csv_header(alert.csv_path)

    led = LedGpiod(gpio_cfg)
    led.off()

    # Create UDP socket with a reasonably large receive buffer.
    # The socket timeout is important: it allows us to run the LED scheduler even
    # if no packets arrive for a while.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind((udp.bind_ip, udp.port))
    sock.settimeout(0.5)

    # Latest dial frequency from WSJT-X STATUS packets (Hz).
    dial_freq_hz: Optional[int] = None

    # Monotonic timestamp of the last DX event (for hold timer).
    last_dx_mono: Optional[float] = None

    # Convert hold minutes into seconds.
    hold_seconds = max(0, int(alert.dx_hold_minutes)) * 60

    # Heartbeat scheduling:
    #  - pulse_off_at: if a heartbeat pulse is currently active, this is when to turn it off.
    #  - next_heartbeat_at: schedule the next heartbeat ON transition.
    pulse_off_at: Optional[float] = None
    next_heartbeat_at: float = time.monotonic() + alert.heartbeat_every_seconds

    def led_tick(now: float) -> None:
        """
        Update LED state based on:
          - DX hold timer
          - heartbeat scheduler

        The ordering matters:
          - If DX is active -> LED is forced ON and heartbeat is suppressed.
          - If no DX -> heartbeat controls brief blinks.
        """
        nonlocal pulse_off_at, next_heartbeat_at, last_dx_mono

        dx_active = (last_dx_mono is not None) and ((now - last_dx_mono) < hold_seconds)

        if dx_active:
            led.on()
            pulse_off_at = None
            return

        # No DX active: handle heartbeat.
        if pulse_off_at is not None:
            # Heartbeat pulse is ON: check if it's time to turn it off.
            if now >= pulse_off_at:
                led.off()
                pulse_off_at = None
            return

        # Heartbeat pulse is OFF: check if it's time to start a new pulse.
        if now >= next_heartbeat_at:
            led.on()
            pulse_off_at = now + float(alert.heartbeat_on_seconds)
            next_heartbeat_at = now + float(alert.heartbeat_every_seconds)

    while True:
        # Always tick the LED scheduler. This ensures heartbeat continues even if
        # WSJT-X isn't sending packets for a while.
        led_tick(time.monotonic())

        # Receive UDP packet (or timeout).
        try:
            data, _addr = sock.recvfrom(65535)
        except socket.timeout:
            continue

        # Parse a single packet.
        status_dial, decoded_text = parse_wsjtx_packet(data)

        # STATUS: update dial frequency and continue.
        if status_dial is not None:
            dial_freq_hz = status_dial
            continue

        # Non-decode or unknown packet: ignore.
        if decoded_text is None:
            continue

        # Normalize the decode line slightly for token extraction.
        # Angle brackets often appear in some WSJT-X formats; we remove them to
        # simplify callsign scanning.
        clean = decoded_text.replace("<", "").replace(">", "").replace(";", " ")

        sender, grid, is_cq = parse_sender_and_grid_from_text(clean)

        # DX classification:
        # - If only_cq is set, non-CQ lines are not considered DX candidates.
        # - Otherwise, a sender is DX if NOT blacklisted by prefix.
        is_dx = False
        if sender:
            if ft8.only_cq and not is_cq:
                is_dx = False
            else:
                is_dx = not is_blacklisted_callsign(sender, ft8.blacklist_prefixes)

        # Print the line with frequency prefix.
        abs_freq = dial_freq_hz
        print(fmt_line(abs_freq, decoded_text, is_dx), flush=True)

        # If this is a DX sender, log it and arm the DX hold timer.
        if is_dx and sender:
            last_dx_mono = time.monotonic()
            ts = now_utc_str()
            append_csv(alert.csv_path, ts, abs_freq, sender, grid, decoded_text)

    # Unreachable
    # return 0


if __name__ == "__main__":
    raise SystemExit(main())
