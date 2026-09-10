# BPBS49 parameter poller

A small, dependency-free Python 3 tool that talks to a BHE Bonn Hungary
Electronics **BPBS49** SSPA final stage over its native TCP/IP "BPBS"
ASCII protocol (see the attached `Communication Protocol of the BPBS49
V1.1` document), reads all 28 monitored/set parameters with the `AA`
command, decodes them into named, typed values (voltages, currents,
temperature, forward/reflected power, ALC level, attenuation, network
settings, and all failure/warning/status bits), and logs the result.

## Why JSON as the internal format

JSON was chosen as the "master" format the tool builds internally,
because it is self-describing, human-readable, and the most widely
understood format across logging backends (files, MQTT, Node-RED,
Home Assistant, REST APIs, log shippers, etc.). From that same JSON
reading the tool can also directly generate:

- **InfluxDB line protocol** — written straight into InfluxDB (1.x or
  2.x) over HTTP, ready for Grafana.
- **CSV** — one row per poll, for spreadsheets or simple archiving.

No third-party libraries are required (only Python's standard
library: `socket`, `json`, `csv`, `urllib`), so it will run on pretty
much any machine with Python 3 installed, including small
single-board computers next to the amplifier.

## Files

- `bpbs49_poller.py` — the tool itself (also importable as a module).
- `test_bpbs49_poller.py` — self-tests, including a fake TCP server
  that mimics a BPBS49 unit, so the protocol logic (checksum, framing,
  bit decoding) can be verified without real hardware.

## Quick start

One-shot poll, print JSON to the terminal:

```bash
python3 bpbs49_poller.py --host 192.168.16.210
```

Poll every 10 seconds and append JSON lines to a file:

```bash
python3 bpbs49_poller.py --host 192.168.16.210 --interval 10 \
    --output json --out-file bpbs49_log.jsonl
```

Poll every 10 seconds and write directly into **InfluxDB 2.x**
(Grafana then reads from the bucket):

```bash
python3 bpbs49_poller.py --host 192.168.16.210 --interval 10 \
    --output influx --influx-version 2 \
    --influx-url http://localhost:8086 \
    --influx-org myorg --influx-bucket radio --influx-token <TOKEN>
```

Poll every 10 seconds and write into **InfluxDB 1.x**:

```bash
python3 bpbs49_poller.py --host 192.168.16.210 --interval 10 \
    --output influx --influx-version 1 \
    --influx-url http://localhost:8086 --influx-db bpbs49
```

Append rows to a **CSV** file:

```bash
python3 bpbs49_poller.py --host 192.168.16.210 --interval 30 \
    --output csv --out-file bpbs49_log.csv
```

Run it in the background (e.g. under `systemd` or `cron @reboot`) with
`--interval` set, and it will keep polling until stopped.

## What gets logged

Every reading includes, among others:

- `v9`, `v30` — supply voltages (V)
- `i9v`, `i30v`, `imw6`, `icgh1`, `icgh2` — supply/FET currents (A)
- `temp` — heat sink temperature (°C)
- `fwpw`, `reflpw` — forward/reflected power (dBm)
- `alc`, `att` — ALC level and attenuator set points
- `ip_address`, `net_mask`, `gateway`, `port`, `dhcp` — network config
  as reported by the unit
- Decoded booleans for every documented status/failure/warning bit
  (`fail_temp`, `fail_vswr`, `warn_v9`, `latched_fail_overdrive`,
  `rf_output_not_shutdown`, `fan_on`, `connected_clients`, ...),
  plus convenience summary flags `any_failure_active` and
  `any_warning_active` for a single Grafana alert rule.

Run `python3 bpbs49_poller.py --host <ip> --identify-once -v` once to
also capture the unit's identification field (serial number,
manufacturing date, hardware/firmware version) alongside the first
reading.

## Running the self-tests

```bash
python3 -m unittest test_bpbs49_poller.py -v
```

These tests build/parse frames per the protocol spec, verify the
checksum, decode the documented bit tables, and run a full poll
against an in-process fake BPBS49 server — no real amplifier needed.

## Notes / things to double check against your actual unit

- Default TCP port is 23, matching the factory default in the
  protocol document; change with `--port` if you reconfigured it.
- `--serial` defaults to `***` ("don't care" — any unit answers).
  If you have several BPBS49 units on the same network/port and want
  to address one specifically, set its 3-digit serial with
  `--serial 001` etc.
- The tool only *reads* parameters (`AA`/`AI`). The `Bpbs49Client`
  class also has `set_attenuator()`, `set_alc_level()`,
  `set_alc_enabled()` and `set_rf_power()` methods if you ever want to
  extend the tool to change settings, but the CLI does not expose
  those on purpose, to keep a monitoring tool from accidentally
  keying the PA off/on.
