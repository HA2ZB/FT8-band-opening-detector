# WSPR UDP Watcher with GPIO LED (Orange Pi)

This is the "twin project" of **FT8-band-opening-detector** (https://github.com/HA2ZB/FT8-band-opening-detector).

This project monitors **WSJT-X WSPR decodes via UDP**, classifies stations as **DX / non-DX** using a configurable prefix blacklist, and provides:

- **Visual indication via GPIO LED**
  - steady ON while DX activity is present
  - periodic heartbeat blink while idle
- **CSV logging** of DX-related decodes
- **Headless / semi-headless operation**, suitable for Orange Pi / Raspberry Pi class systems

The design assumes **receive-only WSPR monitoring** using an RTL-SDR + SpyVerter, with WSJT-X running over VNC.

The primary target was to continuously monitor the "magic" 6-meter band for Es openings.

---

## System overview

```
RTL-SDR + SpyVerter
        │
        ▼
rtl_sdr  →  sox (I/Q → audio)  →  PulseAudio null sink
                                              │
                                              ▼
                                         WSJT-X (VNC)
                                              │
                                              ▼
                                     WSJT-X UDP messages
                                              │
                                              ▼
                                      wsprwatch_udp.py
                                              │
                           ┌──────────────────┴──────────────────┐
                           ▼                                     ▼
                       GPIO LED                              CSV log
```

---

## Requirements

### Hardware
- Orange Pi (tested on Orange Pi 4A)
- RTL-SDR dongle
- SpyVerter (or equivalent HF upconverter)
- GPIO-connected LED (example: PI6 → GPIO line 262)

### Software
- Linux with PulseAudio (Orange Pi 1.0.4 Jammy with Linux 5.15.147-sun55iw3)
- Python 3.9+
- WSJT-X
- rtl-sdr
- sox
- libgpiod + python3-gpiod

---

## Installing dependencies (example)

```bash
sudo apt update
sudo apt install -y \
  wsjtx \
  rtl-sdr \
  sox \
  pulseaudio \
  python3-yaml \
  python3-gpiod
```

---

## Starting WSJT-X via VNC

WSJT-X is started with its GUI exported over VNC:

```bash
wsjtx -platform vnc &
```

Connect using any VNC viewer from another machine.

---

## WSJT-X configuration (important)

Inside the WSJT-X GUI (via VNC):

### 1. Audio
- **Input**: PulseAudio
- **Device**: `WSJTX_SINK`

### 2. Mode and band
- Mode: **WSPR**
- Band: must match the actually received band (e.g. 20 m = 14.074 MHz)

### 3. Reporting → UDP server
- Enable **UDP Server**
- Address: `127.0.0.1`
- Port: `2237` (must match `config.yaml`)
- Accept UDP requests: **enabled**

WSJT-X continuously sends:
- *Status packets* (including dial frequency)
- *Decode packets* (WSPR messages)

These packets are consumed by `wspr8watch_udp.py`.

---

## Creating the PulseAudio audio sink

WSJT-X does not read audio directly from `rtl_sdr`.  
Instead, a **virtual PulseAudio null sink** is used.

```bash
pactl load-module module-null-sink \
  sink_name=wsjtx_sink \
  sink_properties=device.description=WSJTX_SINK
```

This creates a sink named `wsjtx_sink`, which WSJT-X uses as its audio input.

---

## Starting the radio / SDR audio chain

Example for **6 m WSPR (50.293 MHz)** using a SpyVerter:

```bash
sudo rtl_biast -b 1

HF_KHZ=50293
LO_KHZ=120000
FS=1200000
TUNE_HZ=$(( (HF_KHZ + LO_KHZ) * 1000 ))

rtl_sdr -f ${TUNE_HZ} -s ${FS} -g 22 - | \
sox -t raw -r 1200000 -e unsigned -b 8 -c 2 - \
    -t raw -r 12000 -e signed -b 16 -c 1 - | \
pacat --raw --format=s16le --rate=12000 --channels=1 --device=wsjtx_sink
```

Explanation:
- `rtl_biast -b 1` enables Bias-T power for the SpyVerter
- `rtl_sdr` captures I/Q samples
- `sox` converts raw I/Q to 12 kHz audio
- `pacat` feeds the audio into the PulseAudio sink

At this point:
- WSJT-X should show a waterfall
- WSPR decodes should appear in the WSJT-X window

---

## Running the WSPR UDP watcher

```bash
sudo python3 wsprwatch_udp.py /home/orangepi/WSPR/config.yaml
```

On startup the script prints:
- parsed configuration
- GPIO mapping
- UDP bind address

During operation:
- WSPR decodes are printed to the console
- DX lines are highlighted
- LED state reflects DX / idle status

---

## LED behavior

- **DX detected**
  - LED is turned ON
  - stays ON for `dx_hold_minutes` after the last DX decode
- **No DX**
  - LED blinks periodically as a heartbeat
  - controlled by `heartbeat_every_seconds` and `heartbeat_on_seconds`

This allows quick visual indication of band openings without watching the screen.

---

## CSV logging

DX-related decodes are appended to:

```
/home/orangepi/WSPR/wspr_dx_log.csv
```

CSV format:

```
timestamp_utc,freq_hz,sender_callsign,grid,snr,raw_line
```

The file can be safely monitored while the script is running:

```bash
tail -f /home/orangepi/WSPR/wspr_dx_log.csv
```

Reading the file does **not** interfere with logging.

---

## GPIO notes (Orange Pi example)

- Logical port: `PI6`
- Resolved internally to **GPIO line 262**
- Uses `libgpiod` (not legacy sysfs GPIO)

Polarity is configurable in `config.yaml`:

```yaml
gpio:
  active_high: true
```
LED is connected to `PI6` with a 5k resistor to GND.

---

## Notes and limitations

- DX classification is **prefix-based**, not distance-based
- Grid information may be missing from individual decodes and is optional
- Dial frequency is taken from WSJT-X status packets  
  (ensure the WSJT-X band matches the actual manually tuned band)

---

## License

This project is released under the MIT License.

You are free to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of this software, provided that the original copyright notice and this permission notice are included in all copies or substantial portions of the software.

This project is intended as a practical utility for the amateur radio community.
While not required by the license, contributions, improvements, and bug fixes are always welcome and greatly appreciated.

For the full license text, see the LICENSE file in this repository.

This project was developed by the author with iterative assistance from AI-based coding tools.

---

This setup is intentionally modular and transparent, making it easy to adapt to:
- different bands
- different SDR front-ends
- alternative alerting mechanisms

