"""Analog scope tab for the PicoGremlin LA GUI.

One-shot + continuous captures of the three ADC inputs (GP26..GP28) and the
on-chip temperature sensor, drawn as voltage traces with per-channel
measurements and a PASS/FAIL expectation check for lab work.
"""
import csv
import os
import threading
import time

from picogremlin_la import cli as la

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
except ImportError:                                   # pragma: no cover
    from PySide6 import QtCore, QtGui, QtWidgets      # type: ignore

import pyqtgraph as pg
import numpy as np

CHANNEL_NAMES = {0: "A0/GP26", 1: "A1/GP27", 2: "A2/GP28", 3: "temp"}
COLORS = {0: "#2f81f7", 1: "#f78166", 2: "#3fb950", 3: "#d29922"}


class AnalogTab(QtWidgets.QWidget):
    """Analog capture/stream tab; the parent window owns the serial port."""

    capture_done = QtCore.pyqtSignal(object)

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.values = []                 # interleaved raw values
        self.channels = []
        self.rate = 0
        self.frames = 0
        self.streaming = False
        self._stream_buf = bytearray()
        self._stream_vals = []
        self._stream_t0 = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._poll_stream)
        self.capture_done.connect(self._show_capture)
        self._build()

    # ------------------------------------------------------------- UI
    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)

        row = QtWidgets.QHBoxLayout()
        self.cb = {}
        for ch in range(4):
            cb = QtWidgets.QCheckBox(CHANNEL_NAMES[ch])
            cb.setChecked(ch == 0)
            cb.setStyleSheet(f"color: {COLORS[ch]};")
            row.addWidget(cb)
            self.cb[ch] = cb
        row.addSpacing(12)
        row.addWidget(QtWidgets.QLabel("rate"))
        self.rate_cb = QtWidgets.QComboBox()
        self.rate_cb.setEditable(True)
        for r in ("10k", "50k", "100k", "200k", "500k"):
            self.rate_cb.addItem(r)
        self.rate_cb.setCurrentText("100k")
        self.rate_cb.setMaximumWidth(90)
        row.addWidget(self.rate_cb)
        row.addWidget(QtWidgets.QLabel("samples/ch"))
        self.samples_sb = QtWidgets.QSpinBox()
        self.samples_sb.setRange(16, 100000)
        self.samples_sb.setValue(4000)
        self.samples_sb.setSingleStep(1000)
        row.addWidget(self.samples_sb)
        row.addSpacing(12)
        self.trig_cb = QtWidgets.QCheckBox("trigger")
        row.addWidget(self.trig_cb)
        self.trig_ch = QtWidgets.QComboBox()
        for ch in range(3):
            self.trig_ch.addItem(CHANNEL_NAMES[ch], ch)
        row.addWidget(self.trig_ch)
        self.level_edit = QtWidgets.QLineEdit("1.65V")
        self.level_edit.setMaximumWidth(80)
        row.addWidget(self.level_edit)
        self.falling_cb = QtWidgets.QCheckBox("falling")
        row.addWidget(self.falling_cb)
        row.addStretch(1)

        self.btn_single = QtWidgets.QPushButton("⚡ Single")
        self.btn_stream = QtWidgets.QPushButton("▶ Stream")
        self.btn_stream.setCheckable(True)
        self.btn_save = QtWidgets.QPushButton("Save…")
        self.btn_csv = QtWidgets.QPushButton("CSV")
        for b in (self.btn_single, self.btn_stream, self.btn_save, self.btn_csv):
            row.addWidget(b)
        layout.addLayout(row)

        row2 = QtWidgets.QHBoxLayout()
        self.use_8bit = QtWidgets.QCheckBox("8-bit (more headroom)")
        row2.addWidget(self.use_8bit)
        row2.addSpacing(16)
        row2.addWidget(QtWidgets.QLabel("check mean"))
        self.chk_mean = QtWidgets.QLineEdit("1.65V±5%")
        self.chk_mean.setMaximumWidth(110)
        row2.addWidget(self.chk_mean)
        row2.addWidget(QtWidgets.QLabel("freq"))
        self.chk_freq = QtWidgets.QLineEdit("")
        self.chk_freq.setMaximumWidth(110)
        row2.addWidget(self.chk_freq)
        self.btn_check = QtWidgets.QPushButton("Check")
        row2.addWidget(self.btn_check)
        self.check_lbl = QtWidgets.QLabel("")
        row2.addWidget(self.check_lbl)
        row2.addStretch(1)
        layout.addLayout(row2)

        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "time", units="ms")
        self.plot.setLabel("left", "volts", units="V")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.addLegend(offset=(10, 10))
        layout.addWidget(self.plot, 3)

        self.table = QtWidgets.QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["input", "mean", "min", "max", "pp", "rms", "freq", "duty"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        self.btn_single.clicked.connect(self.single)
        self.btn_stream.toggled.connect(self._toggle_stream)
        self.btn_save.clicked.connect(self.save_png)
        self.btn_csv.clicked.connect(self.save_csv)
        self.btn_check.clicked.connect(self.check)

    # ---------------------------------------------------------- helpers
    def _selected(self):
        return [ch for ch in range(4) if self.cb[ch].isChecked()]

    def _port(self):
        return getattr(self.win, "port", None)

    def _set_status(self, text):
        if hasattr(self.win, "status"):
            self.win.status.showMessage(text)

    # ---------------------------------------------------------- single shot
    def single(self):
        port = self._port()
        if port is None:
            self._set_status("analog: no probe")
            return
        chans = self._selected()
        if not chans:
            self._set_status("analog: select at least one input")
            return
        rate = la.parse_rate(self.rate_cb.currentText())
        post = self.samples_sb.value()
        trig = None
        level = 0
        if self.trig_cb.isChecked():
            trig = self.trig_ch.currentData()
            level = la.parse_volts(self.level_edit.text() or "0")
        eight = self.use_8bit.isChecked()

        def worker():
            res = la.la_analog_capture(port, chans, rate, 0, post,
                                       eight_bit=eight, trigger=trig,
                                       level=level,
                                       inverted=self.falling_cb.isChecked(),
                                       head_timeout=5.0)
            if not res["ok"] and port:
                port.write(b"\xff")
                port.flush()
                port.drain(0.5)
            self.capture_done.emit((res, chans, rate))

        self._set_status("analog: capturing…")
        threading.Thread(target=worker, daemon=True).start()

    def _show_capture(self, args):
        res, chans, rate = args
        if not res.get("ok"):
            self._set_status(f"analog capture failed: {res.get('response')}")
            return
        self._render(res["values"], chans, res["rate"])
        self._set_status(f"analog: {len(res['values']) // len(chans)} samples/ch "
                         f"@ {res['rate'] / 1e3:g} kS/s")

    def _render(self, values, chans, rate):
        self.values = list(values)
        self.channels = list(chans)
        self.rate = rate
        per = la.analog_channel_values(self.values, len(chans))
        t = np.arange(len(per[0])) / rate * 1e3
        self.plot.clear()
        for k, ch in enumerate(chans):
            y = np.array([la.adc_temp_c(v) if ch == 3 else la.adc_volts(v)
                          for v in per[k]])
            self.plot.plot(t, y, pen=pg.mkPen(COLORS[ch], width=1.4),
                           name=CHANNEL_NAMES[ch])
        if chans == [3]:
            self.plot.setLabel("left", "°C")
        else:
            self.plot.setLabel("left", "volts", units="V")
        self._fill_table(per, chans, rate)

    def _fill_table(self, per, chans, rate):
        self.table.setRowCount(len(chans))
        for k, ch in enumerate(chans):
            m = la.measure_analog(per[k], rate)
            cells = [
                CHANNEL_NAMES[ch],
                f"{la.adc_temp_c(m['mean']):.1f} °C" if ch == 3 else f"{la.adc_volts(m['mean']):.3f} V",
                f"{m['min']:.0f}" if ch == 3 else f"{la.adc_volts(m['min']):.3f} V",
                f"{m['max']:.0f}" if ch == 3 else f"{la.adc_volts(m['max']):.3f} V",
                f"{m['pp']:.0f}" if ch == 3 else f"{la.adc_volts(m['pp']):.3f} V",
                f"{m['rms']:.0f}" if ch == 3 else f"{la.adc_volts(m['rms']):.3f} V",
                f"{m['freq']:.4g} Hz" if m["freq"] else "—",
                f"{m['duty']:.1f} %" if m["freq"] else "—",
            ]
            for col, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(k, col, item)

    # ---------------------------------------------------------- stream
    def _toggle_stream(self, on):
        if on:
            self._start_stream()
        else:
            self._stop_stream()

    def _start_stream(self):
        if getattr(self.win, "running", False):
            self.win.stop_quiet()          # both streams share the LA CDC
        port = self._port()
        chans = self._selected()
        if port is None or not chans:
            self.btn_stream.setChecked(False)
            self._set_status("analog: no probe / no input selected")
            return
        rate = la.parse_rate(self.rate_cb.currentText())
        bps = 1 if self.use_8bit.isChecked() else 2
        limit = la.MAX_STREAM_BYTES // (bps * len(chans))
        rate = min(rate, limit)
        half = max(64, min(8192, rate // 20))
        started, leftover, rate_rep = la.analog_stream_start(
            port, chans, rate, half, eight_bit=self.use_8bit.isChecked())
        if not started:
            self.btn_stream.setChecked(False)
            self._set_status("analog: stream did not start")
            return
        self._stream_buf = leftover
        self._stream_vals = []
        self._stream_chans = chans
        self._stream_rate = rate_rep
        self._stream_t0 = time.monotonic()
        self.frames = 0
        self.streaming = True
        self.btn_stream.setText("■ Stop")
        self._timer.start(100)
        self._set_status(f"analog: streaming {len(chans)} ch @ {rate_rep / 1e3:g} kS/s")

    def _stop_stream(self):
        self._timer.stop()
        self.streaming = False
        self.btn_stream.setText("▶ Stream")
        port = self._port()
        if port is not None:
            try:
                la.stream_stop(port)
            except Exception:
                pass
        if self._stream_vals:
            self._render(self._stream_vals, self._stream_chans, self._stream_rate)
            self._set_status(f"analog: stopped after {len(self._stream_vals) // len(self._stream_chans)} "
                             f"samples/ch")

    def _poll_stream(self):
        port = self._port()
        if not self.streaming or port is None:
            return
        for _ in range(10):
            data = la.read_frame(port, self._stream_buf, timeout=0.05)
            if data is None:
                break
            self.frames += 1
            if self.use_8bit.isChecked():
                self._stream_vals.extend(data)
            else:
                self._stream_vals.extend(
                    np.frombuffer(data, dtype="<u2").tolist())
        # keep the live view bounded (~20 s of data)
        cap = 4 * self._stream_rate * 20
        if len(self._stream_vals) > cap:
            del self._stream_vals[:len(self._stream_vals) - cap]
        if self._stream_vals:
            self._render(self._stream_vals, self._stream_chans, self._stream_rate)

    # ---------------------------------------------------------- check
    def check(self):
        if not self.values:
            self._set_status("analog: capture first")
            return
        ch = self.channels[0]
        per = la.analog_channel_values(self.values, len(self.channels))[0]
        m = la.measure_analog(per, self.rate)
        parts = []
        ok_all = True
        try:
            if self.chk_mean.text().strip():
                exp, tol = la.parse_expect(self.chk_mean.text())
                got = la.adc_temp_c(m["mean"]) if ch == 3 else la.adc_volts(m["mean"])
                tol = tol if tol is not None else abs(exp) * 0.01
                ok = abs(got - exp) <= tol
                ok_all &= ok
                parts.append(f"{'PASS' if ok else 'FAIL'} mean {got:.3g} (want {exp:.3g}±{tol:.2g})")
            if self.chk_freq.text().strip():
                exp, tol = la.parse_expect(self.chk_freq.text())
                tol = tol if tol is not None else abs(exp) * 0.01
                ok = abs(m["freq"] - exp) <= tol
                ok_all &= ok
                parts.append(f"{'PASS' if ok else 'FAIL'} freq {m['freq']:.4g} Hz "
                             f"(want {exp:.4g}±{tol:.3g})")
        except Exception as exc:                       # bad spec typed by the user
            self.check_lbl.setText(f"bad expectation: {exc}")
            return
        self.check_lbl.setText("  ·  ".join(parts) or "—")
        self.check_lbl.setStyleSheet("color: #3fb950;" if ok_all else "color: #f85149;")

    # ---------------------------------------------------------- export
    def save_png(self):
        if not self.values:
            self._set_status("analog: nothing to save")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save analog plot", "analog.png", "PNG (*.png)")
        if path:
            self.plot.grab().save(path)
            self._set_status(f"wrote {path}")

    def save_csv(self):
        if not self.values:
            self._set_status("analog: nothing to save")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save analog CSV", "analog.csv", "CSV (*.csv)")
        if path:
            la.write_analog_csv(path, self.values, self.channels, self.rate)
            self._set_status(f"wrote {path}")
