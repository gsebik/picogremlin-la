"""Device discovery — find the PicoGremlin CDC ports.

The only backend today is USB CDC via /dev/serial/by-id (Linux). A future
simulator backend (pico-sim) plugs in here: everything above this layer talks
to a plain byte stream.
"""
import glob
import os
import re

from .protocol import SerialPort


def _detect_serial():
    """Resolve the PicoGremlin USB serial without hardcoding a board.

    Order: the PICOGREMLIN_SERIAL env var, else the single PicoGremlin probe
    found on /dev/serial/by-id, else None (find_ports then matches any
    connected LogicAnalyzer device by its product string).
    """
    env = os.environ.get("PICOGREMLIN_SERIAL")
    if env:
        return env.strip()
    serials = set()
    for p in glob.glob("/dev/serial/by-id/*LogicAnalyzer*-if03"):
        m = re.search(r"_([0-9A-Fa-f]{16})-if03$", p)
        if m:
            serials.add(m.group(1).upper())
    return next(iter(serials)) if len(serials) == 1 else None


PICOGREMLIN_SERIAL = _detect_serial()
# Export for OpenOCD child processes: the .cfg files select the adapter via
# $::env(PICOGREMLIN_SERIAL) when it is set (else the sole CMSIS-DAP device).
if PICOGREMLIN_SERIAL:
    os.environ.setdefault("PICOGREMLIN_SERIAL", PICOGREMLIN_SERIAL)


def find_ports():
    """Return (uart_path, la_path) for the PicoGremlin LA device.

    Boards are matched on /dev/serial/by-id, filtered by the USB serial from
    PICOGREMLIN_SERIAL (env or auto-detected when exactly one board is
    attached). With several boards attached and no serial chosen the result is
    (None, None), so the caller can ask the user to set PICOGREMLIN_SERIAL.
    Interface if01 = CDC0 (UART bridge), if03 = CDC1 (logic analyzer).
    """
    ports = {}          # serial -> {"if01": path, "if03": path}
    for p in sorted(glob.glob("/dev/serial/by-id/*LogicAnalyzer*")):
        if PICOGREMLIN_SERIAL and PICOGREMLIN_SERIAL not in p:
            continue
        m = re.search(r"_([0-9A-Fa-f]{16})-if(01|03)$", p)
        if not m:
            continue
        ports.setdefault(m.group(1).upper(), {})["if" + m.group(2)] = os.path.realpath(p)
    if PICOGREMLIN_SERIAL:
        entry = ports.get(PICOGREMLIN_SERIAL.upper(), {})
    elif len(ports) == 1:
        entry = next(iter(ports.values()))
    else:
        return None, None
    return entry.get("if01"), entry.get("if03")


def open_la():
    _uart, la = find_ports()
    if not la:
        raise SystemExit("no PicoGremlin LA port found — probe attached? "
                         "(set PICOGREMLIN_SERIAL when several are)")
    return SerialPort(la, 115200)
