#!/usr/bin/env python3
"""la — quick logic-analyzer client for PicoGremlin probes.

    la info                 probe identity (stock reply + PicoGremlin extras)
    la cap [options]        one-shot capture -> summary, CSV/VCD export, plot
    la stream [options]     continuous stream -> CSV/VCD + measured rate
    la acap [options]       one-shot analog capture (GP26/27/28 + on-chip temp)
    la astream [options]    continuous analog stream recording
    la acheck [options]     PASS/FAIL one analog input against an expectation
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
                       stream_start, stream_stop, read_frame, LA_ID_CMD,
                       la_analog_capture, analog_stream_start, analog_channel_values,
                       la_sync, drain_quiet,
                       adc_volts, adc_temp_c, parse_analog_channels, ANALOG_INPUTS)
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
        if line.startswith(("PICOGREMLIN:", "STREAM:", "BRIDGE_TX:", "ANALOG:")):
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


# ------------------------------------------------------------------ analog

def parse_volts(spec):
    """Voltage spec -> 12-bit ADC count: '1.65', '1.65V', '1650mV'."""
    t = str(spec).strip().lower().replace(" ", "")
    mult = 1.0
    if t.endswith("mv"):
        mult, t = 1e-3, t[:-2]
    elif t.endswith("v"):
        mult, t = 1.0, t[:-1]
    elif t.endswith("k"):
        mult, t = 1e3, t[:-1]
    v = float(t) * mult
    return max(0, min(4095, int(round(v / 3.3 * 4096.0))))


def parse_unit(spec):
    """Value spec -> float in base units: '1.65V', '1kHz', '250ms', '5%'."""
    t = str(spec).strip().lower().replace(" ", "")
    if t.endswith("%"):
        return float(t[:-1])
    for suf, mult in (("°c", 1.0), ("mv", 1e-3), ("v", 1.0), ("khz", 1e3), ("mhz", 1e6),
                      ("hz", 1.0), ("ms", 1e-3), ("us", 1e-6), ("s", 1.0), ("k", 1e3),
                      ("c", 1.0)):
        if t.endswith(suf):
            return float(t[:-len(suf)]) * mult
    return float(t)


def parse_expect(spec):
    """'1.65V±5%' -> (1.65, 0.0825); '1kHz±2%' -> (1000.0, 20.0); '3.2V' -> (3.2, None)."""
    s = str(spec).replace("+/-", "±").replace("+-", "±")
    if "±" in s:
        value_part, tol_part = s.split("±", 1)
        value = parse_unit(value_part)
        if tol_part.strip().endswith("%"):
            return value, abs(value) * parse_unit(tol_part) / 100.0
        return value, abs(parse_unit(tol_part))
    return parse_unit(s), None


def measure_analog(vals, rate, level=None):
    """Per-channel analog metrics from one channel's samples."""
    n = len(vals)
    if not n:
        return {}
    lo, hi = min(vals), max(vals)
    mean = sum(vals) / n
    pp = hi - lo
    rms = (sum(v * v for v in vals) / n) ** 0.5
    thr = mean if level is None else level
    hyst = max(4.0, pp * 0.1)            # 10% of the swing, min 4 counts
    rising, falling, period_duties = [], [], []
    state = None
    last_rise = None
    high_since = None
    for i, v in enumerate(vals):
        if state is None:
            state = 1 if v > thr else 0
            if state:
                rising.append(i)
                last_rise, high_since = i, i
            continue
        if state == 0 and v > thr + hyst:
            state = 1
            rising.append(i)
            if last_rise is not None and i > last_rise:
                period_duties.append((i - last_rise))
            last_rise = i
            high_since = i
        elif state == 1 and v < thr - hyst:
            state = 0
            falling.append(i)
    periods = [rising[k + 1] - rising[k] for k in range(len(rising) - 1)]
    if periods:
        sp = sorted(periods)
        period = sp[len(sp) // 2]
        freq = rate / period
    else:
        freq = 0.0
    duty = 0.0
    if rising and falling:
        high = []
        for k, r in enumerate(rising):
            f = next((x for x in falling if x > r), None)
            if f is not None:
                high.append(f - r)
        if high:
            duty = sum(high) / len(high) / (rate / freq) * 100.0 if freq else 0.0
    return {"min": lo, "max": hi, "mean": mean, "pp": pp, "rms": rms,
            "freq": freq, "duty": duty, "n": n}


def wave_present(m, ch):
    """A real waveform (not just ADC noise) needs a swing: ~0.16 V, or 20 counts
    on the tiny temperature slope."""
    return m["pp"] >= (20 if ch == 3 else 200)


def analog_summary(vals, rate, channels, metrics=None):
    nch = len(channels)
    per = analog_channel_values(vals, nch)
    out = {}
    for k, ch in enumerate(channels):
        m = measure_analog(per[k], rate)
        out[ch] = m
        if ch == 3:
            print(f"  {ANALOG_INPUTS[ch]:<9} {adc_temp_c(m['mean']):6.1f} °C"
                  f"  (raw min/max {m['min']:.0f}/{m['max']:.0f})")
        else:
            f = (f"{m['freq']:>10.4g} Hz"
                 if (m["freq"] and wave_present(m, ch)) else "         —")
            line = (f"  {ANALOG_INPUTS[ch]:<9} mean {adc_volts(m['mean']):6.3f} V"
                    f"  min {adc_volts(m['min']):6.3f}  max {adc_volts(m['max']):6.3f}"
                    f"  pp {adc_volts(m['pp']):6.3f}  rms {adc_volts(m['rms']):6.3f}"
                    f"  {f}")
            if wave_present(m, ch):
                line += f"  duty {m['duty']:5.1f} %"
            print(line)
    return out


def write_analog_csv(path, vals, channels, rate):
    nch = len(channels)
    per = analog_channel_values(vals, nch)
    dt = 1.0 / rate
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_s"] + [ANALOG_INPUTS[c].replace("/", "_") for c in channels])
        for i in range(len(per[0])):
            w.writerow([f"{i * dt:.10g}"] +
                       [f"{adc_volts(per[k][i]):.6f}" for k in range(nch)])


def write_analog_vcd(path, vals, channels, rate):
    nch = len(channels)
    per = analog_channel_values(vals, nch)
    scale = 1e9 / rate
    ids = [chr(33 + i) for i in range(nch)]
    with open(path, "w") as f:
        f.write(f"$date\n    {time.strftime('%Y-%m-%d %H:%M')}\n$end\n")
        f.write("$version\n    PicoGremlin la acap\n$end\n$timescale 1 ns $end\n")
        f.write("$scope module analog $end\n")
        for c, sid in zip(channels, ids):
            f.write(f"$var real 64 {sid} {ANALOG_INPUTS[c].replace('/', '_')} $end\n")
        f.write("$upscope $end\n$enddefinitions $end\n$dumpvars\n")
        for k, sid in enumerate(ids):
            f.write(f"r{adc_volts(per[k][0]):.6f} {sid}\n")
        f.write("$end\n")
        for i in range(1, len(per[0])):
            f.write(f"#{int(round(i * scale))}\n")
            for k, sid in enumerate(ids):
                if per[k][i] != per[k][i - 1]:
                    f.write(f"r{adc_volts(per[k][i]):.6f} {sid}\n")


def cmd_acap(args):
    chans = parse_analog_channels(args.analog)
    rate = parse_rate(args.rate)
    level = parse_volts(args.level) if args.level else 0
    trig = None
    if args.trigger is not None:
        trig = parse_analog_channels(args.trigger)[0]
    post = max(1, args.samples)
    port = open_la()
    t0 = time.monotonic()
    cap = la_analog_capture(port, chans, rate, max(0, args.pre), post,
                            eight_bit=args.eight_bit, trigger=trig, level=level,
                            inverted=args.falling, head_timeout=args.wait)
    if not cap["ok"]:
        port.write(b"\xff")
        port.flush()
        port.drain(0.5)
        port.close()
        raise SystemExit(f"analog capture failed: {cap.get('response')}")
    port.close()
    n = len(cap["values"]) // len(chans)
    print(f"captured {n} samples/channel @ {cap['rate'] / 1e3:g} kS/s "
          f"({n / cap['rate'] * 1e3:.2f} ms, {len(chans)} channel(s), "
          f"{'8-bit' if args.eight_bit else '12-bit'})")
    analog_summary(cap["values"], cap["rate"], chans)
    print(f"  ({time.monotonic() - t0:.2f} s round trip)")
    if args.out:
        write_analog_csv(args.out + ".csv", cap["values"], chans, cap["rate"])
        write_analog_vcd(args.out + ".vcd", cap["values"], chans, cap["rate"])
        print(f"  wrote {args.out}.csv  {args.out}.vcd")
    if args.plot is not None:
        import matplotlib
        if args.plot:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        per = analog_channel_values(cap["values"], len(chans))
        t = [i / cap["rate"] * 1e3 for i in range(len(per[0]))]
        fig, ax = plt.subplots(figsize=(12, max(2.5, 1.2 * len(chans))))
        for k, ch in enumerate(chans):
            label = ANALOG_INPUTS[ch]
            y = [adc_temp_c(v) if ch == 3 else adc_volts(v) for v in per[k]]
            ax.plot(t, y, linewidth=1.1, label=label)
        ax.set_xlabel("time (ms)")
        ax.set_ylabel("°C" if chans == [3] else "V")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
        fig.tight_layout()
        if args.plot:
            fig.savefig(args.plot, dpi=150)
            print(f"  plot saved to {args.plot}")
        else:
            plt.show()


def cmd_astream(args):
    chans = parse_analog_channels(args.analog)
    rate = parse_rate(args.rate)
    half = args.half or max(64, min(8192, rate // 20))
    bps = 1 if args.eight_bit else 2
    budget = MAX_STREAM_BYTES // (bps * len(chans))
    if rate > budget:
        new = max(1000, (budget // 1000) * 1000)
        print(f"note: {rate / 1e3:g} kS/s × {len(chans)} ch × {bps} B exceeds the "
              f"~{MAX_STREAM_BYTES / 1e3:.0f} kB/s link; using {new / 1e3:g} kS/s")
        rate = new
    port = open_la()
    print(f"streaming {len(chans)} analog channel(s) @ {rate / 1e3:g} kS/s "
          f"(half={half}) for {args.seconds}s ...")
    started, leftover, rate_rep = analog_stream_start(port, chans, rate, half,
                                                      eight_bit=args.eight_bit)
    if not started:
        port.close()
        raise SystemExit("analog stream did not start (ANALOG_ERROR?)")
    samples = []
    t0 = time.monotonic()
    frames = 0
    while time.monotonic() - t0 < args.seconds:
        data = read_frame(port, leftover)
        if data is None:
            break
        frames += 1
        if args.eight_bit:
            samples.extend(data)
        else:
            samples.extend(np_free_unpack_16(data))
    elapsed = time.monotonic() - t0
    stream_stop(port)
    port.close()
    values = len(samples)
    measured = values / len(chans) / elapsed if elapsed else 0
    print(f"received {values} values ({values // len(chans)} per channel) in "
          f"{elapsed:.2f}s -> {measured / 1e3:.1f} kS/s "
          f"({measured / rate_rep * 100:.0f}% of requested, {frames} frames)")
    if values:
        analog_summary(samples, rate_rep, chans)
        if args.out:
            write_analog_csv(args.out + ".csv", samples, chans, rate_rep)
            write_analog_vcd(args.out + ".vcd", samples, chans, rate_rep)
            print(f"  wrote {args.out}.csv  {args.out}.vcd")


def np_free_unpack_16(data):
    import struct as _struct
    return list(_struct.unpack("<%dH" % (len(data) // 2), data))


def cmd_acheck(args):
    """Measure one analog input and PASS/FAIL it against expectations."""
    chans = [parse_analog_channels(args.analog)[0]]
    rate = parse_rate(args.rate)
    level = parse_volts(args.level) if args.level else None
    trig = None
    if args.trigger is not None:
        trig = parse_analog_channels(args.trigger)[0]
    port = open_la()
    cap = la_analog_capture(port, chans, rate, max(0, args.pre),
                            max(1, args.samples), eight_bit=args.eight_bit,
                            trigger=trig, level=level if level else 0,
                            inverted=args.falling, head_timeout=args.wait)
    port.close()
    if not cap["ok"]:
        raise SystemExit(f"analog capture failed: {cap.get('response')}")

    m = measure_analog(cap["values"], cap["rate"], level)
    ch = chans[0]
    checks = []
    if args.mean:
        exp, tol = parse_expect(args.mean)
        got = adc_volts(m["mean"])
        checks.append(("mean", got, exp, tol, "V"))
    if args.pp:
        exp, tol = parse_expect(args.pp)
        got = adc_volts(m["pp"])
        checks.append(("peak-to-peak", got, exp, tol, "V"))
    if args.rms:
        exp, tol = parse_expect(args.rms)
        got = adc_volts(m["rms"])
        checks.append(("rms", got, exp, tol, "V"))
    if args.min:
        exp, tol = parse_expect(args.min)
        checks.append(("min", adc_volts(m["min"]), exp, tol, "V"))
    if args.max:
        exp, tol = parse_expect(args.max)
        checks.append(("max", adc_volts(m["max"]), exp, tol, "V"))
    if args.freq:
        exp, tol = parse_expect(args.freq)
        checks.append(("frequency", m["freq"], exp, tol, "Hz"))
    if args.duty:
        exp, tol = parse_expect(args.duty)
        checks.append(("duty", m["duty"], exp, tol, "%"))
    if args.temp:
        exp, tol = parse_expect(args.temp)
        checks.append(("temperature", adc_temp_c(m["mean"]), exp, tol, "°C"))

    if not checks:
        raise SystemExit("no expectation given (try --mean '1.65V±5%' or --freq '1kHz±2%')")

    print(f"input {ANALOG_INPUTS[ch]}: {m['n']} samples @ {cap['rate'] / 1e3:g} kS/s")
    failures = 0
    for name, got, exp, tol, unit in checks:
        if tol is None:
            ok = abs(got - exp) <= max(abs(exp) * 0.01, 1e-9)
            tol_txt = "±1%"
        else:
            ok = abs(got - exp) <= tol
            tol_txt = f"±{tol:.4g} {unit}"
        failures += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<12} {got:10.4g} {unit}"
              f"   expected {exp:10.4g} {unit} {tol_txt}")
    if failures:
        raise SystemExit(1)
    print("  all analog checks passed")


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

    p = sub.add_parser("acap", help="one-shot analog capture (ADC)")
    p.add_argument("-a", "--analog", default="0",
                   help="analog inputs: 0,1 / A0,A2 / GP26 / temp (default 0)")
    p.add_argument("-r", "--rate", default="100k", help="per-channel sample rate")
    p.add_argument("-n", "--samples", type=int, default=2000,
                   help="samples per channel after the trigger")
    p.add_argument("-p", "--pre", type=int, default=0,
                   help="samples per channel before the trigger")
    p.add_argument("-t", "--trigger", default=None,
                   help="analog input to trigger on (default: capture now)")
    p.add_argument("--level", default=None,
                   help="trigger level, e.g. 1.65V (default 0)")
    p.add_argument("--falling", action="store_true", help="trigger below the level")
    p.add_argument("--wait", type=float, default=5.0, help="trigger timeout (s)")
    p.add_argument("--8bit", dest="eight_bit", action="store_true",
                   help="8-bit samples (more stream headroom)")
    p.add_argument("-o", "--out", help="write OUT.csv and OUT.vcd")
    p.add_argument("--plot", nargs="?", const="", default=None, metavar="PNG")

    p = sub.add_parser("astream", help="continuous analog stream recording")
    p.add_argument("-a", "--analog", default="0")
    p.add_argument("-r", "--rate", default="50k", help="per-channel sample rate")
    p.add_argument("--seconds", type=float, default=1.0)
    p.add_argument("--half", type=int, default=0,
                   help="interleaved values per half-frame (default rate/20)")
    p.add_argument("--8bit", dest="eight_bit", action="store_true")
    p.add_argument("-o", "--out", help="write OUT.csv and OUT.vcd")

    p = sub.add_parser("acheck", help="PASS/FAIL one analog input against an expectation")
    p.add_argument("-a", "--analog", default="0")
    p.add_argument("-r", "--rate", default="100k")
    p.add_argument("-n", "--samples", type=int, default=4000,
                   help="samples per channel to measure")
    p.add_argument("-p", "--pre", type=int, default=0)
    p.add_argument("-t", "--trigger", default=None)
    p.add_argument("--level", default=None)
    p.add_argument("--falling", action="store_true")
    p.add_argument("--wait", type=float, default=5.0)
    p.add_argument("--8bit", dest="eight_bit", action="store_true")
    p.add_argument("--mean", metavar="SPEC", help="e.g. '1.65V±5%%'")
    p.add_argument("--pp", metavar="SPEC", help="peak-to-peak, e.g. '3.2V±0.2V'")
    p.add_argument("--rms", metavar="SPEC", help="rms, e.g. '1.15V±5%%'")
    p.add_argument("--min", metavar="SPEC")
    p.add_argument("--max", metavar="SPEC")
    p.add_argument("--freq", metavar="SPEC", help="e.g. '1kHz±2%%'")
    p.add_argument("--duty", metavar="SPEC", help="e.g. '50%%±5%%'")
    p.add_argument("--temp", metavar="SPEC", help="on-chip temperature, e.g. '35°C±10°C'")

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
    elif args.cmd == "acap":
        cmd_acap(args)
    elif args.cmd == "astream":
        cmd_astream(args)
    elif args.cmd == "acheck":
        cmd_acheck(args)
    else:
        cmd_gui(args)


if __name__ == "__main__":
    main()
