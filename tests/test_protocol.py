"""Unit tests for the protocol client, CLI helpers and device discovery.

No hardware needed — run with:
    PYTHONPATH=src python3 -m unittest discover -s tests -v
"""
import fnmatch
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

from picogremlin_la import cli, protocol as p, transport  # noqa: E402

try:
    import numpy as _np
    HAVE_NUMPY = True
except ImportError:      # the unit tests must pass without the [gui] extra
    HAVE_NUMPY = False


class FramingTest(unittest.TestCase):
    def test_id_command_is_a_plain_frame(self):
        self.assertEqual(p.LA_ID_CMD, p.la_frame(0))

    def test_stop_frame_bytes(self):
        self.assertEqual(p.la_frame(0x42), bytes([0x55, 0xAA, 0x42, 0xAA, 0x55]))

    def test_payload_escaping(self):
        # 0xAA, 0x55 and 0xF0 are escaped as F0 <b XOR F0>
        frame = p.la_frame(1, bytes([0xAA, 0x55, 0xF0, 0x00]))
        self.assertEqual(
            frame,
            bytes([0x55, 0xAA, 0x01,
                   0xF0, 0xAA ^ 0xF0, 0xF0, 0x55 ^ 0xF0, 0xF0, 0xF0 ^ 0xF0, 0x00,
                   0xAA, 0x55]))

    def test_capture_request_layout(self):
        req = p.la_build_capture_request(0, 14, 1, 0, [14, 15], 1_000_000,
                                         1000, 20000, 0, 0, 2)
        self.assertEqual(len(req), 48)
        tt, trig, inv, tv = struct.unpack_from("<BBBxH", req, 0)
        self.assertEqual((tt, trig, inv, tv), (0, 14, 1, 0))
        self.assertEqual(req[6:8], bytes([14, 15]))          # channel list
        self.assertEqual(req[8:30], bytes(22))               # padded to 24
        self.assertEqual(req[30], 2)                         # channel count
        freq, pre, post = struct.unpack_from("<III", req, 32)
        self.assertEqual((freq, pre, post), (1_000_000, 1000, 20000))
        loop, measure, mode = struct.unpack_from("<BBB", req, 44)
        self.assertEqual((loop, measure, mode), (0, 0, 2))


class DecodeTest(unittest.TestCase):
    def test_decode_samples_widths(self):
        self.assertEqual(list(p.decode_samples(b"\x01\x02", 1)), [1, 2])
        self.assertEqual(list(p.decode_samples(b"\x01\x00\xff\xff", 2)), [1, 0xFFFF])
        self.assertEqual(list(p.decode_samples(struct.pack("<III", 1, 2, 3), 4)),
                         [1, 2, 3])

    def test_chan_pin_map(self):
        self.assertEqual(p.chan_pin(14), 16)     # GP16
        self.assertEqual(p.chan_pin(23), 28)     # GP28
        self.assertEqual(p.CHAN_OF_PIN[16], 14)
        self.assertEqual(len(p.CHAN_OF_PIN), 24)

    def test_mode_bps(self):
        self.assertEqual(p.mode_bps([0, 7]), (0, 1))
        self.assertEqual(p.mode_bps([0, 15]), (1, 2))
        self.assertEqual(p.mode_bps([14, 15]), (1, 2))
        self.assertEqual(p.mode_bps([0, 23]), (2, 4))

    def test_clamp_rate(self):
        rate, note = p.clamp_rate(200_000, 2)
        self.assertEqual(rate, 200_000)
        self.assertIsNone(note)
        rate, note = p.clamp_rate(2_000_000, 4)
        self.assertLessEqual(rate * 4, p.MAX_STREAM_BYTES)
        self.assertIn("link tops out", note)


class MetricsTest(unittest.TestCase):
    def test_channel_metrics_square_wave(self):
        # 50 samples high, 50 low, repeated -> 1 kHz at 100 kHz sampling
        bits = ([1] * 50 + [0] * 50) * 10
        m = p.channel_metrics(bits, 1, 100_000, nch=1)
        self.assertEqual(m[0]["edge_count"], 19)
        self.assertAlmostEqual(m[0]["frequency_hz"], 1000.0, delta=1.0)
        self.assertAlmostEqual(m[0]["duty_percent"], 50.0, delta=1.0)

    @unittest.skipUnless(HAVE_NUMPY, "numpy not installed (only the [gui] extra needs it)")
    def test_quick_metrics(self):
        bits = _np.array(([1] * 50 + [0] * 50) * 10, dtype=_np.uint32)
        m = p.quick_metrics(bits, 100_000, [14], [0], _np)
        self.assertAlmostEqual(m[14][0], 1000.0, delta=1.0)
        self.assertAlmostEqual(m[14][1], 50.0, delta=1.0)


class CliHelpersTest(unittest.TestCase):
    def test_parse_channels(self):
        self.assertEqual(cli.parse_channels("14,15"), [14, 15])
        self.assertEqual(cli.parse_channels("GP16,GP17"), [14, 15])
        self.assertEqual(cli.parse_channels("0-3"), [0, 1, 2, 3])
        self.assertEqual(cli.parse_channels(""), list(range(24)))
        with self.assertRaises(SystemExit):
            cli.parse_channels("99")

    def test_parse_rate(self):
        self.assertEqual(cli.parse_rate("1M"), 1_000_000)
        self.assertEqual(cli.parse_rate("200k"), 200_000)
        self.assertEqual(cli.parse_rate("500"), 500)

    def test_parse_trigger(self):
        self.assertEqual(cli.parse_trigger("14"), 14)
        self.assertEqual(cli.parse_trigger("GP16"), 14)


class TransportTest(unittest.TestCase):
    BY_ID = [
        "/dev/serial/by-id/usb-PicoGremlin_CMSIS-DAP-Debugger+LogicAnalyzer_AAAAAAAAAAAAAAAA-if01",
        "/dev/serial/by-id/usb-PicoGremlin_CMSIS-DAP-Debugger+LogicAnalyzer_AAAAAAAAAAAAAAAA-if03",
        "/dev/serial/by-id/usb-PicoGremlin_CMSIS-DAP-Debugger+LogicAnalyzer_BBBBBBBBBBBBBBBB-if01",
        "/dev/serial/by-id/usb-PicoGremlin_CMSIS-DAP-Debugger+LogicAnalyzer_BBBBBBBBBBBBBBBB-if03",
        "/dev/serial/by-id/usb-Other_Device_CCCCCCCCCCCCCCCC-if00",
    ]

    def _find(self, serial, only=None):
        """find_ports() against a fake /dev/serial/by-id (fnmatch on the pattern)."""
        entries = [x for x in self.BY_ID if only is None or only in x]

        def fake_glob(pattern):
            return [x for x in entries if fnmatch.fnmatch(x, pattern)]

        with mock.patch.object(transport, "PICOGREMLIN_SERIAL", serial), \
             mock.patch.object(transport.glob, "glob", side_effect=fake_glob):
            return transport.find_ports()

    def test_single_board(self):
        uart, la = self._find(None, only="AAAA")
        self.assertTrue(uart.endswith("-if01"))
        self.assertTrue(la.endswith("-if03"))
        self.assertIn("AAAAAAAAAAAAAAAA", la)

    def test_ambiguous_without_serial(self):
        # two boards attached and no serial chosen -> no port matches
        self.assertEqual(self._find(None), (None, None))

    def test_serial_filter(self):
        uart, la = self._find("AAAAAAAAAAAAAAAA")
        self.assertTrue(uart.endswith("-if01"))
        self.assertTrue(la.endswith("-if03"))
        self.assertNotIn("BBBB", uart)
        self.assertNotIn("BBBB", la)


if __name__ == "__main__":
    unittest.main()
