import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, "src")

from PyQt6 import QtWidgets, QtCore
import numpy as np
from PyQt6.QtTest import QTest
from picogremlin_la.gui import ScopeWindow

app = QtWidgets.QApplication([])
win = ScopeWindow()
win.persist = False
win.show()

# --- wheel = timebase presets, clamped at both ends
win.cb_win.setCurrentText("10")
win._wheel_window(120)
print("wheel up   :", win.cb_win.currentText())
assert win.cb_win.currentText() == "5", win.cb_win.currentText()
win._wheel_window(-120)
print("wheel down :", win.cb_win.currentText())
assert win.cb_win.currentText() == "10", win.cb_win.currentText()
win.cb_win.setCurrentText("0.5")
win._wheel_window(120)
print("clamp min  :", win.cb_win.currentText())
assert win.cb_win.currentText() == "0.5"
win.cb_win.setCurrentText("5000")
win._wheel_window(-120)
print("clamp max  :", win.cb_win.currentText())
assert win.cb_win.currentText() == "5000"

# --- auto-restart: three rapid changes coalesce into ONE stop/start
calls = []
win.running = True
win.stop_quiet = lambda: (calls.append("stop"), setattr(win, "running", False))
win.start = lambda: (calls.append("start"), setattr(win, "running", True))
win._auto_restart()
win._auto_restart()
win._auto_restart()
QTest.qWait(450)
print("restart    :", calls)
assert calls == ["stop", "start"], calls

# --- window growth is live: the ring grows, nothing restarts
win.rate = 100_000
win.ring = np.zeros(2048, dtype=np.uint32)
win.ring_cap, win.ring_total, win.ring_pos = 2048, 0, 0
win.running = True
calls.clear()
win.cb_win.setCurrentText("100")          # 100 ms @ 100k = 10k samples -> need 20k
win._window_changed()
print("grow ring  :", win.ring_cap, "win_samples:", win.win_samples, "restarts:", calls)
assert win.ring_cap >= 20000, win.ring_cap
assert win.win_samples == 10000, win.win_samples
assert calls == [], calls
# shrink stays live too
win.cb_win.setCurrentText("5")
win._window_changed()
print("shrink ring:", win.ring_cap, "win_samples:", win.win_samples, "restarts:", calls)
assert win.win_samples == 500 and calls == []

# --- data-loss detection: the sample stream has no timestamps, so a slower
#     received rate than requested means the probe buffer overran (compressed time)
win.rate, win.rate_meas = 100_000, 96_000
win._check_loss()
assert not win.loss, "96% is within tolerance"
win.rate_meas = 60_000
win._check_loss()
print("loss       :", win.loss, "|", win.meas_lbl.text()[:40])
assert win.loss
assert "data loss" in win.meas_lbl.text().lower()
win.rate_meas = 0
win._check_loss()
assert not win.loss

# --- single-shot pre/post scale with the timebase (same ratio)
win.running = False
win._win_ms = 500.0
win.sb_pre.setValue(0.5)
win.sb_post.setValue(4.9)
win.cb_win.setCurrentText("5")
win._window_changed()
print("pre/post @5ms  : %.4f / %.4f ms" % (win.sb_pre.value(), win.sb_post.value()))
assert abs(win.sb_pre.value() + win.sb_post.value() - 5.0) < 1e-3
assert abs(win.sb_pre.value() / 5.0 - 0.5 / 5.4) < 1e-4, win.sb_pre.value()
win.cb_win.setCurrentText("500")
win._window_changed()
print("pre/post @500ms: %.4f / %.4f ms" % (win.sb_pre.value(), win.sb_post.value()))
assert abs(win.sb_pre.value() + win.sb_post.value() - 500.0) < 1e-3
assert abs(win.sb_pre.value() / 500.0 - 0.5 / 5.4) < 1e-4, win.sb_pre.value()

# --- and nothing restarts while stopped
calls.clear()
win.running = False
win._auto_restart()
QTest.qWait(450)
print("stopped    :", calls)
assert calls == [], calls

print("OK")
win.persist = False
win.close()
app.quit()
