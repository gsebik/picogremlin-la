import os, re, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, "src")

from PyQt6 import QtWidgets
import numpy as np
from picogremlin_la.gui import ScopeWindow

app = QtWidgets.QApplication([])
win = ScopeWindow()
win.persist = False
win.show()
win.chans = [14, 15]

# Synthetic: GP16 toggles every 10 samples at 1 MHz -> 20 us period, 50 kHz.
# GP17 is the inverse, so it has edges too.
n, rate = 2000, 1_000_000
bits = ((np.arange(n) // 10) & 1).astype(np.uint32)
samples = (bits << 0) | ((bits ^ 1) << 1)
x = (np.arange(n) - 500) / rate * 1e3          # ms, edge at t=0
win.view, win.view_x = samples, x
win._draw(view=samples, x=x, bits=[0, 1])

def label():
    return re.sub(r"<[^>]+>", "", win.meas_lbl.text())

# --- cursors: A=0 ms, B=0.2 ms -> 200 us, 5 kHz
win.cursors_btn.setChecked(True)
win.cur_a.setValue(0.0)
win.cur_b.setValue(0.2)
win._cursors_changed()
t1 = label()
print("cursor:", t1)
assert "200 µs" in t1, t1
assert "5 kHz" in t1, t1

# --- hover just after the edge at 0.150 ms, on the GP16 row
win._hover_on = True
win._hover_ms = 0.151
win._hover_y = (len(win.chans) - 1 - 0) * 2.0 + 0.4      # row centre for k=0
win._hover_update()
t2 = label()
print("hover :", t2)
assert "GP16" in t2, t2
assert "period 20 µs" in t2, t2
assert "50 kHz" in t2, t2
assert "width 10 µs" in t2, t2
assert "last edge" in t2, t2

# --- hover on GP17 (row 1) -> inverse phase, still edges
win._hover_ms = 0.1505
win._hover_y = (len(win.chans) - 1 - 1) * 2.0 + 0.4
win._hover_update()
t3 = label()
print("hover2:", t3)
assert "GP17" in t3, t3

# --- a view with no edges nearby must not claim a period
win._hover_ms = 5.0        # outside the array: clamps to the last sample
win._hover_update()
print("clamp :", label())

print("OK")
win.persist = False
win.close()
app.quit()
