"""PicoGremlin logic-analyzer protocol client.

Wire protocol for the PicoGremlin firmware: the stock gusmanb command set
(CMD 0..6) plus the PicoGremlin extensions (CMD 0x40..0x48). No third-party
dependencies; the wire format is documented in the firmware repository's
docs/LA_PROTOCOL.md.
"""
import fcntl
import os
import select
import struct
import sys
import termios
import time
from array import array

# ---- LA protocol constants ---------------------------------------------------
LA_ID_CMD = bytes([0x55, 0xAA, 0x00, 0xAA, 0x55])
LA_FRAME_START = bytes([0x55, 0xAA])
LA_FRAME_END = bytes([0xAA, 0x55])
LA_ESC = 0xF0

# ---- serial port (Linux tty) -------------------------------------------------
# Linux termios2 ioctl numbers (not exposed by Python's termios module).
_TCGETS2 = 0x802C542A
_TCSETS2 = 0x402C542B
_BOTHER = 0x1000
_TERMIOS2_SIZE = 44


class SerialPort:
    """Thin wrapper over a Linux tty with baud/DTR/read-write control."""

    def __init__(self, path, baud=115200, timeout=2.0):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        self.baud = baud
        attrs = termios.tcgetattr(self.fd)
        attrs[0] &= ~(termios.IGNBRK | termios.IGNPAR | termios.PARMRK |
                      termios.ISTRIP | termios.INLCR | termios.IGNCR | termios.ICRNL)
        attrs[1] = 0  # no output processing
        attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0  # raw input
        attrs[6] = [0] * 32  # cc
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.set_baud(baud)
        self.timeout = timeout
        self.set_dtr(True)

    def set_baud(self, baud):
        """Set arbitrary baud via termios2 (BOTHER), falling back to standard."""
        self.baud = baud
        try:
            buf = bytearray(_TERMIOS2_SIZE)
            fcntl.ioctl(self.fd, _TCGETS2, buf)
            struct.pack_into("I", buf, 0, 0)   # c_iflag
            struct.pack_into("I", buf, 4, 0)   # c_oflag
            struct.pack_into("I", buf, 8, _BOTHER | termios.CS8 | termios.CREAD | termios.CLOCAL)
            struct.pack_into("I", buf, 12, 0)  # c_lflag
            buf[16] = 0                        # c_line
            buf[17:36] = b"\x00" * 19          # c_cc
            struct.pack_into("I", buf, 36, baud)  # c_ispeed
            struct.pack_into("I", buf, 40, baud)  # c_ospeed
            fcntl.ioctl(self.fd, _TCSETS2, buf)
            return
        except (OSError, ValueError):
            pass
        std = {9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
               57600: termios.B57600, 115200: termios.B115200, 230400: termios.B230400,
               460800: termios.B460800, 921600: termios.B921600}
        if baud in std:
            attrs = termios.tcgetattr(self.fd)
            termios.cfsetispeed(attrs, std[baud])
            termios.cfsetospeed(attrs, std[baud])
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            return
        raise ValueError(f"unsupported baud {baud}")

    def set_dtr(self, on):
        m = fcntl.ioctl(self.fd, termios.TIOCMGET, struct.pack("I", 0))
        bits = struct.unpack("I", m)[0]
        if on:
            bits |= termios.TIOCM_DTR
        else:
            bits &= ~termios.TIOCM_DTR
        fcntl.ioctl(self.fd, termios.TIOCMSET, struct.pack("I", bits))

    def read(self, n, timeout=None):
        """Read up to n bytes with timeout; returns bytes."""
        t = timeout if timeout is not None else self.timeout
        deadline = time.monotonic() + t
        out = b""
        while len(out) < n:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    chunk = os.read(self.fd, n - len(out))
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            if time.monotonic() > deadline:
                break
        return out

    def read_until(self, marker, timeout=2.0, maxlen=65536):
        deadline = time.monotonic() + timeout
        out = b""
        while marker not in out and len(out) < maxlen:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    chunk = os.read(self.fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            if time.monotonic() > deadline:
                break
        return out

    def drain(self, timeout=0.5):
        """Read and discard pending input."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if not r:
                continue
            try:
                os.read(self.fd, 4096)
            except OSError:
                break

    def write(self, data):
        return os.write(self.fd, data)

    def flush(self):
        termios.tcdrain(self.fd)

    def close(self):
        try:
            self.set_dtr(False)
        except OSError:
            pass
        os.close(self.fd)

# ---- framing, capture --------------------------------------------------------
# ---- LA protocol helpers -----------------------------------------------------
def la_escape(payload):
    out = bytearray()
    for b in payload:
        if b in (0xAA, 0x55, 0xF0):
            out.append(LA_ESC)
            out.append(b ^ 0xF0)
        else:
            out.append(b)
    return bytes(out)


def la_frame(command, payload=b""):
    return LA_FRAME_START + bytes([command]) + la_escape(payload) + LA_FRAME_END


def la_build_capture_request(trigger_type, trigger, inverted_or_count,
                             trigger_value, channels, frequency,
                             pre_samples, post_samples, loop_count=0,
                             measure=0, capture_mode=1):
    ch = channels + [0] * (24 - len(channels))
    return struct.pack(
        "<BBBxH24sBxIIIBBBx",
        trigger_type, trigger, inverted_or_count, trigger_value,
        bytes(ch), len(channels), frequency, pre_samples, post_samples,
        loop_count, measure, capture_mode,
    )


def la_read_response(port, timeout=10):
    """Read a newline-terminated response line from the LA CDC."""
    data = port.read_until(b"\n", timeout=timeout, maxlen=2048)
    return data


def la_capture(port, channels, frequency, pre, post, trigger_type=0,
               trigger=14, inverted=0, trigger_value=0, loop_count=0,
               measure=0, head_timeout=30, data_timeout=90, progress=None,
               drain=1.0):
    """Issue a capture request and read the binary capture payload.

    The firmware may or may not send a leading "CAPTURE_STARTED\\n" line
    (it is dropped when la_bridge_connected() is momentarily false), so the
    parser tolerates both cases.
    """
    port.drain(drain)
    mode = 2 if max(channels) >= 16 else (1 if max(channels) >= 8 else 0)
    req = la_build_capture_request(trigger_type, trigger, inverted,
                                   trigger_value, channels, frequency,
                                   pre, post, loop_count, measure, mode)
    port.write(la_frame(1, req))
    port.flush()
    if progress:
        progress("armed")

    # read the first chunk (response line and/or start of data)
    head = port.read(4096, timeout=head_timeout)
    if b"CAPTURE_STARTED" in head:
        idx = head.index(b"CAPTURE_STARTED") + len(b"CAPTURE_STARTED\n")
        head = head[idx:]
    elif b"CAPTURE_ERROR" in head or b"ERR_" in head:
        return {"ok": False, "response": head.decode(errors="replace")[:80]}
    # head now should start with the 4-byte sample count
    if len(head) < 4:
        head += port.read(4 - len(head), timeout=10)
        if len(head) < 4:
            return {"ok": False, "response": "timeout waiting for data"}
    count = struct.unpack("<I", head[0:4])[0]
    if count == 0 or count > 400000:
        return {"ok": False, "response": f"bad count {count}", "count": count}
    width = 1 if mode == 0 else (2 if mode == 1 else 4)
    total = count * width + 1   # samples + timestamp byte (already have 4 count bytes)
    need = total - (len(head) - 4)
    deadline = time.monotonic() + data_timeout
    streamed = False
    while need > 0 and time.monotonic() < deadline:
        chunk = port.read(min(8192, need), timeout=2.0)
        if not chunk:
            continue
        if progress and not streamed:
            progress("streaming")     # the trigger fired; data is flowing
            streamed = True
        head += chunk
        need = total - (len(head) - 4)
    if need > 0:
        return {"ok": False, "response": "timeout", "count": count,
                "got": len(head) - 4, "want": total}
    samples_raw = head[4:4 + count * width]
    idx = 4 + count * width
    stamp_len = head[idx]
    timestamps = []
    idx += 1
    for _ in range(stamp_len):
        timestamps.append(struct.unpack("<I", head[idx:idx + 4])[0])
        idx += 4
    samples = []
    for i in range(count):
        if width == 1:
            samples.append(samples_raw[i])
        elif width == 2:
            samples.append(struct.unpack("<H", samples_raw[2 * i:2 * i + 2])[0])
        else:
            samples.append(struct.unpack("<I", samples_raw[4 * i:4 * i + 4])[0])
    return {"ok": True, "count": count, "mode": mode, "width": width,
            "stamp_len": stamp_len, "timestamps": timestamps, "samples": samples}

# ---- streaming ---------------------------------------------------------------

def stream_start(port, chans, rate, half, triggered=False, trigger=0, falling=False):
    """Start CMD=7 (or 8, triggered). Returns (started, leftover, bps, mode)."""
    mode, bps = mode_bps(chans)
    req = la_build_capture_request(0, trigger, 1 if falling else 0, 0,
                                   chans, rate, 0, half, 0, 0, mode)
    # a crashed previous run can leave the LA streaming (it ignores commands
    # until it sees the stop frame) — clear that first
    port.write(la_frame(0x42))
    port.flush()
    port.drain(0.5)
    port.write(la_frame(0x44 if triggered else 0x43, req))
    port.flush()
    buf = b""
    t0 = time.time()
    while (b"STREAM_CONT_STARTED" not in buf and b"STREAM_ERROR" not in buf
           and time.time() - t0 < 5):
        buf += port.read(256, timeout=0.5)
    started = b"STREAM_CONT_STARTED" in buf
    tail = bytearray(buf[buf.find(b"STREAM_CONT_STARTED\n") + 20:] if started else b"")
    return started, tail, bps, mode


def stream_stop(port):
    port.write(la_frame(0x42))
    port.flush()
    buf = b""
    t0 = time.time()
    while b"STREAM_STOPPED" not in buf and time.time() - t0 < 3:
        buf += port.read(8192, timeout=0.3)
    return b"STREAM_STOPPED" in buf


def read_frame(port, leftover, timeout=2.0):
    """Read one [u32 count][payload] half-frame; None on failure/timeout."""
    def rn(n, t):
        deadline = time.time() + t
        while len(leftover) < n and time.time() < deadline:
            leftover.extend(port.read(max(n - len(leftover), 4096), timeout=0.2))
        if len(leftover) < n:
            return None
        out = bytes(leftover[:n])
        del leftover[:n]
        return out

    cnt = rn(4, timeout)
    if cnt is None:
        return None
    n = struct.unpack("<I", cnt)[0]
    if n > (1 << 20):        # sane frame size only
        return None
    return rn(n, timeout) if n else b""


def decode_samples(data, bps):
    """Frame payload -> array('I') of sample ints (one per sample tick)."""
    a = array({1: "B", 2: "H", 4: "I"}[bps])
    a.frombytes(data)
    if sys.byteorder != "little":
        a.byteswap()
    return a if bps == 4 else array("I", a)

# ---- sample analysis ---------------------------------------------------------

def extract_channel(samples, width, ch_index):
    """Extract per-sample bit for requested channel ch_index (0-based)."""
    out = []
    for s in samples:
        if width == 1:
            out.append((s >> ch_index) & 1)
        elif width == 2:
            out.append((s >> ch_index) & 1)
        else:
            out.append((s >> ch_index) & 1)
    return out


def analyze_channels(samples, width, sample_rate):
    """Return a dict of per-channel timing metrics.

    samples: list of raw sample words; channel i is bit i.
    """
    nch = width * 8
    dt = 1.0 / sample_rate
    ch = {}
    for c in range(2):  # we only care about the first two requested channels
        bits = [((s >> c) & 1) for s in samples]
        edges = []
        for i in range(1, len(bits)):
            if bits[i] != bits[i - 1]:
                edges.append((i, bits[i]))  # (index, new value)
        rising = [i for (i, v) in edges if v == 1]
        falling = [i for (i, v) in edges if v == 0]
        # periods between consecutive rising edges (median is robust to a
        # single glitched/missed edge in a long capture)
        periods = [rising[k + 1] - rising[k] for k in range(len(rising) - 1)]
        if periods:
            sp = sorted(periods)
            med = sp[len(sp) // 2]
            period_avg = med * dt
            freq = 1.0 / period_avg
        else:
            period_avg = 0.0
            freq = 0.0
        # per-period duty: measure high fraction between consecutive rising edges
        # (avoids the all-low pre-trigger region skewing a whole-window average)
        period_duties = []
        for k in range(len(rising) - 1):
            s = rising[k]
            e = rising[k + 1]
            if e > s:
                period_duties.append(sum(bits[s:e]) / (e - s) * 100.0)
        duty = sum(period_duties) / len(period_duties) if period_duties else \
            ((sum(bits) / len(bits) * 100.0) if bits else 0.0)
        # high/low run lengths
        runs = []
        cur = bits[0] if bits else 0
        run = 0
        for b in bits:
            if b == cur:
                run += 1
            else:
                runs.append((cur, run))
                cur = b
                run = 1
        runs.append((cur, run))
        high_runs = [r for (v, r) in runs if v == 1]
        low_runs = [r for (v, r) in runs if v == 0]
        ch[c] = {
            "bits": bits,
            "rising": rising,
            "falling": falling,
            "edge_count": len(edges),
            "rising_count": len(rising),
            "falling_count": len(falling),
            "period_avg_s": period_avg,
            "frequency_hz": freq,
            "duty_percent": duty,
            "high_avg_ticks": (sum(high_runs) / len(high_runs)) if high_runs else 0,
            "low_avg_ticks": (sum(low_runs) / len(low_runs)) if low_runs else 0,
            "high_avg_s": ((sum(high_runs) / len(high_runs)) * dt) if high_runs else 0.0,
            "low_avg_s": ((sum(low_runs) / len(low_runs)) * dt) if low_runs else 0.0,
        }
    return ch


def channel_metrics(samples, width, sample_rate, nch=None):
    """Per-bit timing metrics for every captured channel.

    bit k of each sample word corresponds to the k-th requested channel.
    """
    if nch is None:
        nch = width * 8
    dt = 1.0 / sample_rate
    out = {}
    for c in range(nch):
        bits = [((s >> c) & 1) for s in samples]
        edges = [(i, bits[i]) for i in range(1, len(bits)) if bits[i] != bits[i - 1]]
        rising = [i for (i, v) in edges if v == 1]
        falling = [i for (i, v) in edges if v == 0]
        periods = [rising[k + 1] - rising[k] for k in range(len(rising) - 1)]
        if periods:
            sp = sorted(periods)
            med = sp[len(sp) // 2]
            period_avg = med * dt
            freq = 1.0 / period_avg
        else:
            period_avg = 0.0
            freq = 0.0
        period_duties = []
        for k in range(len(rising) - 1):
            s, e = rising[k], rising[k + 1]
            if e > s:
                period_duties.append(sum(bits[s:e]) / (e - s) * 100.0)
        duty = sum(period_duties) / len(period_duties) if period_duties else \
            ((sum(bits) / len(bits) * 100.0) if bits else 0.0)
        out[c] = {
            "edge_count": len(edges),
            "rising_count": len(rising),
            "falling_count": len(falling),
            "period_avg_s": period_avg,
            "frequency_hz": freq,
            "duty_percent": duty,
            "high_ratio": (sum(bits) / len(bits)) if bits else 0.0,
        }
    return out


def quick_metrics(arr, rate, chans, bits, np):
    """Fast per-channel frequency (median period) + duty from a sample array."""
    out = {}
    for ch, bit in zip(chans, bits):
        b = (arr >> bit) & 1
        rising = np.nonzero(np.diff(b) == 1)[0]
        if len(rising) < 2:
            continue
        period = float(np.median(np.diff(rising)))
        if period > 0:
            out[ch] = (rate / period, float(b.mean()) * 100.0)
    return out

# ---- channel/pin and rate helpers --------------------------------------------

def chan_pin(c):
    """LA channel -> PicoGremlin GPIO pin (pin map {2..22, 26..28})."""
    return 2 + c if c <= 20 else 26 + (c - 21)


CHAN_OF_PIN = {chan_pin(c): c for c in range(24)}


def mode_bps(chans):
    mode = 2 if max(chans) >= 16 else (1 if max(chans) >= 8 else 0)
    return mode, {0: 1, 1: 2, 2: 4}[mode]


MAX_STREAM_BYTES = 700_000     # measured USB-CDC ceiling (~700 kB/s)


def clamp_rate(rate, bps):
    """Lower the rate so the stream fits the measured link budget.

    Returns (rate, note) — note is None when the requested rate fits."""
    limit = MAX_STREAM_BYTES // bps
    if rate <= limit:
        return rate, None
    new = max(1000, (limit // 5000) * 5000)
    return new, (f"rate {rate / 1e3:g} kHz needs {rate * bps / 1e3:.0f} kB/s — "
                 f"link tops out ~{MAX_STREAM_BYTES / 1e3:.0f} kB/s; using "
                 f"{new / 1e3:g} kHz")
