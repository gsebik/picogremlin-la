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
la gui                                 # Qt GUI (needs the [gui] extra)
```

The probe is picked via `PICOGREMLIN_SERIAL` (else the only one attached).
Channel *k* is GPIO (2+k): GP16/GP17 are channels 14/15, channels 21-23 are
GP26-28.

## Compatibility

The wire protocol is the stock gusmanb command set (CMD 0..6) plus the
PicoGremlin extensions (CMD 0x40..0x48: probe info, batch streaming,
continuous/delta/edge streaming). `la info` prints the stock identity lines
and, when the firmware answers CMD 0x40, the `PICOGREMLIN:` / `STREAM:` /
`BRIDGE_TX:` capability lines. One-shot capture works against any
stock-protocol LogicAnalyzer device; streaming needs PicoGremlin firmware.
The protocol is documented in the firmware repository (`docs/LA_PROTOCOL.md`).

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v   # unit tests, no hardware
QT_QPA_PLATFORM=offscreen la gui --smoke                  # GUI smoke (no probe needed)
```

## License

GPL-3.0-only — see [LICENSE](LICENSE).
