import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, "src")

from PyQt6 import QtWidgets, QtCore
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
