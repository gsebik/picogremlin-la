# picogremlin-la

[![CI](https://github.com/gsebik/picogremlin-la/actions/workflows/ci.yml/badge.svg)](https://github.com/gsebik/picogremlin-la/actions/workflows/ci.yml)

Host tools for the **PicoGremlin** logic analyzer (RP2350 debug-probe +
logic-analyzer firmware, [picogremlin](https://github.com/gsebik/picogremlin)):

- **`la`** — CLI: probe identity, one-shot captures with CSV/VCD export and
  plotting, continuous streaming with measured rate.
- **`la gui`** / **`la-gui`** — Qt oscilloscope GUI: live stream with a
  software trigger that keeps periodic signals standing still, single-shot
  high-rate captures, per-channel frequency/duty measurements, Collect mode
  (measures every triggered capture: bounce count, settling time, pulse
  widths) with CSV/JSON export, Pico 40-pin header picker, PNG/CSV/VCD export.

Linux, Python ≥ 3.10. The CLI is standard-library only; the GUI needs
PyQt6 (or PySide6) + pyqtgraph + numpy.

## Install

One-liner (no clone needed):

```bash
curl -fsSL https://raw.githubusercontent.com/gsebik/picogremlin-la/main/install.sh | bash
curl -fsSL .../install.sh | bash -s -- --no-gui    # CLI only: no Qt, stdlib only
```

From a checkout:

```bash
./install.sh                # CLI + Qt GUI
./install.sh --no-gui       # CLI only
./install.sh --dev          # editable (development)
./install.sh --ref v1.0.0   # pin a tag
./install.sh --desktop      # also add an application-menu launcher
./install.sh --uninstall    # remove (--purge also deletes the shared venv)
```

The script prefers `pipx`; without it, it creates
`~/.local/share/picogremlin/venv` (the path `la gui` and the firmware's
`./pg doctor` already look for) and links `la` / `la-gui` into `~/.local/bin`.
It also checks serial-port group access and warns if the Qt system libraries
are missing. Manual equivalent:

```bash
pipx install 'picogremlin-la[gui]'
# or a plain venv:
python3 -m venv ~/.local/share/picogremlin/venv
~/.local/share/picogremlin/venv/bin/pip install -e '.[gui]'
```

## Usage

```bash
la info                                # stock identity + PicoGremlin extras
la cap -c 14,15 -t 14                  # one-shot; per-channel freq/duty/edges
la cap -c 0-7 -r 1M -n 5000 -o cap     # -> cap.csv + cap.vcd
la stream -c 14,15 -r 200k --seconds 1 -o rec   # gapless -> rec.csv/.vcd
la acap -a 0,1 -r 100k -n 2000         # analog one-shot (A0/A1 = GP26/GP27)
la astream -a 0,1 -r 50k --seconds 1   # continuous analog recording
la acheck -a A0 --mean '1.65V±5%'      # PASS/FAIL a lab measurement (exit 1 on FAIL)
la gui                                 # Qt GUI (needs the [gui] extra)
```

## Measuring in the GUI

- **Hover** the plot: a dashed line follows the pointer and the bar under the plot
  shows the time, the channel under the mouse (GP name + level), the time since its
  last edge, the **width** of the level, and the **period** (same-direction edges)
  with the frequency. Close to an edge the line snaps to it and turns into a
  dotted edge marker — the cursor gives you the edge-to-edge numbers directly.
- **Cursors** (⊢⊣ button): drag the A/B lines, or **Shift-click** = A,
  **Ctrl-click** = B. The bar shows A→B, Δt and 1/Δt (e.g. `Δ 200 µs · 5 kHz`);
  the shaded band marks the measured interval. The lines stay put while the
  stream updates, so you can park them on two edges.
- **Wheel** over the plot steps the capture-window presets — the scope timebase
  (wheel up = zoom in). View zoom is deliberately *not* on the wheel: rescaling
  an already-captured trace hides the fact that the window is the timebase.
- **Window** changes apply instantly: the history ring grows in place, so wheel
  zoom (and typing a window) never stops the stream. **Rate** and **channels**
  reconfigure the probe, so those restart the stream automatically (300 ms
  debounce) — no manual Stop/Run round trip. Single-shot **pre/post** follow the
  timebase (pre+post = window, same split), so a capture covers exactly what you
  are looking at.

The probe is picked via `PICOGREMLIN_SERIAL` (else the only one attached).
Channel *k* is GPIO (2+k): GP16/GP17 are channels 14/15, channels 21-23 are
GP26-28.

## Analog inputs

Firmware 1.0+ exposes the RP2350 ADC: **A0/A1/A2 = GP26/GP27/GP28** (the same
pins as digital channels 21..23) plus the **on-chip temperature** sensor as
input `3`/`temp`. They are 0–3.3 V only (not 5 V tolerant) and share one ADC:

- **500 kS/s total**, round-robin: 1 channel = 500 kS/s, 3 channels ≈ 166 kS/s each
- **12-bit** (0.8 mV per step) or **8-bit** (13 mV per step, more stream headroom)
- captures are either analog *or* digital at a time (the pins are switched per command)
- streams are limited by the ~700 kB/s link: 12-bit ≈ 350 kS/s, 8-bit ≈ 500 kS/s total

`la acheck` measures one input and compares it with expectations, which makes it
handy for checking lab tasks:

```bash
la acheck -a A0 --mean '1.65V±5%'                 # PWM + RC filter -> mean level
la acheck -a A1 --freq '1kHz±2%' --duty '50%±5%'
la acheck -a temp --temp '35°C±10°C'
```

Exit code is 0 when every check passes, 1 otherwise. The GUI has an **analog**
tab with the same measurements, a level trigger, a live stream and a Check
button that turns green/red for a mean/frequency expectation.

## Compatibility

The wire protocol is the stock gusmanb command set (CMD 0..6) plus the
PicoGremlin extensions (CMD 0x40..0x48: probe info, batch streaming,
continuous/delta/edge streaming). `la info` prints the stock identity lines
and, when the firmware answers CMD 0x40, the `PICOGREMLIN:` / `STREAM:` /
`BRIDGE_TX:` capability lines. One-shot capture works against any
stock-protocol LogicAnalyzer device; streaming needs PicoGremlin firmware.
The protocol is documented in the firmware repository (`docs/LA_PROTOCOL.md`).

> If the bar under the plot shows **⚠ data loss**, the probe's double buffer
> overran (the PC could not drain the USB stream fast enough). The sampled
> stream carries **no timestamps**, so time would be silently compressed and
> periods/widths would be wrong — lower the rate or close CPU-heavy apps on the
> PC.  (The firmware also has a hardware-timestamped edge stream, CMD 0x48, but
> the GUI's live view uses the sampled stream today.)

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v   # unit tests, no hardware
QT_QPA_PLATFORM=offscreen la gui --smoke                  # GUI smoke (no probe needed)
QT_QPA_PLATFORM=offscreen python tests/gui_measure_probe.py   # cursor/hover math, no probe
```

## License

GPL-3.0-only — see [LICENSE](LICENSE).
