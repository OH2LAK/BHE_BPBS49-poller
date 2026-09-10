"""
Self-tests for bpbs49_poller.py that don't require a real BPBS49 unit.

Run with:  python3 test_bpbs49_poller.py
"""
import socket
import threading
import unittest

import bpbs49_poller as m


class TestFrameBuilding(unittest.TestCase):
    def test_build_frame_matches_spec_example(self):
        # Spec example (4.1): "49***00SG=12.5"
        frame = m.build_frame("49", "***", "00", "SG", "12.5")
        self.assertTrue(frame.startswith("BPBS49***00SG=12.5"))
        self.assertTrue(frame.endswith("\r\n"))

    def test_checksum_roundtrip(self):
        body = "49***00AA="
        cs = m.checksum(body)
        self.assertEqual(len(cs), 2)
        # Checksum must be stable / deterministic
        self.assertEqual(cs, m.checksum(body))

    def test_build_frame_rejects_bad_lengths(self):
        with self.assertRaises(ValueError):
            m.build_frame("4", "***", "00", "AA")
        with self.assertRaises(ValueError):
            m.build_frame("49", "**", "00", "AA")
        with self.assertRaises(ValueError):
            m.build_frame("49", "***", "0", "AA")
        with self.assertRaises(ValueError):
            m.build_frame("49", "***", "00", "A")


class TestFrameParsing(unittest.TestCase):
    def test_parse_frame_roundtrip(self):
        frame = m.build_frame("49", "001", "00", "AA", "")
        raw_line = frame[len(m.FRAME_START):-2]  # strip 'BPBS' prefix and CRLF for parse_frame input
        # parse_frame expects the 'BPBS' prefix, so rebuild the line without CRLF only:
        line_no_crlf = frame[:-2]
        cmd, data, cs = m.parse_frame(line_no_crlf)
        self.assertEqual(cmd, "49001" + "00" + "AA=")
        self.assertEqual(data, "")

    def test_parse_frame_detects_bad_checksum(self):
        frame = m.build_frame("49", "***", "00", "AA", "")
        tampered = frame[:-4] + "00" + frame[-2:]  # corrupt the checksum bytes
        with self.assertRaises(m.Bpbs49ProtocolError):
            m.parse_frame(tampered[:-2])


class TestDecodeAAParameters(unittest.TestCase):
    def _sample_raw_values(self):
        # 28 values in the documented order, using the example values from
        # the protocol document's table (chapter 4.11), plus IP fields.
        return [
            "0000",   # general_status_raw
            "00",     # failure1_raw
            "00",     # failure2_raw
            "00",     # warning1_raw
            "00",     # warning2_raw
            "00",     # latched_failure1_raw
            "00",     # latched_failure2_raw
            "08.9",   # v9
            "29.7",   # v30
            "00.16",  # i9v
            "05.28",  # i30v
            "03.40",  # imw6
            "03.75",  # icgh1
            "+28.35", # temp
            "30.3",   # fwpw
            "05.4",   # reflpw
            "00.0",   # param17
            "00.48",  # ifan
            "42.5",   # alc
            "08.3",   # att
            "00",     # mixed_set_raw
            "00.0",   # param22
            "192.168.016.073",  # ip_address
            "255.255.255.000",  # net_mask
            "192.168.016.001",  # gateway
            "00023",  # port
            "1",      # dhcp
            "05.75",  # icgh2
        ]

    def test_decode_types(self):
        reading = m.decode_aa_parameters(self._sample_raw_values())
        self.assertAlmostEqual(reading["v9"], 8.9)
        self.assertAlmostEqual(reading["temp"], 28.35)
        self.assertEqual(reading["port"], 23)
        self.assertEqual(reading["dhcp"], 1)
        self.assertFalse(reading["any_failure_active"])
        self.assertFalse(reading["any_warning_active"])
        self.assertEqual(reading["connected_clients"], 0)

    def test_decode_failure_bits(self):
        values = self._sample_raw_values()
        values[1] = "05"  # failure1_raw: bit0 (temp) + bit2 (v9) set -> 0b00000101 = 0x05
        reading = m.decode_aa_parameters(values)
        self.assertTrue(reading["fail_temp"])
        self.assertTrue(reading["fail_v9"])
        self.assertFalse(reading["fail_vswr"])
        self.assertTrue(reading["any_failure_active"])

    def test_decode_general_status_connected_clients(self):
        values = self._sample_raw_values()
        # bits 8-10 = 3 (binary 011) -> hex 0300
        values[0] = "0300"
        reading = m.decode_aa_parameters(values)
        self.assertEqual(reading["connected_clients"], 3)


class TestInfluxLine(unittest.TestCase):
    def test_to_influx_line_basic(self):
        reading = {
            "timestamp_utc": "2026-09-10T12:00:00+00:00",
            "host": "192.168.16.210",
            "v9": 8.9,
            "port": 23,
            "any_failure_active": False,
            "ip_address": "192.168.016.073",
        }
        line = m.to_influx_line("bpbs49", reading)
        self.assertIn("bpbs49,host=192.168.16.210", line)
        self.assertIn("v9=8.9", line)
        self.assertIn("port=23i", line)
        self.assertIn("any_failure_active=false", line)


class TestEndToEndWithFakeServer(unittest.TestCase):
    """Spin up a fake TCP server implementing just enough of the BPBS49
    protocol to exercise Bpbs49Client end-to-end, without needing real
    hardware."""

    def setUp(self):
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.bind(("127.0.0.1", 0))
        self.server_sock.listen(1)
        self.port = self.server_sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve_one, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server_sock.close()

    def _serve_one(self):
        conn, _addr = self.server_sock.accept()
        with conn:
            data = b""
            while not data.endswith(b"\r\n"):
                data += conn.recv(256)
            request = data.decode("ascii")
            # Expect an AA request; respond with a canned, correctly checksummed frame.
            values = ",".join([
                "0000", "00", "00", "00", "00", "00", "00",
                "08.9", "29.7", "00.16", "05.28", "03.40", "03.75",
                "+28.35", "30.3", "05.4", "00.0", "00.48", "42.5", "08.3",
                "00", "00.0", "192.168.016.073", "255.255.255.000",
                "192.168.016.001", "00023", "1", "05.75",
            ]) + ","
            response = m.build_frame("49", "001", "00", "AA", values)
            conn.sendall(response.encode("ascii"))

    def test_ask_all_parameters(self):
        client = m.Bpbs49Client(host="127.0.0.1", port=self.port, timeout=2.0)
        client.connect()
        try:
            values = client.ask_all_parameters()
        finally:
            client.close()
        self.assertEqual(len(values), 28)
        reading = m.decode_aa_parameters(values)
        self.assertAlmostEqual(reading["v9"], 8.9)


if __name__ == "__main__":
    unittest.main()
