#!/usr/bin/env python3
"""la — quick logic-analyzer client for PicoGremlin probes.

    la info                 probe identity (stock reply + PicoGremlin extras)
    la cap [options]        one-shot capture -> summary, CSV/VCD export, plot
    la stream [options]     continuous stream -> CSV/VCD + measured rate
    la gui                  Qt oscilloscope GUI (needs PyQt6/PySide6 + pyqtgraph)

One-shot capture options:
    -c 14,15 / 0-7   channels (default 0-23)
    -r 1M            sample rate, k/M suffix (default 1M)
    -n 10000         samples after the trigger (-p for before, default 0)
    -t 14            trigger channel (default 0)
    --falling        trigger on falling edge (default rising)
    -o FILE          write FILE.csv and FILE.vcd
    --plot [PNG]     matplotlib window, or save to PNG
    --wait 2.5       seconds to wait for the trigger edge

Stream (continuous) options — no trigger, keeps sampling:
    -c 0-7 -r 200k   channels / rate (default rate 200k)
    --seconds 1.0    record for this long
    -o FILE          write FILE.csv and FILE.vcd (gapless timeline)

Notes:
  * The probe is picked via PICOGREMLIN_SERIAL (else the only one attached).
    `la gui` also has a board selector (by serial, remembered; boards whose
    debug interface is held by OpenOCD are tagged and skipped by the auto-pick).
  * info/cap/stream need only the standard library; --plot needs matplotlib.
    `la gui` needs Qt + pyqtgraph (install with: pipx install
    'picogremlin-la[gui]').
  * Channel vs pin: LA channel k is GPIO (2+k) — GP16/GP17 are channels 14/15;
    channels 21-23 are GP26-28. Rows and summaries show both ("ch14/GP16").
"""
import argparse
import csv
import os
import subprocess
import sys
import time
from array import array

from . import __version__
from .protocol import (SerialPort, la_capture, la_frame, la_build_capture_request,
                       channel_metrics, quick_metrics, chan_pin, CHAN_OF_PIN,
                       mode_bps, MAX_STREAM_BYTES, clamp_rate, decode_samples,
                       stream_start, stream_stop, read_frame, LA_ID_CMD)
from .transport import find_ports, open_la


def parse_channels(spec):
    """Channel list from '14,15', '0-7' or GPIO names: 'GP16,GP17', 'gp16-17'."""
    out = []
    for tok in spec.replace(" ", "").split(","):
        if not tok:
            continue
        gp = tok.lower().startswith("gp")
        if gp:
            tok = tok[2:]
        if "-" in tok:
            a, b = tok.split("-", 1)
            values = range(int(a), int(b) + 1)
        else:
            values = [int(tok)]
        for v in values:
            if gp:
                if v not in CHAN_OF_PIN:
                    raise SystemExit(f"GP{v} is not an LA channel (GP2-22, GP26-28)")
                out.append(CHAN_OF_PIN[v])
            else:
                if not 0 <= v <= 23:
                    raise SystemExit(f"channel {v} out of range (0-23)")
                out.append(v)
    out = sorted(set(out))
    return out or list(range(24))


def parse_trigger(spec):
    """Trigger channel from '14' or 'GP16'."""
    try:
        s = spec.strip().lower()
        if s.startswith("gp"):
            return CHAN_OF_PIN[int(s[2:])]
        v = int(s)
    except (ValueError, KeyError):
        raise argparse.ArgumentTypeError(f"channel or GP name expected, got {spec!r}")
    if not 0 <= v <= 23:
        raise argparse.ArgumentTypeError(f"channel {v} out of range (0-23)")
    return v


def parse_rate(spec):
    s = spec.strip().lower()
    mult = 1
    if s.endswith("k"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    return int(float(s) * mult)


def print_channel_summary(samples, chans, rate, bits):
    """samples: sequence of ints; bits[k] = bit index holding channel chans[k].

    One-shot captures pack the requested channels into consecutive low bits
    (bits = 0..n-1); CMD=7/8 streams send the raw PIO word, where bit k is
    channel k (bits = the channel numbers themselves).
    """
    m = channel_metrics(samples, 1, rate, nch=max(bits) + 1)
    idle = []
    for ch, bit in zip(chans, bits):
        d = m[bit]
        if not d["edge_count"]:
            idle.append(ch)
            continue
        print(f"  ch{ch}/GP{chan_pin(ch):<2} edges {d['edge_count']:<6} "
              f"freq {d['frequency_hz']:>12.4g} Hz   duty {d['duty_percent']:5.1f} %")
    if idle:
        print(f"  idle: {', '.join('ch%d/GP%d' % (c, chan_pin(c)) for c in idle)}")
    if len(idle) == len(chans):
        print("  (nothing toggling — is the target running? an OpenOCD/picolab "
              "session may hold it halted; `openocd -c 'init; reset run'` resumes it)")


def write_csv(path, samples, chans, bits, rate):
    dt = 1.0 / rate
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_s"] + [f"ch{c}" for c in chans])
        for i, s in enumerate(samples):
            w.writerow([f"{i * dt:.10g}"] + [(s >> bit) & 1 for bit in bits])


def write_vcd(path, samples, chans, bits, rate):
    """Verilog VCD — opens in PulseView/GTKWave/sigrok-cli."""
    scale = 1e9 / rate
    ids = [chr(33 + i) for i in range(len(chans))]     # ! " # ...
    with open(path, "w") as f:
        f.write(f"$date\n    {time.strftime('%Y-%m-%d %H:%M')}\n$end\n")
        f.write("$version\n    PicoGremlin ./la\n$end\n$timescale 1 ns $end\n")
        f.write("$scope module logic $end\n")
        for c, sid in zip(chans, ids):
            f.write(f"$var wire 1 {sid} ch{c} $end\n")
        f.write("$upscope $end\n$enddefinitions $end\n")
        f.write("$dumpvars\n")
        for k, sid in enumerate(ids):
            f.write(f"{(samples[0] >> bits[k]) & 1}{sid}\n")
        f.write("$end\n")
        for i in range(1, len(samples)):
            s = samples[i]
            changes = []
            for k, sid in enumerate(ids):
                v = (s >> bits[k]) & 1
                if v != ((samples[i - 1] >> bits[k]) & 1):
                    changes.append((sid, v))
            if changes:
                f.write(f"#{int(round(i * scale))}\n")
                for sid, v in changes:
                    f.write(f"{v}{sid}\n")


def draw_waves(ax, samples, chans, x, np, bits, metrics=None):
    """Stacked digital rows, first channel on top, with room above the top row.

    metrics: optional {channel: (freq_hz, duty_percent)} shown in the row label.
    """
    ax.clear()
    arr = np.asarray(samples, dtype=np.uint32)
    n = len(chans)
    step = 1.4
    labels = []
    for k, ch in enumerate(chans):
        off = (n - 1 - k) * step          # channel 0 at the top
        y = ((arr >> bits[k]) & 1).astype(float) + off
        ax.step(x, y, where="post", linewidth=1.1)
        lab = f"ch{ch}/GP{chan_pin(ch)}"
        if metrics and ch in metrics:
            f, duty = metrics[ch]
            lab += f"   {f:,.4g} Hz  {duty:.0f}%"
        labels.append(lab)
    ax.set_yticks([(n - 1 - k) * step + 0.5 for k in range(n)])
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_ylim(-0.35, (n - 1) * step + 1.65)   # margins above/below the outer rows
    ax.grid(axis="x", alpha=0.3)


def cmd_info(args):
    port = open_la()
    port.drain(0.5)
    port.write(LA_ID_CMD)
    port.flush()
    resp = port.read_until(b"CHANNELS:24", timeout=5, maxlen=4096)
    if b"CHANNELS:24" not in resp:
        raise SystemExit(f"no identity answer: {resp!r}")
    print(resp.decode(errors="replace").strip())
    # PicoGremlin extras (CMD=0x40): firmware version, stream support, bridge state
    port.drain(0.2)
    port.write(la_frame(0x40))
    port.flush()
    extra = port.read(256, timeout=0.6)
    for line in extra.decode(errors="replace").splitlines():
        if line.startswith(("PICOGREMLIN:", "STREAM:", "BRIDGE_TX:")):
            print(line)
    port.close()


def cmd_cap(args):
    chans = parse_channels(args.channels)
    rate = parse_rate(args.rate)
    port = open_la()
    t0 = time.monotonic()
    cap = la_capture(port, chans, rate, args.pre, args.samples, trigger_type=0,
                     trigger=args.trigger, inverted=1 if args.falling else 0,
                     head_timeout=args.wait, drain=0.3)
    if not cap["ok"]:
        port.write(b"\xff")            # cancel a capture still waiting for trigger
        port.flush()
        port.drain(0.5)
        port.close()
        raise SystemExit(f"capture failed: {cap.get('response')} "
                         f"(no {('falling' if args.falling else 'rising')} edge on "
                         f"ch{args.trigger} within {args.wait}s?)")
    port.close()
    bits = list(range(len(chans)))          # one-shot: packed from bit 0
    print(f"captured {cap['count']} samples @ {rate / 1e6:g} MHz "
          f"({cap['count'] / rate * 1e3:.2f} ms)")
    print_channel_summary(cap["samples"], chans, rate, bits)
    print(f"  ({time.monotonic() - t0:.2f} s round trip)")
    if args.out:
        write_csv(args.out + ".csv", cap["samples"], chans, bits, rate)
        write_vcd(args.out + ".vcd", cap["samples"], chans, bits, rate)
        print(f"  wrote {args.out}.csv  {args.out}.vcd")
    if args.plot is not None:
        import matplotlib
        if args.plot:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        fig, ax = plt.subplots(figsize=(12, max(2.5, 0.35 * len(chans))))
        x = np.arange(cap["count"]) / rate
        draw_waves(ax, cap["samples"], chans, x, np, bits)
        ax.set_xlabel("time (s)")
        fig.tight_layout()
        if args.plot:
            fig.savefig(args.plot, dpi=150)
            print(f"  plot saved to {args.plot}")
        else:
            plt.show()


def cmd_stream(args):
    chans = parse_channels(args.channels)
    rate = parse_rate(args.rate)
    _mode, bps = mode_bps(chans)
    rate, note = clamp_rate(rate, bps)
    if note:
        print(f"note: {note}")
    half = args.half or max(64, min(8192, rate // 20))
    port = open_la()
    print(f"streaming ch{chans[0]}-ch{chans[-1]} @ {rate / 1e3:g} kHz "
          f"(half={half} samples) for {args.seconds}s ...")
    started, leftover, bps, _mode = stream_start(port, chans, rate, half)
    if not started:
        port.close()
        raise SystemExit("stream did not start (STREAM_ERROR?)")
    samples = array("I")
    t0 = time.monotonic()
    frames = 0
    while time.monotonic() - t0 < args.seconds:
        data = read_frame(port, leftover)
        if data is None:
            break
        samples.extend(decode_samples(data, bps))
        frames += 1
    elapsed = time.monotonic() - t0
    stream_stop(port)
    port.close()
    n = len(samples)
    measured = n / elapsed if elapsed else 0
    print(f"received {n} samples in {elapsed:.2f}s -> {measured / 1e3:.1f} kHz "
          f"({measured / rate * 100:.0f}% of requested, {frames} frames)")
    if n:
        bits = chans                        # stream: raw PIO word, bit k = channel k
        print_channel_summary(samples, chans, rate, bits)
        if args.out:
            write_csv(args.out + ".csv", samples, chans, bits, rate)
            write_vcd(args.out + ".vcd", samples, chans, bits, rate)
            print(f"  wrote {args.out}.csv  {args.out}.vcd")


def cmd_gui(args):
    """Launch the Qt GUI (needs the [gui] extra: PyQt6/PySide6 + pyqtgraph)."""
    fwd = []
    if getattr(args, "smoke", False):
        fwd.append("--smoke")
    if getattr(args, "grab", None):
        fwd += ["--grab", args.grab]
    try:
        from . import gui                      # in-process when Qt is available
        sys.exit(gui.main(fwd))
    except ImportError:
        pass
    # Fall back to a Python that has Qt (LA_PYTHON, the shared venv, or this one)
    venv_py = os.path.expanduser("~/.local/share/picogremlin/venv/bin/python")
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    for py in (os.environ.get("LA_PYTHON"), venv_py, sys.executable):
        if not py or not os.path.exists(py):
            continue
        for probe in ("import pyqtgraph, PyQt6", "import pyqtgraph, PySide6"):
            if subprocess.run([py, "-c", probe], capture_output=True).returncode == 0:
                sys.exit(subprocess.run([py, "-m", "picogremlin_la.gui", *fwd],
                                        env=env).returncode)
    print("The GUI needs Qt (PyQt6 or PySide6) + pyqtgraph:\n"
          "  pipx install 'picogremlin-la[gui]'          (recommended)\n"
          "  python3 -m venv ~/.local/share/picogremlin/venv && \\\n"
          "    ~/.local/share/picogremlin/venv/bin/pip install -e '.[gui]'\n"
          "  or distro packages:\n"
          "    sudo pacman -S python-pyqt6 python-pyqtgraph    (Arch)\n"
          "    sudo apt install python3-pyqt6 python3-pyqtgraph  (Debian/Ubuntu)")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(prog="la", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__} (picogremlin-la)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="probe identity")

    p = sub.add_parser("cap", help="one-shot capture")
    p.add_argument("-c", "--channels", default="0-23",
                   help="channels: 0-7, 14,15 or GP16,GP17 (default 0-23)")
    p.add_argument("-r", "--rate", default="1M")
    p.add_argument("-n", "--samples", type=int, default=10000)
    p.add_argument("-p", "--pre", type=int, default=0)
    p.add_argument("-t", "--trigger", type=parse_trigger, default=0,
                   help="trigger channel or GP name (e.g. 14 / GP16)")
    p.add_argument("--falling", action="store_true")
    p.add_argument("-o", "--out", help="write OUT.csv and OUT.vcd")
    p.add_argument("--plot", nargs="?", const="", default=None, metavar="PNG")
    p.add_argument("--wait", type=float, default=2.5)

    p = sub.add_parser("stream", help="continuous stream recording")
    p.add_argument("-c", "--channels", default="0-23",
                   help="channels: 0-7, 14,15 or GP16,GP17 (default 0-23)")
    p.add_argument("-r", "--rate", default="200k")
    p.add_argument("--seconds", type=float, default=1.0)
    p.add_argument("--half", type=int, default=0,
                   help="samples per half-frame (default rate/20)")
    p.add_argument("-o", "--out", help="write OUT.csv and OUT.vcd")

    p = sub.add_parser("gui", help="Qt oscilloscope GUI (needs PyQt6+pyqtgraph)")
    p.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--grab", metavar="PNG", help=argparse.SUPPRESS)

    args = ap.parse_args()
    if args.cmd == "info":
        cmd_info(args)
    elif args.cmd == "cap":
        cmd_cap(args)
    elif args.cmd == "stream":
        cmd_stream(args)
    else:
        cmd_gui(args)


if __name__ == "__main__":
    main()
