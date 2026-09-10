#!/usr/bin/env python3
"""
bpbs49_poller.py

Polling tool for the BHE Bonn Hungary Electronics BPBS49 SSPA final stage
amplifier ("pate-aste" / PA), using the vendor's ASCII/TCP-IP "BPBS"
communication protocol (see "Communication Protocol of the BPBS49", v1.1).

WHAT THIS TOOL DOES
--------------------
1. Opens a TCP connection to the BPBS49 unit (default port 23).
2. Sends the 'AA' command ("Asking all of the variable RF parameters"),
   optionally also the 'AI' command ("Asking Identification field of the
   unit") once at start-up.
3. Parses the raw ASCII response frame according to the protocol spec:
   - decodes the 28 AA parameters (voltages, currents, temperature,
     forward/reflected power, ALC level, attenuation, network settings...)
   - decodes the packed status/failure/warning bit-fields into named
     boolean flags, so downstream systems don't need to know about the
     BHE bit tables at all.
4. Emits ONE normalised reading as a flat Python dict (see
   `read_bpbs49()` / `to_flat_dict()`), which is the "lingua franca"
   internal representation.
5. From that flat dict, the tool can produce, on every poll:
   - JSON               (most general-purpose format; good for logging,
                          REST APIs, MQTT payloads, files, Node-RED, etc.)
   - InfluxDB line protocol (for InfluxDB 1.x /write or 2.x /api/v2/write,
                          which is what Grafana normally reads from)
   - CSV                (append to a spreadsheet-friendly log file)

Why JSON as the "master" format? It is self-describing (field names
travel with the data), trivially converted to line-protocol/XML/CSV/
whatever a specific logging backend wants, human-readable for
debugging, and understood natively by virtually every current logging
and dashboard tool (Telegraf, Node-RED, Home Assistant, MQTT brokers,
Loki, Elastic, plain files, ...). InfluxDB line-protocol is generated
directly from the same dict for the common Grafana/InfluxDB case, so
you do not need a separate agent like Telegraf if you don't want one.

USAGE EXAMPLES
---------------
One-shot poll, print JSON to stdout:
    python3 bpbs49_poller.py --host 192.168.16.210

Poll every 10 s, append JSON lines to a file:
    python3 bpbs49_poller.py --host 192.168.16.210 --interval 10 \\
        --output json --out-file bpbs49_log.jsonl

Poll every 10 s and write straight into InfluxDB 2.x:
    python3 bpbs49_poller.py --host 192.168.16.210 --interval 10 \\
        --output influx --influx-version 2 \\
        --influx-url http://localhost:8086 \\
        --influx-org myorg --influx-bucket radio --influx-token <TOKEN>

Poll every 10 s and write into InfluxDB 1.x:
    python3 bpbs49_poller.py --host 192.168.16.210 --interval 10 \\
        --output influx --influx-version 1 \\
        --influx-url http://localhost:8086 --influx-db bpbs49

Append CSV rows:
    python3 bpbs49_poller.py --host 192.168.16.210 --interval 30 \\
        --output csv --out-file bpbs49_log.csv

Only standard library is used (socket, json, csv, urllib) so nothing
needs to be pip-installed to use JSON/CSV output or InfluxDB writing.

Author: generated for Erik / Finskas Networks Oy.
"""

from __future__ import annotations

import argparse
import csv
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

FRAME_START = "BPBS"
FRAME_END = "\r\n"  # CR LF
DEFAULT_PORT = 23
DEFAULT_TIMEOUT = 5.0

# Order and names of the 28 parameters returned by the 'AA' command
# (see chapter 4.11 of the protocol document).
AA_PARAM_NAMES = [
    "general_status_raw",      # param1  Hex/4 ASCII
    "failure1_raw",            # param2  Hex/2 ASCII
    "failure2_raw",            # param3  Hex/2 ASCII
    "warning1_raw",            # param4  Hex/2 ASCII
    "warning2_raw",            # param5  Hex/2 ASCII
    "latched_failure1_raw",    # param6  Hex/2 ASCII
    "latched_failure2_raw",    # param7  Hex/2 ASCII
    "v9",                      # param8  V
    "v30",                     # param9  V
    "i9v",                     # param10 A
    "i30v",                    # param11 A  (sum of FET currents, not measured)
    "imw6",                    # param12 A  driver FET current
    "icgh1",                   # param13 A  end FET1 current
    "temp",                    # param14 degC
    "fwpw",                    # param15 dBm forward power
    "reflpw",                  # param16 dBm reflected power
    "param17_not_used",        # param17 not used
    "ifan",                    # param18 A fan current
    "alc",                     # param19 dBm ALC level (set point)
    "att",                     # param20 dB attenuation (set point)
    "mixed_set_raw",           # param21 Hex/2 ASCII
    "param22_not_used",        # param22 not used
    "ip_address",              # param23
    "net_mask",                # param24
    "gateway",                 # param25
    "port",                    # param26
    "dhcp",                    # param27
    "icgh2",                   # param28 A end FET2 current
]

FLOAT_FIELDS = {
    "v9", "v30", "i9v", "i30v", "imw6", "icgh1", "temp", "fwpw", "reflpw",
    "ifan", "alc", "att", "icgh2",
}
INT_FIELDS = {"port", "dhcp"}

# --- bit tables -------------------------------------------------------------
# Each entry: bit position -> flag name. Value 1 means "true" for that flag
# in the sense described in the protocol document (failure/warning/on/etc).

GENERAL_STATUS_BITS = {
    0: "rf_output_not_shutdown",   # 0 = RF output shut down, 1 = not shut down
    2: "warning_present",
    3: "failure_latched",
    4: "fan_on",
    7: "hw_temp_shutdown",
    11: "self_test_mode_inactive",  # 0 = self test active, 1 = not active
    12: "manual_fan_on",
    13: "failure_momentary",
}
# bits 8..10 form a 3-bit number: number of connected clients (redundant mode)

FAILURE1_BITS = {
    0: "fail_temp",
    1: "fail_vswr",
    2: "fail_v9",
    3: "fail_v30",
    4: "fail_overdrive",
}

FAILURE2_BITS = {
    0: "fail_i9v",
    3: "fail_imw6",
    4: "fail_icgh1",
    5: "fail_ifan",
    6: "fail_icgh2",
}

WARNING1_BITS = {
    0: "warn_temp",
    1: "warn_vswr",
    2: "warn_v9",
    3: "warn_v30",
}

WARNING2_BITS = {
    0: "warn_i9v",
    3: "warn_imw6",
    4: "warn_icgh1",
    6: "warn_icgh2",
}

LATCHED_FAILURE1_BITS = {
    0: "latched_fail_temp",
    1: "latched_fail_vswr",
    2: "latched_fail_v9",
    3: "latched_fail_v30",
    4: "latched_fail_overdrive",
}

# param7 uses the same bit layout as failure2 per the protocol document.
LATCHED_FAILURE2_BITS = {
    0: "latched_fail_i9v",
    3: "latched_fail_imw6",
    4: "latched_fail_icgh1",
    5: "latched_fail_ifan",
    6: "latched_fail_icgh2",
}

MIXED_SET_BITS = {
    2: "rf_power_on_by_command",    # SR: 0 off, 1 on
    3: "alc_on_by_command",         # SS: 0 off, 1 on
    4: "redundant_mode_detected",   # 0 standalone, 1 redundant
    7: "sw_rf_shutdown_enabled",
}


def _decode_bits(raw_hex: str, table: Dict[int, str]) -> Dict[str, bool]:
    """Decode a Hex/2 (or Hex/4) ASCII byte into named booleans."""
    value = int(raw_hex, 16)
    return {name: bool((value >> bit) & 1) for bit, name in table.items()}


# ---------------------------------------------------------------------------
# Low level protocol helpers
# ---------------------------------------------------------------------------

def checksum(text: str) -> str:
    """Modulo-256 sum of the ASCII byte values, as two upper-case hex digits.

    Per the protocol: 'the checksum byte is result of a modulo 256 addition
    of the character bytes of the message from the first character of the
    Command Field to the last character of the Data Field.'
    """
    total = sum(ord(c) for c in text) % 256
    return f"{total:02X}"


def build_frame(subtype: str, serial: str, ctrl: str, code: str, data: str = "") -> str:
    """Build a full BPBS49 command frame ready to be sent over the socket."""
    if len(subtype) != 2:
        raise ValueError("subtype must be exactly 2 characters, e.g. '49' or '**'")
    if len(serial) != 3:
        raise ValueError("serial must be exactly 3 characters, e.g. '001' or '***'")
    if len(ctrl) != 2:
        raise ValueError("ctrl must be exactly 2 hex-ascii characters, e.g. '00'")
    if len(code) != 2:
        raise ValueError("code must be exactly 2 characters, e.g. 'AA'")

    command_field = f"{subtype}{serial}{ctrl}{code}="
    body = command_field + data
    cs = checksum(body)
    return FRAME_START + body + cs + FRAME_END


class Bpbs49ProtocolError(RuntimeError):
    pass


def parse_frame(raw: str) -> Tuple[str, str, str]:
    """Parse a raw response line (without trailing CR/LF) into
    (command_field_without_equals, data_field, received_checksum).

    Raises Bpbs49ProtocolError if the frame is malformed or the checksum
    does not match.
    """
    raw = raw.strip("\r\n")
    if not raw.startswith(FRAME_START):
        raise Bpbs49ProtocolError(f"Frame does not start with '{FRAME_START}': {raw!r}")

    body = raw[len(FRAME_START):]
    if len(body) < 10 + 2:
        raise Bpbs49ProtocolError(f"Frame too short: {raw!r}")

    command_field = body[:10]
    if command_field[9] != "=":
        raise Bpbs49ProtocolError(f"10th char of command field is not '=': {raw!r}")

    rest = body[10:]
    data_field = rest[:-2]
    received_cs = rest[-2:]

    expected_cs = checksum(command_field + data_field)
    if expected_cs != received_cs.upper():
        raise Bpbs49ProtocolError(
            f"Checksum mismatch: expected {expected_cs}, got {received_cs} in {raw!r}"
        )

    return command_field, data_field, received_cs


# ---------------------------------------------------------------------------
# TCP client
# ---------------------------------------------------------------------------

@dataclass
class Bpbs49Client:
    host: str
    port: int = DEFAULT_PORT
    timeout: float = DEFAULT_TIMEOUT
    subtype: str = "49"     # '49' for BPBS49, '**' don't-care
    serial: str = "***"     # '***' don't-care, or e.g. '001'
    ctrl: str = "00"        # control byte, '00' = PC source, master dest, answer requested
    _sock: Optional[socket.socket] = field(default=None, init=False, repr=False)

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "Bpbs49Client":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _send_and_receive(self, code: str, data: str = "") -> Tuple[str, str]:
        if self._sock is None:
            raise RuntimeError("Not connected - use 'with Bpbs49Client(...) as c:' or call connect()")

        frame = build_frame(self.subtype, self.serial, self.ctrl, code, data)
        self._sock.sendall(frame.encode("ascii"))

        # Read until CRLF terminator (frames are short, so this is fine).
        buf = b""
        self._sock.settimeout(self.timeout)
        while not buf.endswith(b"\r\n"):
            chunk = self._sock.recv(256)
            if not chunk:
                raise Bpbs49ProtocolError("Connection closed before a full frame was received")
            buf += chunk

        line = buf.decode("ascii", errors="replace")
        command_field, data_field, _cs = parse_frame(line)
        return command_field, data_field

    def ask_all_parameters(self) -> List[str]:
        """Send 'AA' and return the raw list of 28 parameter strings."""
        _cmd, data_field = self._send_and_receive("AA")
        # Data field looks like "v1,v2,...,v28," (trailing comma) -> strip empties
        parts = [p for p in data_field.split(",")]
        if parts and parts[-1] == "":
            parts = parts[:-1]
        if len(parts) != len(AA_PARAM_NAMES):
            raise Bpbs49ProtocolError(
                f"Expected {len(AA_PARAM_NAMES)} parameters from AA, got {len(parts)}: {parts}"
            )
        return parts

    def ask_identification(self) -> Dict[str, str]:
        """Send 'AI' and return the decoded identification fields."""
        _cmd, data_field = self._send_and_receive("AI")
        # AI reply is a fixed 30-char string with no comma separators.
        raw = data_field
        if len(raw) < 30:
            raw = raw.ljust(30, "\x00")
        serial_no = raw[0:10].lstrip("\x00")
        mfg_date = raw[10:18]  # YYYYMMDD
        unit_type = raw[18:26]
        hw_version = raw[26:28]
        fw_version = raw[28:30]
        return {
            "serial_number": serial_no,
            "manufacturing_date": mfg_date,
            "unit_type": unit_type.strip("\x00"),
            "hw_version": f"{hw_version[0]}.{hw_version[1]}" if len(hw_version) == 2 else hw_version,
            "fw_version": f"{fw_version[0]}.{fw_version[1]}" if len(fw_version) == 2 else fw_version,
        }

    # -- convenience set commands (not needed for pure logging, but handy) --

    def set_attenuator(self, db: float) -> None:
        self._send_and_receive("SG", f"{db}")

    def set_alc_level(self, dbm: float) -> None:
        self._send_and_receive("SA", f"{dbm}")

    def set_alc_enabled(self, enabled: bool) -> None:
        self._send_and_receive("SS", "1" if enabled else "0")

    def set_rf_power(self, enabled: bool) -> None:
        self._send_and_receive("SR", "1" if enabled else "0")


# ---------------------------------------------------------------------------
# Decoding raw AA parameters into a normalised, flat reading
# ---------------------------------------------------------------------------

def decode_aa_parameters(raw_values: List[str]) -> Dict[str, Any]:
    """Turn the 28 raw strings from 'AA' into typed values + decoded bit flags."""
    raw = dict(zip(AA_PARAM_NAMES, raw_values))
    out: Dict[str, Any] = {}

    for name, value in raw.items():
        if name in FLOAT_FIELDS:
            try:
                out[name] = float(value)
            except ValueError:
                out[name] = None
        elif name in INT_FIELDS:
            try:
                out[name] = int(value)
            except ValueError:
                out[name] = None
        elif name in ("ip_address", "net_mask", "gateway"):
            out[name] = value
        elif name.endswith("_raw"):
            out[name] = value  # keep the raw hex string too, for troubleshooting
        else:
            out[name] = value

    # i30v is documented as *not* a directly-measured value (sum of FET currents),
    # but it is still returned as a plain xx.xx field, so it is parsed above already.

    gen_status_hex = raw["general_status_raw"]
    gen_status_val = int(gen_status_hex, 16)
    out.update(_decode_bits(gen_status_hex, GENERAL_STATUS_BITS))
    out["connected_clients"] = (gen_status_val >> 8) & 0x7

    out.update(_decode_bits(raw["failure1_raw"], FAILURE1_BITS))
    out.update(_decode_bits(raw["failure2_raw"], FAILURE2_BITS))
    out.update(_decode_bits(raw["warning1_raw"], WARNING1_BITS))
    out.update(_decode_bits(raw["warning2_raw"], WARNING2_BITS))
    out.update(_decode_bits(raw["latched_failure1_raw"], LATCHED_FAILURE1_BITS))
    out.update(_decode_bits(raw["latched_failure2_raw"], LATCHED_FAILURE2_BITS))
    out.update(_decode_bits(raw["mixed_set_raw"], MIXED_SET_BITS))

    # A single convenience "any_failure" / "any_warning" summary flag, handy
    # for a single Grafana alert rule instead of watching many booleans.
    out["any_failure_active"] = any(
        out[k] for k in list(FAILURE1_BITS.values()) + list(FAILURE2_BITS.values())
    )
    out["any_warning_active"] = any(
        out[k] for k in list(WARNING1_BITS.values()) + list(WARNING2_BITS.values())
    )

    return out


def read_bpbs49(
    host: str,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    subtype: str = "49",
    serial: str = "***",
    include_identification: bool = False,
) -> Dict[str, Any]:
    """Connect once, poll AA (and optionally AI), return one flat reading dict."""
    with Bpbs49Client(host=host, port=port, timeout=timeout, subtype=subtype, serial=serial) as client:
        raw_values = client.ask_all_parameters()
        reading = decode_aa_parameters(raw_values)
        if include_identification:
            try:
                reading["identification"] = client.ask_identification()
            except Bpbs49ProtocolError as exc:
                reading["identification_error"] = str(exc)

    reading["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    reading["host"] = host
    return reading


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

# Fields that are numeric measurements -> InfluxDB "fields".
# Everything else that identifies the source -> InfluxDB "tags".
INFLUX_TAG_KEYS = {"host", "ip_address"}


def to_influx_line(measurement: str, reading: Dict[str, Any], extra_tags: Optional[Dict[str, str]] = None) -> str:
    """Convert a flat reading dict into a single InfluxDB line-protocol line."""
    tags: Dict[str, str] = {}
    fields: Dict[str, Any] = {}

    for key, value in reading.items():
        if key in ("timestamp_utc", "identification", "identification_error"):
            continue
        if key in INFLUX_TAG_KEYS:
            tags[key] = str(value).replace(" ", "_")
        elif isinstance(value, bool):
            fields[key] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            fields[key] = value
        else:
            # strings become quoted string fields (net_mask, gateway, raw hex bytes, ...)
            escaped = str(value).replace('"', '\\"')
            fields[key] = f'"{escaped}"'

    if extra_tags:
        for k, v in extra_tags.items():
            tags[k] = str(v).replace(" ", "_")

    tag_str = "".join(f",{k}={v}" for k, v in sorted(tags.items()))

    field_parts = []
    for k, v in fields.items():
        if isinstance(v, bool):
            field_parts.append(f"{k}={v}")
        elif isinstance(v, int):
            field_parts.append(f"{k}={v}i")
        elif isinstance(v, float):
            field_parts.append(f"{k}={v}")
        else:
            field_parts.append(f"{k}={v}")
    field_str = ",".join(field_parts)

    ts = reading.get("timestamp_utc")
    ts_ns = ""
    if ts:
        dt = datetime.fromisoformat(ts)
        ts_ns = str(int(dt.timestamp() * 1_000_000_000))

    line = f"{measurement}{tag_str} {field_str}"
    if ts_ns:
        line += f" {ts_ns}"
    return line


def write_json(reading: Dict[str, Any], out_file: Optional[str]) -> None:
    line = json.dumps(reading, ensure_ascii=False)
    if out_file:
        with open(out_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    else:
        print(line)


def write_csv(reading: Dict[str, Any], out_file: str) -> None:
    file_exists = False
    try:
        with open(out_file, "r", encoding="utf-8"):
            file_exists = True
    except FileNotFoundError:
        pass

    fieldnames = list(reading.keys())
    with open(out_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(reading)


def write_influx(
    reading: Dict[str, Any],
    measurement: str,
    influx_version: int,
    influx_url: str,
    influx_org: Optional[str],
    influx_bucket: Optional[str],
    influx_token: Optional[str],
    influx_db: Optional[str],
    verbose: bool = False,
) -> None:
    line = to_influx_line(measurement, reading)
    if verbose:
        print(f"[influx line] {line}", file=sys.stderr)

    if influx_version == 2:
        url = f"{influx_url.rstrip('/')}/api/v2/write?org={influx_org}&bucket={influx_bucket}&precision=ns"
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if influx_token:
            headers["Authorization"] = f"Token {influx_token}"
    else:
        url = f"{influx_url.rstrip('/')}/write?db={influx_db}&precision=ns"
        headers = {"Content-Type": "text/plain; charset=utf-8"}

    req = urllib.request.Request(url, data=line.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 204):
                print(f"WARNING: InfluxDB returned HTTP {resp.status}", file=sys.stderr)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"ERROR writing to InfluxDB: HTTP {exc.code}: {body}", file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"ERROR writing to InfluxDB: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Poll a BHE BPBS49 SSPA final stage over TCP/IP and log its parameters."
    )
    p.add_argument("--host", required=True, help="IP address or hostname of the BPBS49 unit")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"TCP port (default {DEFAULT_PORT})")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Socket timeout in seconds")
    p.add_argument("--subtype", default="49", help="Command field subtype, default '49'")
    p.add_argument("--serial", default="***", help="Command field serial (3 chars), default '***' (don't care)")
    p.add_argument("--interval", type=float, default=0,
                    help="Polling interval in seconds. 0 (default) = poll once and exit.")
    p.add_argument("--identify-once", action="store_true",
                    help="Also fetch the AI identification field on the first poll.")

    p.add_argument("--output", choices=["json", "csv", "influx"], default="json",
                    help="Output format for each reading (default: json)")
    p.add_argument("--out-file", help="File to append JSON lines or CSV rows to. "
                                       "For --output json, omit to print to stdout instead.")
    p.add_argument("--measurement", default="bpbs49",
                    help="InfluxDB measurement name (default: bpbs49)")

    p.add_argument("--influx-version", type=int, choices=[1, 2], default=2, help="InfluxDB API version")
    p.add_argument("--influx-url", default="http://localhost:8086", help="InfluxDB base URL")
    p.add_argument("--influx-org", help="InfluxDB 2.x organisation")
    p.add_argument("--influx-bucket", help="InfluxDB 2.x bucket")
    p.add_argument("--influx-token", help="InfluxDB 2.x API token")
    p.add_argument("--influx-db", help="InfluxDB 1.x database name")

    p.add_argument("-v", "--verbose", action="store_true", help="Print extra diagnostic information")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    if args.output == "influx":
        if args.influx_version == 2 and not (args.influx_org and args.influx_bucket):
            print("ERROR: --influx-org and --influx-bucket are required for InfluxDB v2", file=sys.stderr)
            return 2
        if args.influx_version == 1 and not args.influx_db:
            print("ERROR: --influx-db is required for InfluxDB v1", file=sys.stderr)
            return 2

    first_poll = True
    try:
        while True:
            try:
                reading = read_bpbs49(
                    host=args.host,
                    port=args.port,
                    timeout=args.timeout,
                    subtype=args.subtype,
                    serial=args.serial,
                    include_identification=(args.identify_once and first_poll),
                )
                first_poll = False

                if args.output == "json":
                    write_json(reading, args.out_file)
                elif args.output == "csv":
                    if not args.out_file:
                        print("ERROR: --out-file is required for --output csv", file=sys.stderr)
                        return 2
                    write_csv(reading, args.out_file)
                elif args.output == "influx":
                    write_influx(
                        reading,
                        measurement=args.measurement,
                        influx_version=args.influx_version,
                        influx_url=args.influx_url,
                        influx_org=args.influx_org,
                        influx_bucket=args.influx_bucket,
                        influx_token=args.influx_token,
                        influx_db=args.influx_db,
                        verbose=args.verbose,
                    )

                if args.verbose:
                    print(f"OK: polled {args.host} at {reading['timestamp_utc']}", file=sys.stderr)

            except (OSError, Bpbs49ProtocolError) as exc:
                print(f"ERROR polling {args.host}:{args.port}: {exc}", file=sys.stderr)

            if args.interval <= 0:
                break
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
