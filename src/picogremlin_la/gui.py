#!/usr/bin/env python3
"""
la_gui — PicoGremlin logic-analyzer GUI (Qt + pyqtgraph).

Launched by `la gui` (or the `la-gui` entry point). Needs PyQt6 (or PySide6)
+ pyqtgraph.

Features: dark theme, live stream view with software trigger, single-shot
high-rate capture (button bounce), Collect mode that measures every triggered
capture (bounce count, settling time, min/avg/max pulse widths) into a results
table with CSV/JSON export, Pico 40-pin header picker, per-channel
measurements, zoom/pan, PNG/CSV export, remembered window geometry.

Protocol helpers come from the `cli` module (single source of truth).
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque

from picogremlin_la import cli as la

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    QT = "PyQt6"
except ImportError:
    from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore
    QT = "PySide6"

import pyqtgraph as pg  # noqa: E402
import numpy as np  # noqa: E402

pg.setConfigOptions(antialias=False, background="#11161c", foreground="#c9d1d9")

# ------------------------------------------------------------------ pin table
# Physical 40-pin header of the Pico 2 W. (pin number, GP label or rail, note)
LEFT = [(1, "GP0"), (2, "GP1"), (3, "GND"), (4, "GP2"), (5, "GP3"), (6, "GP4"),
        (7, "GP5"), (8, "GND"), (9, "GP6"), (10, "GP7"), (11, "GP8"), (12, "GP9"),
        (13, "GND"), (14, "GP10"), (15, "GP11"), (16, "GP12"), (17, "GP13"),
        (18, "GND"), (19, "GP14"), (20, "GP15")]
RIGHT = [(40, "VBUS"), (39, "VSYS"), (38, "GND"), (37, "3V3_EN"), (36, "3V3"),
         (35, "ADC_VREF"), (34, "GP28"), (33, "GND"), (32, "GP27"), (31, "GP26"),
         (30, "RUN"), (29, "GP22"), (28, "GND"), (27, "GP21"), (26, "GP20"),
         (25, "GP19"), (24, "GP18"), (23, "GND"), (22, "GP17"), (21, "GP16")]

NOT_CAPTURABLE = {
    "GP0": "complex-trigger loopback (jumpered to GP1)",
    "GP1": "complex-trigger loopback (jumpered to GP0)",
}


def pin_gp(label):
    return int(label[2:]) if label.startswith("GP") else None


def capturable(label):
    gp = pin_gp(label)
    return gp is not None and gp in la.CHAN_OF_PIN and label not in NOT_CAPTURABLE


CH_COLORS = ["#4fc3f7", "#ffb74d", "#81c784", "#e57373", "#ba68c8", "#4db6ac",
             "#f06292", "#aed581", "#64b5f6", "#ffd54f", "#7986cb", "#a1887f",
             "#4dd0e1", "#ff8a65", "#9575cd", "#dce775", "#90a4ae", "#f48fb1",
             "#80cbc4", "#ffcc80", "#9fa8da", "#c5e1a5", "#ef9a9a", "#b39ddb"]


def ch_color(ch):
    return CH_COLORS[ch % len(CH_COLORS)]


def bounce_metrics(samples, rate, bit, first_idx=0):
    """Button-bounce metrics for one capture.

    Counts every edge on `bit` from `first_idx` (the trigger position) on:
    edges, bounce transitions, settling time (first..last edge) and the
    min/avg/max width of the pulses between consecutive edges (µs).
    """
    arr = np.asarray(samples, dtype=np.uint32)
    b = ((arr >> bit) & 1).astype(np.int8)
    idx = np.nonzero(np.diff(b) != 0)[0] + 1
    idx = idx[idx >= first_idx]
    if len(idx) == 0:
        return None
    times = idx / rate
    widths = np.diff(times)
    return {
        "edges": int(len(idx)),
        "bounces": int(len(idx) - 1),
        "settle_ms": float(times[-1] - times[0]) * 1e3,
        "min_us": float(widths.min()) * 1e6 if len(widths) else 0.0,
        "avg_us": float(widths.mean()) * 1e6 if len(widths) else 0.0,
        "max_us": float(widths.max()) * 1e6 if len(widths) else 0.0,
    }


def list_boards():
    """Attached PicoGremlins: dicts with serial/product/la/uart/busy.

    busy = some process (e.g. OpenOCD) holds the CMSIS-DAP interface; the LA
    CDC of that board is still free for us.
    """
    import glob
    out = []
    for d in glob.glob("/sys/bus/usb/devices/*/"):
        if ":" in os.path.basename(d.rstrip("/")):
            continue
        try:
            with open(d + "idVendor") as f:
                if f.read().strip() != "2e8a":
                    continue
            with open(d + "idProduct") as f:
                if f.read().strip() != "0009":
                    continue
            with open(d + "serial") as f:
                ser = f.read().strip()
            with open(d + "product") as f:
                prod = f.read().strip()
            with open(d + "busnum") as f:
                bus = int(f.read())
            with open(d + "devnum") as f:
                dev = int(f.read())
        except (OSError, ValueError):
            continue
        if not ser:
            continue
        la = uart = None
        for l in glob.glob("/dev/serial/by-id/*"):
            if ser not in l:
                continue
            tgt = os.path.realpath(l)
            if l.endswith("-if01"):
                uart = tgt
            elif l.endswith("-if03"):
                la = tgt
        busy = False
        try:
            busy = subprocess.run(
                ["fuser", "-s", f"/dev/bus/usb/{bus:03d}/{dev:03d}"],
                capture_output=True).returncode == 0
        except (FileNotFoundError, OSError):
            pass
        out.append({"serial": ser, "product": prod, "la": la, "uart": uart,
                    "busy": busy})
    return sorted(out, key=lambda b: b["serial"])


# ---------------------------------------------------------- event detector
class EventDetector:
    """Find edge bursts (button presses) in a continuous stream.

    feed() is called with each new slice of the trigger channel; an event ends
    when no edge has arrived for gap_s seconds. Returns (first_abs, last_abs)
    sample indices of the burst when it ends.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.active = False
        self.first_abs = 0
        self.last_abs = 0
        self.seen_abs = -1

    def arm(self, total):
        """Start watching from now on (ignore history)."""
        self.reset()
        self.seen_abs = total - 1

    def feed(self, base_abs, bits, total, rate, gap_s):
        idx = np.nonzero(np.diff(bits) != 0)[0] + 1 + base_abs
        idx = idx[idx > self.seen_abs]
        if len(idx):
            self.seen_abs = int(idx[-1])
            for a in idx:
                if not self.active:
                    self.active = True
                    self.first_abs = int(a)
                self.last_abs = int(a)
        if self.active and (total - 1 - self.last_abs) / rate > gap_s:
            ev = (self.first_abs, self.last_abs)
            self.active = False
            return ev
        return None


# -------------------------------------------------------------- pin widget
class PinPicker(QtWidgets.QWidget):
    """The Pico 40-pin header: click to capture, right-click to trigger."""

    changed = QtCore.pyqtSignal()          # selection changed
    trigger = QtCore.pyqtSignal(int)       # gp wants to become the trigger

    ROW = 25
    W = 320

    def __init__(self):
        super().__init__()
        self.selected = set()              # GPIO numbers
        self.trig = None                   # GPIO number or None
        self.setMinimumSize(self.W, self.ROW * 20 + 30)
        self.setMouseTracking(True)
        self._rects = []                   # (QRect, pin, label, gp)
        self._build()

    def _build(self):
        self._rects.clear()
        for col, pins in ((0, LEFT), (1, RIGHT)):
            x = 6 + col * (self.W // 2 - 8)
            for row, (num, label) in enumerate(pins):
                y = 8 + row * self.ROW
                r = QtCore.QRect(x, y, self.W // 2 - 14, self.ROW - 4)
                self._rects.append((r, num, label, pin_gp(label)))

    def sizeHint(self):
        return QtCore.QSize(self.W, self.ROW * 20 + 30)

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        font = p.font()
        font.setPointSizeF(8.5)
        p.setFont(font)
        for rect, num, label, gp in self._rects:
            cap = capturable(label)
            sel = gp is not None and gp in self.selected
            is_trig = gp is not None and gp == self.trig
            if sel:
                ch = la.CHAN_OF_PIN[gp]
                bg = QtGui.QColor(ch_color(ch))
                fg = QtGui.QColor("#0b0f14")
            elif cap:
                bg = QtGui.QColor("#1d2630")
                fg = QtGui.QColor("#c9d1d9")
            else:
                bg = QtGui.QColor("#161b22")
                fg = QtGui.QColor("#5a6570")
            p.setBrush(bg)
            pen = QtGui.QPen(QtGui.QColor("#e6edf3") if is_trig else QtGui.QColor("#2b3440"))
            pen.setWidth(2 if is_trig else 1)
            p.setPen(pen)
            p.drawRoundedRect(rect, 4, 4)
            p.setPen(fg)
            txt = f"{label}"
            if is_trig:
                txt += "  ⚡"
            p.drawText(rect.adjusted(8, 0, -6, 0),
                       QtCore.Qt.AlignmentFlag.AlignVCenter, txt)
            p.setPen(QtGui.QColor("#7d8794"))
            p.drawText(rect.adjusted(rect.width() - 30, 0, -6, 0),
                       QtCore.Qt.AlignmentFlag.AlignVCenter |
                       QtCore.Qt.AlignmentFlag.AlignRight, str(num))

    def mousePressEvent(self, ev):
        pos = ev.position().toPoint()
        for rect, _num, label, gp in self._rects:
            if not rect.contains(pos):
                continue
            if not capturable(label):
                reason = NOT_CAPTURABLE.get(label, "not wired to the logic analyzer")
                QtWidgets.QToolTip.showText(ev.globalPosition().toPoint(),
                                            f"{label}: {reason}", self)
                return
            if ev.button() == QtCore.Qt.MouseButton.RightButton:
                self.trig = gp
                self.selected.add(gp)
                self.trigger.emit(gp)
            else:
                if gp in self.selected:
                    self.selected.discard(gp)
                    if self.trig == gp:
                        self.trig = None
                else:
                    self.selected.add(gp)
            self.changed.emit()
            self.update()
            return

    def set_selection(self, gps):
        self.selected = {g for g in gps}
        self.update()

    def set_trigger(self, gp):
        self.trig = gp
        self.update()

    def channels(self):
        chans = sorted(la.CHAN_OF_PIN[g] for g in self.selected)
        return chans or list(range(24))


# ------------------------------------------------------------------- window
class ScopePlot(pg.PlotWidget):
    """PlotWidget whose wheel changes the CAPTURE window — the scope timebase.

    pyqtgraph's default wheel zoom only rescales an already captured trace, which is
    meaningless for a live scope: the window setting *is* the timebase.  Wheel up =
    zoom in (shorter window).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.wheel_cb = None

    def wheelEvent(self, ev):
        dy = ev.angleDelta().y()
        if self.wheel_cb is not None and dy:
            self.wheel_cb(dy)
            ev.accept()
            return
        super().wheelEvent(ev)


class ScopeWindow(QtWidgets.QMainWindow):
    oneshot_done = QtCore.pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PicoGremlin LA")
        self.cfg = QtCore.QSettings("picogremlin", "la")

        # runtime state (the window stays usable without a probe: settings,
        # results table and exports still work; Run/Single stay disabled)
        try:
            self.port = la.open_la()
            self.no_probe = None
        except SystemExit as e:
            self.port = None
            self.no_probe = str(e)
        self.ring = None
        self.lock = threading.Lock()
        self.thread = None
        self.stopflag = None
        self.arming = False
        self.running = False
        self.hold = False
        self.chans = list(range(24))
        self.rate = 175_000
        self.view = None
        self.view_x = None
        self.metrics = {}
        self.results = []
        self.auto_single = False
        self.board_serial = None
        self.detector = None
        self.held_view = None
        self.held_x = None
        self.held_metrics = None
        self.annot_text = ""
        self.n = 0
        self.t0 = 0.0
        self.rate_meas = 0.0
        self.rate_t = 0.0
        self.rate_n = 0
        self.frames = 0
        self.aligned = False

        # Redraw/measurement state.  The UI thread must never do unbounded work per
        # timer tick — that is what shows up as "(Not Responding)": a long window at a
        # high rate used to rebuild every plot item and re-scan the whole ring 12x/s.
        self._drawn_total = -1
        self._draw_t = 0.0
        self._det_fed = 0
        self._curve_key = None
        self._curve_items = []
        self._view_bits = []
        self._view_serial = 0
        self._hover_on = False
        self._hover_ms = None
        self._hover_y = 0.0
        self._hover_k = -1
        self._hover_t = 0.0
        self._hover_edges = None
        self._hover_bit = 0
        self._hover_text = ""
        self.win_samples = 0
        self.ring_cap = 0
        self.ring_pos = 0
        self.ring_total = 0

        # Changing rate/window/channels reconfigures the probe: debounce and restart
        # automatically instead of making the user press Stop then Run.
        self.restart_timer = QtCore.QTimer(self)
        self.restart_timer.setSingleShot(True)
        self.restart_timer.setInterval(300)
        self.restart_timer.timeout.connect(self._restart_stream)

        # connect once here: connecting per one_shot() call would stack
        # duplicate connections and fire _show_oneshot N times per capture
        self.oneshot_done.connect(self._show_oneshot)

        self._build_ui()
        self.setMinimumSize(1300, 800)
        self._restore()
        self.resize(max(self.width(), 1300), max(self.height(), 800))
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(80)

    # ------------------------------------------------------------- UI build
    def _build_ui(self):
        tb = self.addToolBar("main")
        tb.setMovable(False)
        tb.setIconSize(QtCore.QSize(18, 18))

        self.btn_run = QtWidgets.QToolButton()
        self.btn_run.setText("▶  Run")
        self.btn_run.clicked.connect(self.start)
        self.btn_run.setToolTip("Start the continuous stream (the trigger field keeps "
                                "periodic signals standing still)")
        self.btn_stop = QtWidgets.QToolButton()
        self.btn_stop.setText("■  Stop")
        self.btn_stop.clicked.connect(self.stop)
        self.btn_stop.setToolTip("Stop the stream / cancel an armed single-shot")
        self.btn_single = QtWidgets.QToolButton()
        self.btn_single.setText("⚡  Single")
        self.btn_single.clicked.connect(self.one_shot)
        self.btn_single.setToolTip(
            "Single-shot: arm one high-rate capture on the trigger channel+edge\n"
            "using pre/post below, then cause the event.\n"
            "While armed, press Single again (or Stop) to cancel.")
        self.btn_collect = QtWidgets.QToolButton()
        self.btn_collect.setText("⟳  Collect")
        self.btn_collect.setCheckable(True)
        self.btn_collect.toggled.connect(self._collect_toggled)
        self.btn_collect.setToolTip(
            "Collect mode: every triggered capture is measured (bounces, settling\n"
            "time, pulse widths) into the captures tab, and the probe re-arms\n"
            "automatically for the next press. Stop ends the collection.")
        self.btn_hold = QtWidgets.QToolButton()
        self.btn_hold.setText("⏸  Hold")
        self.btn_hold.setCheckable(True)
        self.btn_hold.toggled.connect(self._hold_toggled)
        self.btn_hold.setToolTip("Freeze the display (the stream keeps running)")
        tb.addWidget(QtWidgets.QLabel(" board "))
        self.cb_dev = QtWidgets.QComboBox()
        self.cb_dev.setFixedWidth(230)
        self.cb_dev.currentIndexChanged.connect(self._dev_changed)
        tb.addWidget(self.cb_dev)
        tb.addSeparator()
        for b in (self.btn_run, self.btn_stop, self.btn_single, self.btn_collect,
                  self.btn_hold):
            b.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
            b.setAutoRaise(False)
            b.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            tb.addWidget(b)
        tb.addSeparator()

        tb.addWidget(QtWidgets.QLabel(" rate "))
        self.cb_rate = QtWidgets.QComboBox()
        self.cb_rate.setEditable(True)
        self.cb_rate.addItems(["10k", "50k", "100k", "200k", "500k",
                               "1M", "2M", "5M", "10M", "25M"])
        self.cb_rate.setCurrentText("1M")
        self.cb_rate.setFixedWidth(80)
        self.cb_rate.currentTextChanged.connect(lambda _=None: self._update_maxpost())
        self.cb_rate.activated.connect(self._auto_restart)
        self.cb_rate.lineEdit().editingFinished.connect(self._auto_restart)
        tb.addWidget(self.cb_rate)

        tb.addWidget(QtWidgets.QLabel("  window ms "))
        self.cb_win = QtWidgets.QComboBox()
        self.cb_win.setEditable(True)
        self.cb_win.addItems(["0.5", "1", "2", "5", "10", "20", "50", "100", "200",
                              "500", "1000", "2000", "5000"])
        self.cb_win.setFixedWidth(70)
        self.cb_win.activated.connect(self._window_changed)
        self.cb_win.lineEdit().editingFinished.connect(self._window_changed)
        tb.addWidget(self.cb_win)

        tb.addWidget(QtWidgets.QLabel("  trigger "))
        self.cb_trig = QtWidgets.QComboBox()
        self.cb_trig.setFixedWidth(120)
        self._fill_triggers()
        self.cb_trig.currentIndexChanged.connect(self._trigger_picked)
        self.cb_trig.setToolTip("Trigger for both modes: the live view uses it to hold "
                                "the trace still; Single-shot waits for this edge\n"
                                "(a pin right-click in the header sets it too)")
        tb.addWidget(self.cb_trig)

        self.cb_edge = QtWidgets.QComboBox()
        self.cb_edge.addItems(["rising", "falling"])
        tb.addWidget(self.cb_edge)

        tb.addWidget(QtWidgets.QLabel("  single: pre "))
        self.sb_pre = QtWidgets.QDoubleSpinBox()
        self.sb_pre.setRange(0, 1000)
        self.sb_pre.setSingleStep(0.1)
        self.sb_pre.setValue(0.5)
        self.sb_pre.setSuffix(" ms")
        self.sb_pre.setFixedWidth(90)
        self.sb_pre.setToolTip("Single-shot: capture time before the trigger edge")
        self.sb_pre.valueChanged.connect(lambda _=None: self._update_maxpost())
        tb.addWidget(self.sb_pre)
        tb.addWidget(QtWidgets.QLabel(" post "))
        self.sb_post = QtWidgets.QDoubleSpinBox()
        self.sb_post.setRange(0.001, 5000)
        self.sb_post.setSingleStep(0.1)
        self.sb_post.setValue(4.9)
        self.sb_post.setSuffix(" ms")
        self.sb_post.setFixedWidth(95)
        self.sb_post.setToolTip("Single-shot: capture time after the trigger edge")
        tb.addWidget(self.sb_post)
        self.store_btn = QtWidgets.QToolButton()
        self.store_btn.setText("store")
        self.store_btn.setCheckable(True)
        self.store_btn.setChecked(True)
        self.store_btn.setToolTip(
            "Store each measured capture (Single and Collect) in the captures\n"
            "tab with CSV/JSON export. Untick to just watch the measurements\n"
            "on the plot without recording them.")
        self.store_btn.toggled.connect(lambda _=None: self._style_buttons())
        tb.addWidget(self.store_btn)
        self.fit_btn = QtWidgets.QToolButton()
        self.fit_btn.setText("auto-fit")
        self.fit_btn.setCheckable(True)
        self.fit_btn.setChecked(True)
        self.fit_btn.setToolTip(
            "If the single-shot window doesn't fit the capture buffer, lower the\n"
            "sample rate automatically (keeping your pre/post ms). Untick to get\n"
            "a warning instead, and set the rate yourself.")
        self.fit_btn.toggled.connect(lambda _=None: self._style_buttons())
        tb.addWidget(self.fit_btn)
        self.cursors_btn = QtWidgets.QToolButton()
        self.cursors_btn.setText("⊢⊣  Cursors")
        self.cursors_btn.setCheckable(True)
        self.cursors_btn.setToolTip(
            "Time cursors A and B: drag them, or Shift-click / Ctrl-click the plot.\n"
            "The bar under the plot shows A→B (Δt and 1/Δt).")
        self.cursors_btn.toggled.connect(self._cursors_toggled)
        tb.addWidget(self.cursors_btn)
        self.maxpost_lbl = QtWidgets.QLabel("")
        self.maxpost_lbl.setStyleSheet("color:#8b949e; padding-left:4px;")
        self.maxpost_lbl.setToolTip(
            "Largest post time that fits the probe's capture buffer for the\n"
            "current rate and pin selection (≈192 kB per half-buffer)")
        tb.addWidget(self.maxpost_lbl)

        tb.addSeparator()
        self.data_actions = []
        for name, slot in (("PNG", self.save_png), ("CSV", self.save_csv),
                           ("VCD", self.save_vcd)):
            a = QtGui.QAction(name, self)
            a.triggered.connect(slot)
            a.setEnabled(False)
            self.data_actions.append(a)
        for a in self.data_actions:
            tb.addAction(a)

        self.state_lbl = QtWidgets.QLabel()
        self.blink = False
        self.blink_timer = QtCore.QTimer(self)
        self.blink_timer.timeout.connect(self._blink_tick)
        self._set_state("stopped")

        # sidebar: pin picker + measurements
        side = QtWidgets.QWidget()
        sv = QtWidgets.QVBoxLayout(side)
        sv.setContentsMargins(6, 6, 6, 6)
        row = QtWidgets.QHBoxLayout()
        for name, gps in (("All", None), ("None", []),
                          ("GP14/15", [14, 15]), ("GP16/17", [16, 17]),
                          ("GP2-9", list(range(2, 10)))):
            b = QtWidgets.QPushButton(name)
            b.clicked.connect(lambda _=False, g=gps: self._preset(g))
            row.addWidget(b)
        sv.addLayout(row)
        self.pins = PinPicker()
        self.pins.changed.connect(self._pins_changed)
        self.pins.trigger.connect(self._pin_trigger)
        sv.addWidget(self.pins)
        self.lbl_sel = QtWidgets.QLabel()
        self.lbl_sel.setWordWrap(True)
        sv.addWidget(self.lbl_sel)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["GP", "freq (Hz)", "duty %", "ch"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)

        self.tabs = QtWidgets.QTabWidget()
        ch_tab = QtWidgets.QWidget()
        ctl = QtWidgets.QVBoxLayout(ch_tab)
        ctl.setContentsMargins(0, 2, 0, 0)
        ctl.addWidget(self.table)
        self.tabs.addTab(ch_tab, "channels")

        cap_tab = QtWidgets.QWidget()
        cpl = QtWidgets.QVBoxLayout(cap_tab)
        cpl.setContentsMargins(0, 2, 0, 0)
        brow = QtWidgets.QHBoxLayout()
        for name, slot in (("Clear", self.clear_results),
                           ("Export CSV", self.export_csv),
                           ("Export JSON", self.export_json)):
            b = QtWidgets.QPushButton(name)
            b.clicked.connect(slot)
            brow.addWidget(b)
        cpl.addLayout(brow)
        self.results_table = QtWidgets.QTableWidget(0, 7)
        self.results_table.setHorizontalHeaderLabels(
            ["#", "edges", "bounce", "settle ms", "min µs", "avg µs", "max µs"])
        self.results_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.Stretch)
        f = self.results_table.font()
        f.setPointSizeF(8.0)
        self.results_table.setFont(f)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        cpl.addWidget(self.results_table, 1)
        self.summary_lbl = QtWidgets.QLabel("no captures yet — use ⚡ Single or ⟳ Collect")
        self.summary_lbl.setWordWrap(True)
        cpl.addWidget(self.summary_lbl)
        self.tabs.addTab(cap_tab, "captures")

        sv.addWidget(self.tabs, 1)
        side.setFixedWidth(340)

        # plot — the measurement overlays (cursors, hover) are created once and never
        # cleared; _draw() updates the curve items in place instead of rebuilding them.
        self.plot = ScopePlot()
        self.plot.wheel_cb = self._wheel_window
        self.plot.showGrid(x=True, y=False, alpha=0.15)
        self.plot.setLabel("bottom", "time (ms)")
        self.plot.getAxis("left").setWidth(150)
        self.annot = pg.TextItem(anchor=(0, 0))
        self.annot.setZValue(20)
        self.plot.addItem(self.annot)
        self.curves = []

        self.cur_a = pg.InfiniteLine(angle=90, movable=True, label="A",
                                     pen=pg.mkPen("#58a6ff", width=1.4),
                                     labelOpts={"position": 0.94, "color": "#58a6ff"})
        self.cur_b = pg.InfiniteLine(angle=90, movable=True, label="B",
                                     pen=pg.mkPen("#f0883e", width=1.4),
                                     labelOpts={"position": 0.90, "color": "#f0883e"})
        self.cur_region = pg.LinearRegionItem(brush=pg.mkBrush(88, 166, 255, 26),
                                              pen=pg.mkPen(None), movable=False)
        self.hover_line = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen("#8b949e", width=1, style=QtCore.Qt.PenStyle.DashLine))
        self.edge_line = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen("#7ee787", width=1, style=QtCore.Qt.PenStyle.DotLine))
        for it in (self.cur_region, self.cur_a, self.cur_b, self.edge_line,
                   self.hover_line):
            it.setZValue(40)
            it.setVisible(False)
            self.plot.addItem(it)
        self.cur_a.sigPositionChanged.connect(self._cursors_changed)
        self.cur_b.sigPositionChanged.connect(self._cursors_changed)
        self.plot.scene().sigMouseMoved.connect(self._hover_moved)
        self.plot.scene().sigMouseClicked.connect(self._plot_clicked)

        central = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(side)
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        rv.addWidget(self.plot, 1)
        self.meas_lbl = QtWidgets.QLabel()
        self.meas_lbl.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.meas_lbl.setStyleSheet("color:#8b949e; padding:3px 6px;")
        mf = self.meas_lbl.font()
        mf.setFamily("monospace")
        mf.setPointSizeF(8.5)
        self.meas_lbl.setFont(mf)
        self.meas_lbl.setMinimumWidth(600)
        rv.addWidget(self.meas_lbl)
        h.addWidget(right, 1)
        self.setCentralWidget(central)

        self.status = self.statusBar()
        self.dev_lbl = QtWidgets.QLabel()
        self.dev_lbl.setStyleSheet("color:#f85149; padding-right:12px;")
        self.status.addPermanentWidget(self.dev_lbl)
        self.status.addPermanentWidget(self.state_lbl)   # always visible, right side
        if self.no_probe:
            self.status.showMessage(f"no probe: {self.no_probe}")
        else:
            self.status.showMessage("ready — pick pins, then ▶ Run")
        self._refresh_devices()
        self._check_device()
        self.dev_timer = QtCore.QTimer(self)
        self.dev_timer.timeout.connect(self._check_device)
        self.dev_timer.start(2000)

    def _set_state(self, state, detail=""):
        """State pill + button availability: stopped / running / armed / single."""
        changed = state != getattr(self, "state", None)
        self.state = state
        styles = {
            "stopped": ("#2b3440", "#c9d1d9", "STOPPED"),
            "running": ("#1f6f3f", "#ffffff", "RUNNING"),
            "armed":   ("#d29922", "#11161c", "ARMED · waiting for trigger"),
            "single":  ("#2f81f7", "#ffffff", "SINGLE CAPTURE"),
        }
        bg, fg, text = styles[state]
        self._state_style = (bg, fg)
        if detail:
            text += f" · {detail}"
        self.state_lbl.setText(f"  {text}  ")
        self.blink = False
        self._apply_state_style()
        if state == "armed":
            self.blink_timer.start(450)
        else:
            self.blink_timer.stop()
        if changed:
            self._update_actions()

    QSS = {
        "normal": "QToolButton{background:#1b232d;border:1px solid #2b3440;"
                  "color:#c9d1d9;padding:3px 10px;border-radius:6px;}"
                  "QToolButton:hover{border-color:#3d4854;background:#202a35;}",
        "disabled": "QToolButton{background:#12171d;border:1px solid #1b222a;"
                    "color:#454f5b;padding:3px 10px;border-radius:6px;}",
        "green": "QToolButton{background:#1f6f3f;border:1px solid #2ea043;"
                 "color:#ffffff;padding:3px 10px;border-radius:6px;font-weight:600;}",
        "amber": "QToolButton{background:#c69026;border:1px solid #e3b341;"
                 "color:#11161c;padding:3px 10px;border-radius:6px;font-weight:600;}",
        "red": "QToolButton{background:#3d1d1d;border:1px solid #f85149;"
               "color:#ffb3ae;padding:3px 10px;border-radius:6px;}"
               "QToolButton:hover{background:#4c2323;}",
        "blue": "QToolButton{background:#1f4f8f;border:1px solid #2f81f7;"
                "color:#ffffff;padding:3px 10px;border-radius:6px;font-weight:600;}"
                "QToolButton:checked{background:#1f4f8f;border:1px solid #2f81f7;"
                "color:#ffffff;}",
    }

    def _style_buttons(self):
        s = self.QSS
        st = getattr(self, "state", "stopped")

        def put(btn, enabled, active=None):
            if not enabled:
                btn.setStyleSheet(s["disabled"])
            elif active:
                btn.setStyleSheet(s[active])
            else:
                btn.setStyleSheet(s["normal"])

        put(self.btn_run, st in ("stopped", "single"), "green" if st == "running" else None)
        put(self.btn_stop, st in ("running", "armed"), "red")
        put(self.btn_single, True, "amber" if st == "armed" else None)
        put(self.btn_collect, self.port is not None or self.btn_collect.isChecked(),
            "green" if self.btn_collect.isChecked() else None)
        put(self.fit_btn, True, "blue" if self.fit_btn.isChecked() else None)
        if st == "running":
            put(self.btn_hold, True, "blue" if self.btn_hold.isChecked() else None)
        else:
            put(self.btn_hold, False)

    def _apply_state_style(self):
        bg, fg = self._state_style
        if self.state == "armed" and self.blink:
            bg = "#7d5c0a"
        self.state_lbl.setStyleSheet(
            f"background:{bg}; color:{fg}; border-radius:9px;"
            f"padding:3px 12px; font-weight:600;")

    def _blink_tick(self):
        self.blink = not self.blink
        self._apply_state_style()

    def _collect_toggled(self, on):
        self._style_buttons()
        if not on:
            self.detector = None
            self.status.showMessage("collect off (the stream keeps running)")
            return
        if self.trig_channel() is None:
            self.btn_collect.setChecked(False)
            self.status.showMessage("collect needs a trigger channel")
            return
        if not self.running:
            self.start()
        if self.running:
            self._arm_detector()
            self.status.showMessage(
                "collect (stream): every press is detected, measured into the "
                "captures tab and shown on the plot; Stop ends it")

    def _arm_detector(self):
        self.detector = EventDetector()
        self.detector.arm(self.ring_total)
        self._det_fed = self.ring_total      # only samples after arming are fed
        self.held_view = None          # back to live until the next trigger
        self.annot_text = ''

    def _ring_slice(self, a0, a1):
        """Samples for absolute indices [a0, a1) — None when already gone."""
        with self.lock:
            return self._ring_slice_unlocked(a0, a1)

    def _ring_slice_unlocked(self, a0, a1):
        total, pos, cap = self.ring_total, self.ring_pos, self.ring_cap
        a0 = max(a0, total - cap, 0)
        a1 = min(a1, total)
        if a1 <= a0:
            return None
        i0 = (pos - (total - a0)) % cap
        cnt = a1 - a0
        if i0 + cnt <= cap:
            return self.ring[i0:i0 + cnt].copy()
        return np.concatenate((self.ring[i0:], self.ring[:(i0 + cnt) % cap]))

    def _grow_ring(self, need):
        """Grow the history ring in place (no stream restart) keeping absolute indices.

        The window is a display parameter; reallocating + copying the last `have`
        samples costs a few ms, so a larger window applies immediately instead of
        stopping and restarting the probe.
        """
        need = min(need, 8_000_000)                 # 32 MB of uint32, hard cap
        with self.lock:
            cap = self.ring_cap
            if need <= cap:
                return True
            newcap = min(max(need, cap * 2), 8_000_000)
            total = self.ring_total
            have = min(total, cap)
            hist = self._ring_slice_unlocked(total - have, total) if have else None
            new = np.zeros(newcap, dtype=np.uint32)
            if have and hist is not None:
                end = total % newcap
                start = (end - have) % newcap
                if start + have <= newcap:
                    new[start:start + have] = hist
                else:
                    k = newcap - start
                    new[start:] = hist[:k]
                    new[:have - k] = hist[k:]
            self.ring, self.ring_cap, self.ring_pos = new, newcap, total % newcap
            return True

    def _feed_detector(self, n):
        """Look for finished bursts; measure + store + freeze the display."""
        trig = self.trig_channel()
        if self.detector is None or trig is None:
            return False
        # Only NEW samples: feeding the detector the whole ring on every tick made a
        # long window's tick O(ring) and left the window unresponsive.
        start = max(self._det_fed, n - self.ring_cap, 0)
        if start >= n:
            return False
        seen = self._ring_slice(start, n)
        self._det_fed = n
        if seen is None or not len(seen):
            return False
        rate = self.rate
        gap_s = max(self.sb_post.value() / 1000.0, 0.05)
        bits = ((seen >> trig) & 1).astype(np.int8)
        ev = self.detector.feed(start, bits, n, rate, gap_s)
        if ev is None:
            return False
        first, last = ev
        pre_abs = max(0, int(self.sb_pre.value() / 1000.0 * rate))
        post_abs = int(self.sb_post.value() / 1000.0 * rate)
        a0 = max(0, first - pre_abs)
        a1 = min(n, last + post_abs + 1)
        win = self._ring_slice(a0, a1)
        if win is None or not len(win):
            return False
        m = bounce_metrics(win, rate, trig, first - a0)
        if m is None:
            # the burst scrolled out of the ring before the gap closed
            self.status.showMessage("collect: a burst scrolled out of the buffer "
                                    "(lower post ms or raise the window)")
            return False
        self._handle_measure(m, trig)
        x = (np.arange(len(win)) - (first - a0)) / rate * 1e3
        self.held_view, self.held_x, self.held_metrics = win, x, m
        return True

    def clear_results(self):
        self.results = []
        self.results_table.setRowCount(0)
        self.summary_lbl.setText("no captures yet — use ⚡ Single or ⟳ Collect")

    def _handle_measure(self, m, trig_ch):
        """Annotate the plot; append to the captures tab only when storing."""
        self.annot_text = (
            f"{'' if self.store_btn.isChecked() else 'watch · '}"
            f"{m['edges']} edges · {m['bounces']} bounce · "
            f"settle {m['settle_ms']:.3f} ms · pulse {m['min_us']:.0f}–"
            f"{m['max_us']:.0f} µs (avg {m['avg_us']:.0f})")
        if self.store_btn.isChecked():
            self._add_result(m, trig_ch)

    def _add_result(self, m, trig_ch):
        m["n"] = len(self.results) + 1
        m["time"] = time.strftime("%H:%M:%S")
        self.results.append(m)
        i = len(self.results) - 1
        self.results_table.setRowCount(len(self.results))
        vals = [str(m["n"]), str(m["edges"]), str(m["bounces"]),
                f"{m['settle_ms']:.3f}", f"{m['min_us']:.1f}", f"{m['avg_us']:.1f}",
                f"{m['max_us']:.1f}"]
        for j, v in enumerate(vals):
            item = self.results_table.item(i, j)
            if item is None:
                item = QtWidgets.QTableWidgetItem()
                self.results_table.setItem(i, j, item)
            item.setText(v)
        self.results_table.scrollToBottom()
        b = [r["bounces"] for r in self.results]
        s = [r["settle_ms"] for r in self.results]
        self.summary_lbl.setText(
            f"{len(self.results)} captures · bounces min/avg/max "
            f"{min(b)}/{sum(b) / len(b):.1f}/{max(b)} · settle "
            f"{min(s):.3f}/{sum(s) / len(s):.3f}/{max(s):.3f} ms · ch{trig_ch}")
        self.annot_text = f"#{m['n']} · " + self.annot_text

    def export_results(self, path, as_json=False):
        keys = ["n", "time", "edges", "bounces", "settle_ms", "min_us", "avg_us", "max_us"]
        if as_json:
            with open(path, "w") as f:
                json.dump(self.results, f, indent=1)
        else:
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(keys)
                for r in self.results:
                    w.writerow([f"{r[k]:.4g}" if isinstance(r.get(k), float)
                                else r.get(k, "") for k in keys])
        return len(self.results)

    def export_csv(self):
        if not self.results:
            self.status.showMessage("no captures to export yet")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export results", "bounce_results.csv", "CSV (*.csv)")
        if path:
            n = self.export_results(path)
            self.status.showMessage(f"wrote {path} ({n} captures)")

    def export_json(self):
        if not self.results:
            self.status.showMessage("no captures to export yet")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export results", "bounce_results.json", "JSON (*.json)")
        if path:
            n = self.export_results(path, as_json=True)
            self.status.showMessage(f"wrote {path} ({n} captures)")

    def _hold_toggled(self, on):
        self.hold = on
        if self.running:
            self._set_state("running", "display held" if on else
                            f"{self.rate_meas / 1e3:.0f} kHz")
            self._style_buttons()

    @staticmethod
    def _probe_serial():
        import glob
        import re
        for p in glob.glob("/dev/serial/by-id/*"):
            if "PicoGremlin" not in p and "LogicAnalyzer" not in p \
                    and "Debug_Probe" not in p:
                continue
            m = re.search(r"_([0-9A-Fa-f]{16})-if", p)
            if m:
                return m.group(1).upper()
        return None

    @staticmethod
    def _bootsel_present():
        import glob
        for d in glob.glob("/sys/bus/usb/devices/*/"):
            try:
                with open(os.path.join(d, "idVendor")) as f:
                    if f.read().strip() != "2e8a":
                        continue
                with open(os.path.join(d, "idProduct")) as f:
                    if f.read().strip() == "000f":
                        return True
            except OSError:
                continue
        return False

    def _refresh_devices(self):
        boards = list_boards()
        cur = self.board_serial
        want = cur or (str(self.cfg.value("dev", "")) or None)
        pick = None
        if want and any(b["serial"] == want for b in boards):
            pick = want
        elif len(boards) == 1:
            pick = boards[0]["serial"]
        elif boards:
            pick = next((b["serial"] for b in boards if not b["busy"]),
                        boards[0]["serial"])
        self.cb_dev.blockSignals(True)
        self.cb_dev.clear()
        for b in boards:
            tag = "  ● openocd" if b["busy"] else ""
            self.cb_dev.addItem(f"{b['serial']}{tag}", b["serial"])
        if pick:
            i = self.cb_dev.findData(pick)
            if i >= 0:
                self.cb_dev.setCurrentIndex(i)
        self.cb_dev.blockSignals(False)
        present = [b["serial"] for b in boards]
        if pick and pick != self.board_serial and (
                self.board_serial is None or self.board_serial not in present):
            self._open_board(pick)

    def _dev_changed(self, _idx):
        ser = self.cb_dev.currentData()
        if ser and ser != self.board_serial:
            self._open_board(ser)

    def _open_board(self, ser):
        self.stop_quiet()
        self.btn_collect.setChecked(False)
        if self.port:
            try:
                self.port.close()
            except Exception:
                pass
        self.port = None
        os.environ["PICOGREMLIN_SERIAL"] = ser
        try:
            self.port = la.open_la()
            self.no_probe = None
            self.board_serial = ser
            self.cfg.setValue("dev", ser)
            self.status.showMessage(f"board {ser} selected")
        except SystemExit as e:
            self.no_probe = str(e)
            self.status.showMessage(f"board {ser}: {e}")
        self._check_device()
        self._update_actions()

    def _check_device(self):
        """Live probe indicator: connected / BOOTSEL / absent; reconnects itself."""
        present = [self.cb_dev.itemData(i) for i in range(self.cb_dev.count())]
        if not present or (self.board_serial and self.board_serial not in present):
            self._refresh_devices()
        try:
            ports = la.find_ports()
        except SystemExit:
            ports = (None, None)
        connected = bool(ports and ports[1])
        if connected:
            if self.port is None:
                try:
                    self.port = la.open_la()
                    self.no_probe = None
                    self.status.showMessage("probe connected")
                    self._update_actions()
                except SystemExit as e:
                    self.no_probe = str(e)
            if self.port is not None:
                serial = self._probe_serial()
                self.dev_lbl.setText("● PicoGremlin" + (f" {serial}" if serial else ""))
                self.dev_lbl.setStyleSheet("color:#2ea043; padding-right:12px;")
                self._update_actions()
        else:
            if self.port is not None:
                self.stop_quiet()
                try:
                    self.port.close()
                except Exception:
                    pass
                self.port = None
                self.no_probe = "probe disconnected"
                self.status.showMessage("probe disconnected — plug it in and it "
                                        "reconnects automatically")
                self._update_actions()
            if self._bootsel_present():
                self.dev_lbl.setText("● BOOTSEL (no app)")
                self.dev_lbl.setStyleSheet("color:#d29922; padding-right:12px;")
            else:
                self.dev_lbl.setText("● no probe")
                self.dev_lbl.setStyleSheet("color:#f85149; padding-right:12px;")

    def _capture_limits(self):
        """(max samples, pre samples, post samples) for the current settings."""
        rate = la.parse_rate(self.cb_rate.currentText())
        mode, bps = la.mode_bps(self.chans)
        maxs = {0: 393216, 1: 196608, 2: 98304}[mode]
        pre = max(0, int(self.sb_pre.value() / 1000.0 * rate))
        post = max(1, int(self.sb_post.value() / 1000.0 * rate))
        return maxs, pre, post

    def _update_maxpost(self):
        try:
            maxs, pre, _post = self._capture_limits()
            rate = la.parse_rate(self.cb_rate.currentText())
        except Exception:
            return
        max_ms = max(0.0, (maxs - pre) / rate * 1e3)
        self.maxpost_lbl.setText(f"(max {max_ms:,.1f} ms)")

    def _update_actions(self):
        st = self.state
        running = st == "running"
        armed = st == "armed"
        self.btn_run.setEnabled(self.port is not None
                                and st in ("stopped", "single"))
        self.btn_stop.setEnabled(running or armed)
        self.btn_single.setEnabled(self.port is not None)   # doubles as cancel
        self.btn_collect.setEnabled(self.port is not None)
        self.btn_hold.setEnabled(running)
        if not running:
            self.btn_hold.setChecked(False)
        self._style_buttons()

    def _fill_triggers(self):
        self.cb_trig.blockSignals(True)
        self.cb_trig.clear()
        self.cb_trig.addItem("off", None)
        for gp in range(2, 29):
            if gp in la.CHAN_OF_PIN:
                ch = la.CHAN_OF_PIN[gp]
                self.cb_trig.addItem(f"ch{ch} GP{gp}", gp)
        self.cb_trig.blockSignals(False)

    # ----------------------------------------------------------- persistence
    def _restore(self):
        g = self.cfg.value("geometry")
        if g:
            self.restoreGeometry(g)
        else:
            self.resize(1500, 950)
        self.cb_rate.setCurrentText(self.cfg.value("rate", "1M") or "1M")
        self.cb_win.setCurrentText(self.cfg.value("win", "10") or "10")
        self.sb_pre.setValue(float(self.cfg.value("pre", 0.5)))
        self.sb_post.setValue(float(self.cfg.value("post", 4.9)))
        gps = self.cfg.value("pins")
        if gps:
            try:
                self.pins.set_selection({int(g) for g in gps})
            except (TypeError, ValueError):
                self.pins.set_selection(range(2, 23))
        else:
            self.pins.set_selection([14, 15])
        self.fit_btn.setChecked(
            str(self.cfg.value("fit", "true")).lower() == "true")
        self.store_btn.setChecked(
            str(self.cfg.value("store", "true")).lower() == "true")
        trig = self.cfg.value("trig", 14)
        if trig:
            gp = int(trig)
            self.pins.set_trigger(gp)
            for i in range(self.cb_trig.count()):
                if self.cb_trig.itemData(i) == gp:
                    self.cb_trig.setCurrentIndex(i)
                    break
        self._pins_changed()

    def _save_cfg(self):
        self.cfg.setValue("geometry", self.saveGeometry())
        self.cfg.setValue("rate", self.cb_rate.currentText())
        self.cfg.setValue("win", self.cb_win.currentText())
        self.cfg.setValue("pre", self.sb_pre.value())
        self.cfg.setValue("post", self.sb_post.value())
        self.cfg.setValue("pins", sorted(self.pins.selected))
        self.cfg.setValue("trig", self.pins.trig)
        self.cfg.setValue("fit", self.fit_btn.isChecked())
        self.cfg.setValue("store", self.store_btn.isChecked())

    def closeEvent(self, ev):
        self.stop_quiet()
        if getattr(self, "persist", True):
            self._save_cfg()
        super().closeEvent(ev)

    # --------------------------------------------------------------- pins
    def _preset(self, gps):
        if gps is None:
            self.pins.set_selection(sorted(la.CHAN_OF_PIN))
        else:
            self.pins.set_selection(gps)
        self._pins_changed()

    def _pins_changed(self):
        self.chans = self.pins.channels()
        gps = sorted(self.pins.selected)
        self._update_maxpost()
        mode, bps = la.mode_bps(self.chans)
        want = la.parse_rate(self.cb_rate.currentText() or "200k")
        clamp, note = la.clamp_rate(want, bps)
        txt = (f"{len(gps)} pins selected · {mode * 8 + 8}-ch mode "
               f"({bps} B/sample)"
               + (f" · streams at {clamp / 1e3:g} kHz" if note else ""))
        self.lbl_sel.setText(txt)
        if not gps:
            self.pins.set_selection([16, 17])
            self.chans = self.pins.channels()
        self._trigger_picked()
        self._auto_restart()          # channels define the stream mode

    def _pin_trigger(self, gp):
        for i in range(self.cb_trig.count()):
            if self.cb_trig.itemData(i) == gp:
                self.cb_trig.setCurrentIndex(i)
                break
        self._pins_changed()

    def _trigger_picked(self):
        gp = self.cb_trig.currentData()
        self.pins.set_trigger(gp)
        if gp is not None and gp in self.pins.selected:
            pass  # fine
        elif gp is not None:
            self.pins.selected.add(gp)
            self._pins_changed()

    def trig_channel(self):
        gp = self.cb_trig.currentData()
        return la.CHAN_OF_PIN[gp] if gp is not None else None

    # -------------------------------------------------------------- stream
    def start(self):
        if self.port is None:
            self.status.showMessage(self.no_probe or "no probe")
            return
        if self.arming:
            self.status.showMessage("single armed — Stop cancels it first")
            return
        if self.running:
            return
        rate = la.parse_rate(self.cb_rate.currentText())
        mode, bps = la.mode_bps(self.chans)
        rate, note = la.clamp_rate(rate, bps)
        half = int(max(1024, min(196608 // bps, rate // 5)))
        started, leftover, bps, _m = la.stream_start(self.port, self.chans, rate, half)
        if not started:
            self._set_state("stopped")
            ser = self._probe_serial() or "<serial>"
            self.status.showMessage(
                f"stream did not start (STREAM_ERROR?) — if this keeps happening, "
                f"reboot the probe: picotool reboot -f --ser {ser}")
            return
        try:
            win_ms = float(self.cb_win.currentText())
        except ValueError:
            win_ms = 10.0
        win_samples = min(max(int(win_ms / 1000.0 * rate), 64), 2_000_000)
        self.rate = rate
        self.win_samples = win_samples
        event_ms = (self.sb_pre.value()
                    + max(self.sb_post.value(), 50.0) + 100.0)
        self.ring_cap = max(2 * win_samples,
                            int(event_ms / 1000.0 * rate) + max(rate // 5, 4096),
                            8192)
        self.ring = np.zeros(self.ring_cap, dtype=np.uint32)
        self.ring_pos = 0
        self.ring_total = 0
        self._det_fed = 0
        self._drawn_total = -1
        self._draw_t = 0.0
        self.metrics = {}
        self._view_bits = []
        self.leftover = leftover
        self.running = True
        self.n = self.frames = 0
        self.rate_meas = 0.0
        self.rate_t = 0.0
        self.t0 = time.monotonic()
        self.stopflag = threading.Event()
        self.thread = threading.Thread(target=self._reader,
                                       args=(bps, self.stopflag), daemon=True)
        self.thread.start()
        if self.btn_collect.isChecked():
            self._arm_detector()
        self._set_state("running", f"{rate / 1e3:g} kHz")
        msg = f"streaming {len(self.chans)} ch @ {rate / 1e3:g} kHz"
        self.status.showMessage(msg + (f" · {note}" if note else ""))

    def _reader(self, bps, stopflag):
        # Short read timeout: a stop request must not wait a second for the reader to
        # notice.  Partial frames stay in self.leftover and resume next call.
        while not stopflag.is_set():
            data = la.read_frame(self.port, self.leftover, timeout=0.1)
            if not data:
                continue
            a = np.frombuffer(la.decode_samples(data, bps), dtype=np.uint32)
            n = len(a)
            with self.lock:
                cap = self.ring_cap
                pos = self.ring_pos
                if n >= cap:
                    self.ring[:] = a[-cap:]
                    pos = 0
                else:
                    end = pos + n
                    if end <= cap:
                        self.ring[pos:end] = a
                    else:
                        k = cap - pos
                        self.ring[pos:] = a[:k]
                        self.ring[:end - cap] = a[k:]
                    pos = end % cap
                self.ring_pos = pos
                self.ring_total += n
                self.n = self.ring_total
                self.frames += 1

    def stop_quiet(self):
        if self.arming:
            if self.port:
                self.port.write(b"\xff")
                self.port.flush()
            self.arming = False
        if not self.running:
            return
        self.stopflag.set()
        if self.thread:
            self.thread.join(timeout=0.6)      # reader wakes on its 0.1 s read timeout
        if self.port:
            try:
                la.stream_stop(self.port)
            except Exception:
                pass
        self.running = False

    def _auto_restart(self, *_):
        """Rate/channels define the stream: restart on change (debounced)."""
        if self.running:
            self.restart_timer.start()

    def _window_changed(self, *_):
        """The window is a *display* parameter: apply it live by growing the history
        ring if needed — never stop/start the stream for a timebase change."""
        try:
            ms = float(self.cb_win.currentText())
        except ValueError:
            return
        if not self.running:
            return
        # 4M samples = 16 MB for the window; the ring caps at 8M (32 MB).
        win_samples = min(max(int(ms / 1000.0 * self.rate), 64), 4_000_000)
        self._grow_ring(2 * win_samples)
        win_samples = min(win_samples, self.ring_cap // 2)
        self.win_samples = max(win_samples, 64)
        self._drawn_total = -1          # force the next tick to redraw
        self._draw_t = 0.0
        shown = self.win_samples / self.rate * 1e3 if self.rate else ms
        self.status.showMessage(
            f"window {shown:.4g} ms" + ("  (memory limit)" if shown < ms * 0.999 else ""))

    def _restart_stream(self):
        if not self.running:
            return
        self.stop_quiet()
        self.start()

    def _wheel_window(self, dy):
        """Wheel = timebase: step through the window presets (up = zoom in)."""
        vals = []
        for i in range(self.cb_win.count()):
            try:
                vals.append(float(self.cb_win.itemText(i)))
            except ValueError:
                pass
        try:
            cur = float(self.cb_win.currentText())
        except ValueError:
            return
        if cur not in vals:
            vals.append(cur)
            vals.sort()
        i = vals.index(cur) + (-1 if dy > 0 else 1)
        if not (0 <= i < len(vals)) or vals[i] == cur:
            return
        self.cb_win.setCurrentText(f"{vals[i]:g}")
        self._window_changed()
        if not self.running:
            self.status.showMessage(f"window {vals[i]:g} ms")

    def stop(self):
        self.auto_single = False
        if self.btn_collect.isChecked():
            self.btn_collect.setChecked(False)
        if self.arming:
            if self.port:
                self.port.write(b"\xff")
                self.port.flush()
            self.arming = False
            self._set_state("stopped")
            self.status.showMessage("single-shot cancelled")
            return
        if self.running:
            self.stop_quiet()
            self._set_state("stopped")
            self.status.showMessage("stopped")

    # ------------------------------------------------------------ one-shot
    def one_shot(self):
        if self.port is None:
            self.status.showMessage(self.no_probe or "no probe")
            return
        if self.arming:                      # press again to cancel the arm
            self.port.write(b"\xff")
            self.port.flush()
            self.arming = False
            self._set_state("stopped")
            self.status.showMessage("single-shot cancelled")
            return
        if self.running:
            self.stop_quiet()
        rate = la.parse_rate(self.cb_rate.currentText())
        trig = self.trig_channel()
        if trig is None:
            self.status.showMessage("single-shot needs a trigger channel")
            return
        pre = max(0, int(self.sb_pre.value() / 1000.0 * rate))
        post = max(1, int(self.sb_post.value() / 1000.0 * rate))
        maxs, pre, post = self._capture_limits()
        if pre + post > maxs and self.fit_btn.isChecked():
            ms_total = (self.sb_pre.value() + self.sb_post.value()) / 1000.0
            old_rate = la.parse_rate(self.cb_rate.currentText())
            rate_fit = max(1000, int(maxs / ms_total)) if ms_total > 0 else old_rate
            rate_fit = max(1000, (rate_fit // 1000) * 1000)
            while rate_fit > 1000 and (
                    int(self.sb_pre.value() / 1000.0 * rate_fit)
                    + int(self.sb_post.value() / 1000.0 * rate_fit) > maxs):
                rate_fit -= 1000
            if rate_fit < old_rate:
                txt = (f"{rate_fit / 1e6:g}M" if rate_fit >= 1e6
                       else f"{rate_fit / 1000:g}k")
                self.cb_rate.setCurrentText(txt)
                rate = rate_fit               # the capture itself uses this
                self._update_maxpost()
                self.status.showMessage(
                    f"auto-fit: rate {old_rate / 1e6:g} MHz → {rate_fit / 1e6:g} MHz "
                    f"to fit the {ms_total * 1e3:g} ms window (untick auto-fit to "
                    f"set the rate yourself)")
            maxs, pre, post = self._capture_limits()
        if pre + post > maxs:
            rate = la.parse_rate(self.cb_rate.currentText())
            max_ms = max(0.0, (maxs - pre) / rate * 1e3)
            msg = (f"The capture window does not fit the probe's buffer.\n\n"
                   f"needed: {pre + post:,} samples "
                   f"({pre:,} pre + {post:,} post)\n"
                   f"buffer: {maxs:,} samples at this pin selection "
                   f"({len(self.chans)} ch, {2 * len(self.chans) if len(self.chans) > 16 else (2 if len(self.chans) > 8 else 1)} B/sample)\n\n"
                   f"Fits if you: post ≤ {max_ms:,.1f} ms, or a lower rate, or fewer pins.\n"
                   f"(A 200 ms window fits at ≤ ~980 kHz here; with only "
                   f"GP2-GP9 (8-bit mode) it fits up to ~1.9 MHz.)")
            QtWidgets.QMessageBox.warning(self, "Single-shot: window too long", msg)
            self.status.showMessage(
                f"single: {pre + post:,} samples > buffer {maxs:,} — post ≤ "
                f"{max_ms:,.1f} ms (see the dialog)")
            return
        self.arming = True
        self.auto_single = True
        self._set_state("armed", f"ch{trig}/GP{la.chan_pin(trig)} "
                                 f"{self.cb_edge.currentText()} @ {rate / 1e6:g} MHz")
        self.status.showMessage(
            f"single-shot armed: ch{trig} rising @ {rate / 1e6:g} MHz … waiting "
            f"(Stop cancels)")


        def worker():
            res = la.la_capture(self.port, self.chans, rate, pre, post,
                                trigger_type=0, trigger=trig,
                                inverted=1 if self.cb_edge.currentText() == "falling" else 0,
                                head_timeout=15.0, drain=0.2)
            if not res["ok"] and self.port:
                self.port.write(b"\xff")
                self.port.flush()
                self.port.drain(0.5)
            self.oneshot_done.emit((res, rate, pre))

        threading.Thread(target=worker, daemon=True).start()

    def _show_oneshot(self, args):
        res, rate, pre = args
        self.arming = False
        if not res.get("ok"):
            self._set_state("stopped")
            self.auto_single = False
            t = self.cb_trig.currentData()
            where = f"ch{la.CHAN_OF_PIN[t]}/GP{t}" if t is not None else "?"
            self.status.showMessage(
                f"single-shot failed: {res.get('response')} — no "
                f"{self.cb_edge.currentText()} edge on {where}? check the pin/edge, "
                f"then arm again")
            return
        samples = res["samples"]
        x = (np.arange(res["count"]) - pre) / rate * 1e3
        self.view, self.view_x = samples, x
        self.aligned = True
        for a in self.data_actions:
            a.setEnabled(True)
        self.rate = rate
        self.metrics = la.quick_metrics(np.asarray(samples, dtype=np.uint32),
                                        rate, self.chans,
                                        list(range(len(self.chans))), np)
        self._draw(view=samples, x=x, bits=list(range(len(self.chans))))
        trig = self.trig_channel()
        shown = True
        if trig is not None and trig in self.chans:
            m = bounce_metrics(samples, rate, self.chans.index(trig), pre)
            if m:
                self._handle_measure(m, trig)
        self._set_state("single", f"{res['count']} samples @ {rate / 1e6:g} MHz")
        self.status.showMessage(
            f"single-shot: {res['count']} samples @ {rate / 1e6:g} MHz "
            f"(pre {pre / rate * 1e3:.3g} ms) — "
            + ("re-arming for the next trigger (Stop ends it)"
               if self.auto_single else "⚡ Single re-arms"))
        if self.auto_single:
            QtCore.QTimer.singleShot(250, self.one_shot)

    # ------------------------------------------------------------- drawing
    def tick(self):
        if not self.running or self.hold:
            return
        with self.lock:
            pos, total = self.ring_pos, self.ring_total
            n, frames = self.n, self.frames
        have = min(total, self.ring_cap)
        if not have:
            return
        now = time.monotonic()
        if self.rate_t and now - self.rate_t > 1.0:
            self.rate_meas = (n - self.rate_n) / (now - self.rate_t)
            self.rate_t, self.rate_n = now, n
        elif not self.rate_t:
            self.rate_t, self.rate_n = now, n
        # The detector only ever sees NEW samples. Feeding it the whole ring every
        # tick was O(ring) per tick and starved the UI thread on long windows.
        self._feed_detector(n)
        # Replot at most ~7x/s, and only when samples arrived. Rebuilding every
        # item 12.5x/s is what produced the "(Not Responding)" freeze.
        if total == self._drawn_total or now - self._draw_t < 0.14:
            return
        self._draw_t = now
        self._drawn_total = total
        w = min(self.win_samples, have)
        span = min(have, 2 * w)              # window + margin for trigger alignment
        start = (pos - span) % self.ring_cap
        if start + span <= self.ring_cap:
            samples = self.ring[start:start + span].copy()
        else:
            samples = np.concatenate((self.ring[start:],
                                      self.ring[:(start + span) % self.ring_cap]))

        trig = self.trig_channel()
        lead = w // 2
        aligned = False
        view = None
        burst_live = False
        if self.detector is not None and trig is not None:
            det = self.detector
            base = n - len(samples)
            pre_abs = max(0, int(self.sb_pre.value() / 1000.0 * self.rate))
            if det.active:
                # scope-normal: hold on the FIRST edge of the burst and let the
                # aftermath grow in place (0 = trigger)
                a0 = max(0, det.first_abs - pre_abs)
                view0 = self._ring_slice(a0, n)
                if view0 is not None and len(view0):
                    view = view0
                    x = (np.arange(len(view)) - (det.first_abs - a0)) / self.rate * 1e3
                    aligned = True
                    burst_live = True
                    mm = bounce_metrics(view, self.rate, trig, det.first_abs - a0)
                    if mm:
                        self.metrics = mm
                        self.annot_text = (
                            f"live · {mm['edges']} edges · {mm['bounces']} bounce · "
                            f"settle {mm['settle_ms']:.3f} ms (growing…)")
            elif self.held_view is not None:
                view, x, aligned = self.held_view, self.held_x, True
                if self.held_metrics:
                    self.metrics = self.held_metrics
        if view is None:
            if trig is not None and len(samples) >= w + 2:
                bits = ((samples >> trig) & 1).astype(np.int8)
                want = 1 if self.cb_edge.currentText() == "rising" else -1
                idxs = np.nonzero(np.diff(bits) == want)[0] + 1
                idxs = idxs[(idxs >= lead) & (idxs <= len(samples) - (w - lead))]
                if len(idxs):
                    t = int(idxs[-1])
                    view = samples[t - lead:t - lead + w]
                    x = (np.arange(len(view)) - lead) / self.rate * 1e3
                    aligned = True
            if view is None:
                view = samples[-w:]              # free-run: the latest window
                x = (np.arange(len(view)) - len(view) + 1) / self.rate * 1e3
        self.view, self.view_x, self.aligned = view, x, aligned
        for a in self.data_actions:
            a.setEnabled(True)
        if not burst_live:
            mstride = max(1, len(view) // 50000)   # metrics on <=50k samples
            mv = np.asarray(view)[::mstride]
            self.metrics = la.quick_metrics(mv, self.rate / mstride,
                                            self.chans, self.chans, np)
        self._draw(view=view, x=x, bits=self.chans)
        self._set_state("running", f"{self.rate_meas / 1e3:.0f} kHz")
        if self.detector is not None:
            if self.detector.active:
                disp = "capturing burst…"
            elif self.held_view is not None:
                disp = (f"showing trigger #{len(self.results)} (holding until the "
                        f"next edge)")
            else:
                disp = "collect: live — waiting for the first edge"
        else:
            disp = "trigger-aligned" if aligned else "free-run"
        self.status.showMessage(
            f"{self.rate_meas / 1e3:.1f} kHz measured · {frames} frames · "
            f"{n} samples · {disp}")

    def _draw(self, view, x, bits):
        n = len(self.chans)
        step = 2.0
        arr = np.asarray(view, dtype=np.uint32)
        key = tuple(self.chans)
        if key != self._curve_key or len(self._curve_items) != n:
            self._build_curves(n, key)
        # The screen shows ~8k points per row at most; decimate the *display* input
        # first so a 24-channel multi-second window stays responsive. The hover
        # measurements below keep the full-rate arrays.
        arr_plot, x_plot = arr, x
        if len(arr) > 400_000:
            d = -(-len(arr) // 400_000)          # ceil
            arr_plot, x_plot = arr[::d], x[::d]
        stride = max(1, len(arr_plot) // 8000)     # keep redraws smooth
        ax = self.plot.getAxis("left")
        ticks = []
        for k, ch in enumerate(self.chans):
            off = (n - 1 - k) * step
            b = (arr_plot >> bits[k]) & 1
            plain, hi_i, lo_i = self._curve_items[k]
            if stride > 1 and len(b) >= stride:
                # long window: envelope per bucket (any-high fills, all-high line),
                # so fast signals show as bands instead of aliasing garbage
                nb = len(b) // stride
                bb = b[:nb * stride].reshape(nb, stride)
                xb = x_plot[:nb * stride:stride]
                hi_i.setData(xb, bb.any(axis=1).astype(np.float32) * 0.8 + off)
                lo_i.setData(xb, bb.all(axis=1).astype(np.float32) * 0.8 + off)
                plain.setVisible(False)
                hi_i.setVisible(True)
                lo_i.setVisible(True)
            else:
                plain.setData(x_plot, b.astype(np.float32) * 0.8 + off)
                plain.setVisible(True)
                hi_i.setVisible(False)
                lo_i.setVisible(False)
            m = self.metrics.get(ch)
            lab = f"GP{la.chan_pin(ch)}"
            if m:
                lab += f"  {m[0]:,.4g} Hz {m[1]:.0f}%"
            ticks.append((off + 0.4, lab))
        ax.setTicks([[(v, s) for v, s in ticks]])
        self.plot.setYRange(-0.5, (n - 1) * step + 1.7)
        if self.annot_text and len(x):
            self.annot.setHtml(f'<span style="font-size:8pt; color:#e6edf3">'
                               f'{self.annot_text}</span>')
            self.annot.setPos(float(x[0]), (n - 1) * step + 1.15)
        else:
            self.annot.setHtml("")
        # what the hover readout needs (full rate; only ONE channel per mouse move)
        self._view_x = x
        self._arr = arr
        self._view_bits = list(bits)
        self._view_serial += 1
        if self.view is not None:
            self._update_table()
        if self._hover_on:
            self._hover_update()

    def _build_curves(self, n, key):
        """Create the per-row plot items once; _draw() then only setData()s them."""
        for items in self._curve_items:
            for it in items:
                self.plot.removeItem(it)
        self._curve_items = []
        step = 2.0
        for k, ch in enumerate(self.chans):
            off = (n - 1 - k) * step
            col = ch_color(ch)
            fill = QtGui.QColor(col)
            fill.setAlpha(55)
            plain = self.plot.plot([], [], pen=pg.mkPen(col, width=1.6), stepMode="left")
            hi = self.plot.plot([], [], pen=pg.mkPen(col, width=1.2), stepMode="left",
                                fillLevel=off, brush=pg.mkBrush(fill))
            lo = self.plot.plot([], [], pen=pg.mkPen(col, width=1.0, alpha=140),
                                stepMode="left")
            for it in (plain, hi, lo):
                it.setVisible(False)
            self._curve_items.append((plain, hi, lo))
        self._curve_key = key

    # ------------------------------------------- measurements (cursors / edge hover)
    def _fmt_t(self, ms):
        a = abs(ms)
        if a < 1e-3:
            return f"{ms * 1e6:.0f} ns"
        if a < 1.0:
            return f"{ms * 1e3:.4g} µs"
        return f"{ms:.5g} ms"

    def _fmt_hz(self, hz):
        a = abs(hz)
        if a >= 1e6:
            return f"{hz / 1e6:.4g} MHz"
        if a >= 1e3:
            return f"{hz / 1e3:.4g} kHz"
        return f"{hz:.4g} Hz"

    def _cursors_toggled(self, on):
        self.cur_a.setVisible(on)
        self.cur_b.setVisible(on)
        self.cur_region.setVisible(on)
        if on:
            x0, x1 = self.plot.viewRange()[0]
            a, b = self.cur_a.value(), self.cur_b.value()
            if x1 > x0 and (a == b or not (x0 <= a <= x1) or not (x0 <= b <= x1)):
                self.cur_a.setValue(x0 + (x1 - x0) * 0.30)
                self.cur_b.setValue(x0 + (x1 - x0) * 0.70)
            self._cursors_changed()
            self.status.showMessage(
                "cursors on — drag A/B, or Shift-click / Ctrl-click the plot")
        else:
            self.status.showMessage("cursors off")
            self._refresh_meas()

    def _cursors_changed(self, *_):
        a, b = self.cur_a.value(), self.cur_b.value()
        lo, hi = (a, b) if a <= b else (b, a)
        self.cur_region.setRegion((lo, hi))
        self._refresh_meas()

    def _plot_clicked(self, ev):
        if ev.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        vb = self.plot.getViewBox()
        if not vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        mods = ev.modifiers()
        set_a = bool(mods & QtCore.Qt.KeyboardModifier.ShiftModifier)
        set_b = bool(mods & QtCore.Qt.KeyboardModifier.ControlModifier)
        if not (set_a or set_b):
            return
        if not self.cursors_btn.isChecked():
            self.cursors_btn.setChecked(True)
        ms = vb.mapSceneToView(ev.scenePos()).x()
        if set_a:
            self.cur_a.setValue(ms)
        if set_b:
            self.cur_b.setValue(ms)
        self._cursors_changed()

    def _refresh_meas(self):
        parts = []
        if self.cursors_btn.isChecked():
            a, b = self.cur_a.value(), self.cur_b.value()
            dt = b - a
            s = (f'<span style="color:#58a6ff">A</span> {self._fmt_t(a)}'
                 f' · <span style="color:#f0883e">B</span> {self._fmt_t(b)}'
                 f' · <b>Δ {self._fmt_t(dt)}</b>')
            if abs(dt) > 1e-9:
                s += f' · {self._fmt_hz(1000.0 / dt)}'
            parts.append(s)
        if self._hover_text:
            parts.append(self._hover_text)
        if not parts:
            parts.append('<span style="color:#8b949e">cursors off — Shift-click = A, '
                         'Ctrl-click = B · hover the plot for edge timings</span>')
        self.meas_lbl.setText("   ".join(parts))

    def _hover_moved(self, scene_pos):
        if self.view is None or len(self.view) < 2:
            return
        vb = self.plot.getViewBox()
        if not vb.sceneBoundingRect().contains(scene_pos):
            self._hover_off()
            return
        p = vb.mapSceneToView(scene_pos)
        self._hover_on = True
        self._hover_ms = p.x()
        self._hover_y = p.y()
        now = time.monotonic()
        if now - self._hover_t < 0.033:      # ~30 Hz is plenty
            return
        self._hover_t = now
        self._hover_update()

    def _hover_off(self):
        if not self._hover_on and not self._hover_text:
            return
        self._hover_on = False
        self._hover_text = ""
        self.hover_line.setVisible(False)
        self.edge_line.setVisible(False)
        self._refresh_meas()

    def _hover_update(self):
        if not (self._hover_on and self.view is not None):
            return
        x = getattr(self, "_view_x", None)
        arr = getattr(self, "_arr", None)
        if x is None or arr is None or not len(x):
            return
        i = int(np.searchsorted(x, self._hover_ms))
        i = max(0, min(i, len(x) - 1))
        if i > 0 and abs(x[i - 1] - self._hover_ms) <= abs(x[i] - self._hover_ms):
            i -= 1
        ms = float(x[i])
        # which row is under the pointer? (nearest row — the gaps between rows are
        # wider than the rows; dropping the channel there made the GP label vanish)
        n = len(self.chans)
        step = 2.0
        k = None
        if n:
            k = int(round(((n - 1) * step + 0.4 - self._hover_y) / step))
            k = max(0, min(n - 1, k))
        txt = f'<span style="color:#8b949e">t</span> {self._fmt_t(ms)}'
        edge_i = None
        if k is not None:
            ch = self.chans[k]
            bit = self._view_bits[k] if k < len(self._view_bits) else ch
            col = ch_color(ch)
            # Local slice around the pointer: cheap even in a multi-second view.
            half = max(2048, min(len(arr), 200_000) // 2)
            lo = max(0, i - half)
            hi = min(len(arr), i + half + 1)
            seg = ((arr[lo:hi] >> bit) & 1)
            edges = np.nonzero(np.diff(seg))[0] + lo + 1
            lvl = "high" if seg[i - lo] else "low"
            txt += f' · <span style="color:{col}">GP{la.chan_pin(ch)} {lvl}</span>'
            if len(edges):
                j = int(np.searchsorted(edges, i))
                prev_i = int(edges[j - 1]) if j >= 1 else None
                next_i = int(edges[j]) if j < len(edges) else None
                near = [e for e in (prev_i, next_i) if e is not None]
                if near:
                    e = min(near, key=lambda q: abs(int(q) - i))
                    if abs(float(x[e]) - ms) <= 0.02 * max(1e-9, float(x[-1] - x[0])):
                        edge_i = int(e)
                if prev_i is None and next_i is not None:
                    txt += f' · next edge in {self._fmt_t(float(x[next_i]) - ms)}'
                if prev_i is not None:
                    txt += f' · last edge {self._fmt_t(ms - float(x[prev_i]))} ago'
                if prev_i is not None and next_i is not None:
                    # width of the level under the pointer: adjacent edges
                    txt += f' · width {self._fmt_t(float(x[next_i] - x[prev_i]))}'
                # Period = two SAME-DIRECTION edges. Adjacent edges alternate
                # direction, so using them would report only half the period.
                per = None
                if j >= 3 and prev_i is not None:
                    per = float(x[prev_i] - x[int(edges[j - 3])])
                elif prev_i is not None and j + 1 < len(edges):
                    per = float(x[int(edges[j + 1])] - x[prev_i])
                elif prev_i is None and len(edges) >= 3:
                    per = float(x[int(edges[2])] - x[int(edges[0])])
                if per and per > 0:
                    txt += f' · period {self._fmt_t(per)} ({self._fmt_hz(1000.0 / per)})'
        if edge_i is not None:
            self.edge_line.setVisible(True)
            self.edge_line.setValue(float(x[edge_i]))
            self.edge_line.setPen(pg.mkPen(ch_color(self.chans[k]), width=1,
                                           style=QtCore.Qt.PenStyle.DotLine))
            self.hover_line.setValue(float(x[edge_i]))
        else:
            self.edge_line.setVisible(False)
            self.hover_line.setValue(ms)
        self.hover_line.setVisible(True)
        self._hover_text = txt
        self._refresh_meas()

    def _update_table(self):
        self.table.setRowCount(len(self.chans))
        for i, ch in enumerate(self.chans):
            m = self.metrics.get(ch)
            vals = [f"GP{la.chan_pin(ch)}", f"{m[0]:,.6g}" if m else "-",
                    f"{m[1]:.1f}" if m else "-", f"ch{ch}"]
            for j, v in enumerate(vals):
                item = self.table.item(i, j)
                if item is None:
                    item = QtWidgets.QTableWidgetItem()
                    self.table.setItem(i, j, item)
                item.setText(v)
                if j == 0:
                    item.setForeground(QtGui.QColor(ch_color(ch)))

    # -------------------------------------------------------------- exports
    def save_png(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save PNG",
                                                        "scope.png", "PNG (*.png)")
        if path:
            self.grab().save(path)
            self.status.showMessage(f"wrote {path}")

    def save_csv(self):
        if self.view is None:
            self.status.showMessage("nothing captured yet")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save CSV",
                                                        "scope.csv", "CSV (*.csv)")
        if path:
            la.write_csv(path, self.view, self.chans,
                         self.chans if self.aligned else list(range(len(self.chans))),
                         self.rate)
            self.status.showMessage(f"wrote {path}")

    def save_vcd(self):
        if self.view is None:
            self.status.showMessage("nothing captured yet")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save VCD",
                                                        "scope.vcd", "VCD (*.vcd)")
        if path:
            la.write_vcd(path, self.view, self.chans,
                         self.chans if self.aligned else list(range(len(self.chans))),
                         self.rate)
            self.status.showMessage(f"wrote {path}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--grab", metavar="PNG", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    os.environ.pop("QT_STYLE_OVERRIDE", None)   # we use Fusion explicitly
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor("#0d1117"))
    pal.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor("#c9d1d9"))
    pal.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor("#11161c"))
    pal.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor("#161c24"))
    pal.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor("#c9d1d9"))
    pal.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor("#1b232d"))
    pal.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor("#c9d1d9"))
    pal.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor("#2f81f7"))
    pal.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("#ffffff"))
    pal.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor("#1b232d"))
    pal.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor("#c9d1d9"))
    app.setPalette(pal)

    win = ScopeWindow()
    if args.smoke:
        win.persist = False        # don't overwrite the user's saved settings
        win.pins.set_selection([16, 17])
        for i in range(win.cb_trig.count()):
            if win.cb_trig.itemData(i) == 16:
                win.cb_trig.setCurrentIndex(i)
        win.fit_btn.setChecked(True)
        win.sb_pre.setValue(0.5)
        win.sb_post.setValue(20.0)
        win._pins_changed()
    win.show()

    if args.smoke:
        result = {"ok": False}

        def grab(name):
            if args.grab:
                path = args.grab if name == "one" else args.grab.replace(".png", f"_{name}.png")
                win.grab().save(path)
                print(f"grab: {path}", flush=True)

        def phase_stream():
            for i in range(win.cb_trig.count()):
                if win.cb_trig.itemData(i) == 16:
                    win.cb_trig.setCurrentIndex(i)
            win.cb_rate.setCurrentText("2M")
            if win.port is None:
                print(f"smoke0: no probe — synthetic phases only "
                      f"(device='{win.dev_lbl.text()}')", flush=True)
                QtCore.QTimer.singleShot(200, after_single)
                return
            win.start()
            QtCore.QTimer.singleShot(3000, after_stream)

        def after_stream():
            n = win.n
            m = win.metrics.get(14)
            print(f"smoke1: running={win.running}, {n} samples, "
                  f"rate={win.rate_meas / 1e3:.1f} kHz, aligned={win.aligned}, "
                  f"view={0 if win.view is None else len(win.view)}, "
                  f"ch14 metric={m}", flush=True)
            grab("stream")
            win.stop_quiet()
            win.cb_win.setCurrentText("2000")
            win.start()
            QtCore.QTimer.singleShot(2500, after_bigwindow)

        def after_bigwindow():
            v = [] if win.view is None else win.view
            print(f"smoke1b: window=2000 ms -> view={len(v)} samples "
                  f"({len(v) / win.rate * 1e3:.0f} ms at {win.rate / 1e3:g} kHz), "
                  f"ch14 metric={win.metrics.get(14)}", flush=True)
            grab("bigwindow")
            win.stop_quiet()
            win.one_shot()
            QtCore.QTimer.singleShot(700, lambda: grab("armed"))
            QtCore.QTimer.singleShot(6000, after_single)

        def after_single():
            v = [] if win.view is None else win.view
            m = win.metrics.get(14)
            print(f"smoke2: one-shot view={len(v)}, ch14 metric={m}, "
                  f"status='{win.status.currentMessage()}'", flush=True)
            grab("one")

            # phase 3: synthetic bounce captures -> results tab + export
            def synth(widths, pre=100, tail=300):
                s = [0] * pre
                val = 1
                for w in widths:
                    s += [val] * (w * 10)
                    val ^= 1
                s += [val] * tail
                return s

            win.stop()
            win.btn_collect.setChecked(False)
            win.clear_results()
            for widths in ([3, 5, 4], [2, 2, 6], [4]):
                samples = synth(widths)
                win._show_oneshot(({"ok": True, "count": len(samples),
                                    "samples": samples, "width": 2},
                                   1_000_000, 100))
            n = win.export_results("/tmp/opencode/bounce_results.csv")
            print(f"smoke3: captures={len(win.results)} exported={n}", flush=True)
            if win.results:
                r = win.results[0]
                print("smoke3 row1: " + str({k: (round(v, 4) if isinstance(v, float)
                                                 else v) for k, v in r.items()}),
                      flush=True)
            print(f"smoke3 summary: {win.summary_lbl.text()}", flush=True)
            win.tabs.setCurrentIndex(1)
            win._draw(view=win.view, x=win.view_x,
                      bits=list(range(len(win.chans)))) if False else None
            grab("results")
            result["ok"] = True
            app.quit()

        QtCore.QTimer.singleShot(300, phase_stream)

    app.exec()
    sys.exit(0 if not args.smoke or result["ok"] else 1)


if __name__ == "__main__":
    main()
