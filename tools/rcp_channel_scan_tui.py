#!/usr/bin/env python3
"""Terminal (curses) viewer for the 2.4 GHz BLE channel energy scan.

A btop-style text UI. It needs only Python 3 and pyserial, works over SSH, and
shows per-channel energy (min-max band + average), busy %, a Wi-Fi 1/6/11 overlay
and a scrolling waterfall.

Sources:
  rcp     bt_rcp_channel_scan_freertos over raw H4 HCI (vendor commands
          0xFF00-0xFF03). The TUI can start/stop the scan and change parameters.
  chscan  "CHSCAN,..." CSV lines from a serial port, e.g. a device or logger
          that prints the format of `rcp_channel_scan.py watch --chscan` (view only).
  demo    synthetic data, no hardware needed.

Examples:
  rcp_channel_scan_tui.py -p /dev/ttyACM0
  rcp_channel_scan_tui.py --source chscan -p /dev/tty.usbmodem0004403265481
  rcp_channel_scan_tui.py --source demo

Keys:
  space start/stop scan (rcp)     +/- passes per report (rcp)
  [ ]   averaging time (rcp)      i   cycle report interval (rcp)
  t/T   busy threshold -/+ 1 dB   left/right  select channel
  v     waterfall: max/avg/min    w   Wi-Fi overlay on/off
  h     peak hold on/off          c   clear history and peaks
  p     pause display             q   quit
"""
import argparse
import collections
import curses
import math
import queue
import random
import threading
import time

NUM_CHANNELS = 40
INVALID_DBM = -128
DBM_FLOOR = -110
DBM_CEIL = -20
HISTORY = 500
WIFI_CHANNELS = {1: 2412, 6: 2437, 11: 2462}   # 22 MHz wide
INTERVALS_MS = [0, 500, 1000, 5000, 10000, 30000]
AVG_STEPS_US = [32, 64, 128, 256, 512, 1024, 2048]

# xterm-256 heat scale: dark blue -> cyan -> green -> yellow -> red.
HEAT_256 = [17, 18, 19, 20, 26, 32, 38, 44, 43, 49, 48, 84, 120, 156, 192,
            226, 220, 214, 208, 202, 196]
HEAT_8 = [curses.COLOR_BLUE, curses.COLOR_CYAN, curses.COLOR_GREEN,
          curses.COLOR_YELLOW, curses.COLOR_RED]
EIGHTHS = " ▁▂▃▄▅▆▇█"
SHADES = " ░▒▓█"


def rf_to_mhz(rf):
    return 2402 + 2 * rf


def rf_to_ble(rf):
    if rf == 0:
        return 37
    if rf == 12:
        return 38
    if rf == 39:
        return 39
    return rf - 1 if rf < 12 else rf - 2


class Report:
    __slots__ = ("t", "seq", "passes", "stats")

    def __init__(self, seq, passes, stats):
        self.t = time.monotonic()
        self.seq = seq
        self.passes = passes
        self.stats = stats          # list of (min, avg, max, busy)


# --------------------------------------------------------------------------
# Data sources: each runs in a thread and pushes Reports into a queue.
# --------------------------------------------------------------------------
class Source(threading.Thread):
    controllable = False

    def __init__(self, out):
        super().__init__(daemon=True)
        self.out = out
        self.cmds = queue.Queue()
        self.stop_evt = threading.Event()
        self.status = "starting"
        self.error = ""
        self.running = None
        self.params = {}
        self.label = ""

    def command(self, *cmd):
        self.cmds.put(cmd)


class DemoSource(Source):
    controllable = True

    def __init__(self, out):
        super().__init__(out)
        self.label = "demo"
        self.running = True
        self.params = dict(passes=20, averaging_us=128, interval_ms=0, threshold=-71)

    def run(self):
        seq, t0 = 0, time.monotonic()
        while not self.stop_evt.is_set():
            while not self.cmds.empty():
                cmd = self.cmds.get()
                if cmd[0] == "start":
                    self.running = True
                    self.params.update(cmd[1])
                elif cmd[0] == "stop":
                    self.running = False
            self.status = "running" if self.running else "stopped"
            if not self.running:
                time.sleep(0.1)
                continue
            now = time.monotonic() - t0
            stats = []
            for rf in range(NUM_CHANNELS):
                mhz = rf_to_mhz(rf)
                floor = -97 + random.gauss(0, 0.6)
                # Wi-Fi channel 6 with a duty cycle, plus a hopping burst.
                wifi = -55 if abs(mhz - 2437) < 10 and math.sin(now * 0.7) > -0.3 else None
                hop = -45 if rf == int(now * 3) % NUM_CHANNELS else None
                adv = -60 if rf in (0, 12, 39) and random.random() < 0.4 else None
                peak = max([v for v in (wifi, hop, adv) if v is not None], default=floor + 4)
                avg = floor + (8 if wifi else 0) + random.gauss(0, 0.5)
                busy = 0
                if wifi:
                    busy = random.randint(25, 60)
                elif hop or adv:
                    busy = random.randint(5, 15)
                stats.append((int(floor - 2), int(avg), int(peak + random.gauss(0, 2)), busy))
            seq += 1
            self.out.put(Report(seq, self.params["passes"], stats))
            delay = self.params["passes"] * 40 * 0.0007 + self.params["interval_ms"] / 1000
            self.stop_evt.wait(max(0.1, delay))


class ChscanSource(Source):
    """Reads CHSCAN,<ver>,<passes>,<n>,{min,avg,max,busy}xN lines."""

    def __init__(self, out, port, baud):
        super().__init__(out)
        self.port, self.baud = port, baud
        self.label = f"CHSCAN {port}"

    def run(self):
        import serial
        seq = 0
        while not self.stop_evt.is_set():
            try:
                with serial.Serial(self.port, self.baud, timeout=0.5) as ser:
                    self.status, self.error = "receiving", ""
                    buf = b""
                    while not self.stop_evt.is_set():
                        buf += ser.read(ser.in_waiting or 1)
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            f = line.decode(errors="replace").strip().split(",")
                            if f[0] != "CHSCAN" or len(f) < 4:
                                continue
                            try:
                                n = int(f[3])
                                v = list(map(int, f[4:4 + 4 * n]))
                            except ValueError:
                                continue
                            if len(v) != 4 * n:
                                continue
                            seq += 1
                            self.out.put(Report(seq, int(f[2]),
                                                [tuple(v[i * 4:i * 4 + 4]) for i in range(n)]))
            except Exception as e:   # noqa: BLE001  keep the UI alive, retry
                self.status, self.error = "error", str(e)
                self.stop_evt.wait(1.0)


class RcpSource(Source):
    controllable = True

    def __init__(self, out, port, baud, rtscts, poll_ms, reset):
        super().__init__(out)
        self.port, self.baud, self.rtscts = port, baud, rtscts
        self.poll_s = poll_ms / 1000
        self.reset = reset
        self.label = f"RCP {port}"
        self.params = dict(passes=20, averaging_us=128, interval_ms=0, threshold=-71)

    def run(self):
        import rcp_channel_scan as r
        while not self.stop_evt.is_set():
            rcp = None
            try:
                rcp = r.Rcp(self.port, self.baud, self.rtscts, timeout=1.0)
                if self.reset:
                    rcp.reset()
                info = rcp.info()
                self.status, self.error = "connected", ""
                last = None
                while not self.stop_evt.is_set():
                    while not self.cmds.empty():
                        cmd = self.cmds.get()
                        if cmd[0] == "start":
                            self.params.update(cmd[1])
                            p = self.params
                            rcp.start(p["passes"], p["averaging_us"], p["interval_ms"],
                                      p["threshold"])
                        elif cmd[0] == "stop":
                            rcp.stop()
                    info = rcp.info()
                    self.running = info["running"]
                    self.status = "running" if info["running"] else "stopped"
                    seq = info["report_seq"]
                    if seq and seq != last:
                        s, passes, stats = rcp.read_report(info["num_channels"],
                                                           info["max_per_read"])
                        last = s
                        self.out.put(Report(s, passes, stats))
                    self.stop_evt.wait(self.poll_s)
            except Exception as e:   # noqa: BLE001  keep the UI alive, retry
                self.status, self.error = "error", str(e)
                self.stop_evt.wait(1.0)
            finally:
                if rcp is not None:
                    try:
                        rcp.ser.close()
                    except Exception:   # noqa: BLE001
                        pass


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
class Ui:
    def __init__(self, scr, src, args):
        self.scr = scr
        self.src = src
        self.reports = queue.Queue()
        src.out = self.reports
        self.history = collections.deque(maxlen=HISTORY)
        self.latest = None
        self.peak = [INVALID_DBM] * NUM_CHANNELS
        self.peak_hold = True
        self.wifi = True
        self.wf_mode = 0          # 0 max, 1 avg, 2 min
        self.paused = False
        self.sel = None
        self.threshold = args.threshold
        self.times = collections.deque(maxlen=20)
        self.msg = ""
        self.msg_t = 0
        self._init_colors()

    # ---- colors ----
    def _init_colors(self):
        self.color = curses.has_colors()
        self.heat = []
        if not self.color:
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        scale = HEAT_256 if curses.COLORS >= 256 else HEAT_8
        for i, c in enumerate(scale):
            curses.init_pair(10 + i, c, bg)
            self.heat.append(curses.color_pair(10 + i))
        curses.init_pair(1, curses.COLOR_CYAN, bg)      # titles / axes
        curses.init_pair(2, curses.COLOR_RED, bg)       # threshold / errors
        curses.init_pair(3, curses.COLOR_YELLOW, bg)    # selection / wifi
        curses.init_pair(4, curses.COLOR_GREEN, bg)     # running
        curses.init_pair(5, curses.COLOR_MAGENTA, bg)   # adv channels / peak
        self.c_title = curses.color_pair(1) | curses.A_BOLD
        self.c_axis = curses.color_pair(1)
        self.c_red = curses.color_pair(2)
        self.c_yel = curses.color_pair(3)
        self.c_grn = curses.color_pair(4) | curses.A_BOLD
        self.c_mag = curses.color_pair(5)

    def attr(self, name):
        return getattr(self, name, 0) if self.color else 0

    def heat_attr(self, dbm):
        if not self.color or dbm <= INVALID_DBM:
            return curses.A_DIM
        t = (min(max(dbm, DBM_FLOOR), DBM_CEIL) - DBM_FLOOR) / (DBM_CEIL - DBM_FLOOR)
        return self.heat[min(len(self.heat) - 1, int(t * len(self.heat)))]

    def put(self, y, x, s, a=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        s = s[:max(0, w - x)]
        try:
            self.scr.addstr(y, x, s, a)
        except curses.error:
            pass   # writing the bottom-right cell raises; harmless

    def flash(self, msg):
        self.msg, self.msg_t = msg, time.monotonic()

    # ---- input ----
    def handle_key(self, k):
        p = dict(self.src.params)
        ctl = self.src.controllable
        if k in (ord("q"), ord("Q")):
            return False
        if k == ord(" ") and ctl:
            if self.src.running:
                self.src.command("stop")
                self.flash("stop")
            else:
                self.src.command("start", {})
                self.flash("start")
        elif k in (ord("+"), ord("=")) and ctl:
            self._restart(passes=min(1000, p["passes"] * 2 if p["passes"] >= 5 else p["passes"] + 1))
        elif k in (ord("-"), ord("_")) and ctl:
            self._restart(passes=max(1, p["passes"] // 2 if p["passes"] > 5 else p["passes"] - 1))
        elif k in (ord("]"), ord("[")) and ctl:
            i = AVG_STEPS_US.index(p["averaging_us"]) if p["averaging_us"] in AVG_STEPS_US else 2
            i = min(len(AVG_STEPS_US) - 1, i + 1) if k == ord("]") else max(0, i - 1)
            self._restart(averaging_us=AVG_STEPS_US[i])
        elif k == ord("i") and ctl:
            i = INTERVALS_MS.index(p["interval_ms"]) if p["interval_ms"] in INTERVALS_MS else 0
            self._restart(interval_ms=INTERVALS_MS[(i + 1) % len(INTERVALS_MS)])
        elif k in (ord("t"), ord("T")):
            self.threshold += 1 if k == ord("T") else -1
            if ctl:
                self._restart(threshold=self.threshold)
            else:
                self.flash(f"threshold line {self.threshold} dBm (busy % is computed on the device)")
        elif k == curses.KEY_LEFT:
            self.sel = NUM_CHANNELS - 1 if self.sel is None else max(0, self.sel - 1)
        elif k == curses.KEY_RIGHT:
            self.sel = 0 if self.sel is None else min(NUM_CHANNELS - 1, self.sel + 1)
        elif k == 27:
            self.sel = None
        elif k == ord("v"):
            self.wf_mode = (self.wf_mode + 1) % 3
        elif k == ord("w"):
            self.wifi = not self.wifi
        elif k == ord("h"):
            self.peak_hold = not self.peak_hold
        elif k == ord("c"):
            self.history.clear()
            self.peak = [INVALID_DBM] * NUM_CHANNELS
            self.flash("history cleared")
        elif k == ord("p"):
            self.paused = not self.paused
        elif not ctl and k in (ord(" "), ord("+"), ord("-"), ord("["), ord("]"), ord("i")):
            self.flash("this source is view only")
        return True

    def _restart(self, **kw):
        self.src.command("start", kw)
        if "threshold" in kw:
            self.threshold = kw["threshold"]
        self.flash("restart: " + ", ".join(f"{k}={v}" for k, v in kw.items()))

    # ---- data ----
    def pump(self):
        got = False
        while True:
            try:
                rep = self.reports.get_nowait()
            except queue.Empty:
                break
            if self.paused:
                continue
            self.latest = rep
            self.history.appendleft(rep)
            self.times.append(rep.t)
            for rf, (_, _, mx, _) in enumerate(rep.stats[:NUM_CHANNELS]):
                if mx > self.peak[rf]:
                    self.peak[rf] = mx
            got = True
        return got

    def rate(self):
        if len(self.times) < 2 or self.times[-1] == self.times[0]:
            return 0.0
        return (len(self.times) - 1) / (self.times[-1] - self.times[0])

    # ---- drawing ----
    def draw(self):
        s = self.scr
        s.erase()
        h, w = s.getmaxyx()
        if w < 50 or h < 20:
            self.put(0, 0, f"Terminal too small ({w}x{h}); need at least 50x20.")
            s.refresh()
            return
        y_axis_w = 5
        cw = max(1, (w - y_axis_w - 1) // NUM_CHANNELS)
        x0 = y_axis_w
        plot_w = cw * NUM_CHANNELS

        self._draw_header(w)
        # Vertical budget: header 2, energy E, axis 3, busy B+1, wf title 1, wf rest, footer 1.
        avail = h - 2 - 3 - 1 - 1 - 1
        energy_h = max(6, int(avail * 0.45))
        busy_h = max(2, min(6, int(avail * 0.12)))
        y = 2
        self._draw_energy(y, x0, cw, energy_h)
        y += energy_h
        y = self._draw_x_axis(y, x0, cw, plot_w)
        y = self._draw_busy(y, x0, cw, busy_h)
        self._draw_waterfall(y, x0, cw, h - 1 - y, plot_w)
        self._draw_footer(h - 1, w)
        s.refresh()

    def _draw_header(self, w):
        src = self.src
        state = src.status.upper()
        sa = self.attr("c_grn") if src.status in ("running", "receiving") else (
            self.attr("c_red") if src.status == "error" else self.attr("c_yel"))
        self.put(0, 0, " BLE channel scan ", self.attr("c_title") | curses.A_REVERSE)
        x = 19
        self.put(0, x, f" {src.label} ")
        x += len(src.label) + 2
        self.put(0, x, f"[{state}]", sa | curses.A_BOLD)
        x += len(state) + 3
        rep = self.latest
        info = f"rpt #{rep.seq}  {rep.passes} passes  {self.rate():.1f}/s" if rep else "waiting for data"
        if self.paused:
            info += "  PAUSED"
        self.put(0, x, info)
        p = src.params
        if src.controllable and p:
            line = (f" avg {p['averaging_us']} us   interval {p['interval_ms']} ms   "
                    f"busy > {self.threshold} dBm")
        else:
            line = f" busy threshold line {self.threshold} dBm (device-side busy %)"
        self.put(1, 0, line, curses.A_DIM)
        if src.error:
            self.put(1, len(line) + 2, "error: " + src.error, self.attr("c_red"))
        elif self.sel is not None and rep:
            mn, av, mx, b = rep.stats[self.sel]
            d = (f"RF {self.sel} / BLE {rf_to_ble(self.sel)} / {rf_to_mhz(self.sel)} MHz: "
                 f"min {mn} avg {av} max {mx} dBm, busy {b}%, peak {self.peak[self.sel]}")
            self.put(1, max(len(line) + 2, w - len(d) - 1), d, self.attr("c_yel") | curses.A_BOLD)

    def _row_of(self, dbm, top, height):
        """Map dBm to a fractional row position (0 = bottom) in eighths."""
        t = (min(max(dbm, DBM_FLOOR), DBM_CEIL) - DBM_FLOOR) / (DBM_CEIL - DBM_FLOOR)
        return t * height * 8

    def _draw_energy(self, top, x0, cw, height):
        bottom = top + height - 1
        for i in range(height):
            dbm = DBM_CEIL - (DBM_CEIL - DBM_FLOOR) * i / max(1, height - 1)
            if i % 2 == 0 or height < 10:
                self.put(top + i, 0, f"{dbm:4.0f}", self.attr("c_axis"))
            self.put(top + i, x0 - 1, "┤" if i % 2 == 0 else "│", self.attr("c_axis"))
        # Wi-Fi spans and threshold, behind the data.
        if self.wifi:
            for ch, center in WIFI_CHANNELS.items():
                lo = max(0, math.ceil((center - 11 - 2402) / 2))
                hi = min(NUM_CHANNELS - 1, math.floor((center + 11 - 2402) / 2))
                for i in range(1, height):
                    for x in (x0 + lo * cw, x0 + (hi + 1) * cw - 1):
                        self.put(top + i, x, "┊", self.attr("c_yel") | curses.A_DIM)
        th_row = bottom - int(self._row_of(self.threshold, top, height) / 8)
        rep = self.latest
        for rf in range(NUM_CHANNELS):
            x = x0 + rf * cw
            col_w = max(1, cw - 1) if cw >= 3 else cw
            if top < th_row <= bottom:
                self.put(th_row, x, "╌" * cw, self.attr("c_red"))
            if not rep:
                continue
            mn, av, mx, _ = rep.stats[rf]
            if av <= INVALID_DBM:
                self.put(bottom, x, "?" * col_w, curses.A_DIM)
                continue
            r_avg = self._row_of(av, top, height)
            r_min = self._row_of(mn, top, height)
            r_max = self._row_of(mx, top, height)
            sel = curses.A_BOLD if rf == self.sel else 0
            for i in range(height):
                cell_lo, cell_hi = i * 8, i * 8 + 8
                yy = bottom - i
                if r_avg >= cell_hi:
                    ch = "█"
                elif r_avg > cell_lo:
                    ch = EIGHTHS[int(r_avg - cell_lo)]
                    if ch == " ":
                        ch = None
                else:
                    ch = None
                band = r_min < cell_hi and r_max > cell_lo
                if ch:
                    cell_dbm = DBM_FLOOR + (DBM_CEIL - DBM_FLOOR) * min(cell_hi, r_avg) / (height * 8)
                    self.put(yy, x, ch * col_w, self.heat_attr(cell_dbm) | sel)
                elif band:
                    self.put(yy, x, "░" * col_w, self.heat_attr(mx) | sel)
            if self.peak_hold and self.peak[rf] > INVALID_DBM:
                pr = int(self._row_of(self.peak[rf], top, height) / 8)
                py = bottom - min(height - 1, pr)
                if pr * 8 >= r_max - 8:
                    self.put(py, x, "▔" * col_w, self.attr("c_mag"))
        if self.wifi:
            for ch, center in WIFI_CHANNELS.items():
                lo = max(0, math.ceil((center - 11 - 2402) / 2))
                hi = min(NUM_CHANNELS - 1, math.floor((center + 11 - 2402) / 2))
                label = f"Wi-Fi {ch}" if (hi - lo + 1) * cw >= 9 else f"W{ch}"
                lx = x0 + lo * cw + max(0, ((hi - lo + 1) * cw - len(label)) // 2)
                self.put(top, lx, label, self.attr("c_yel"))

    def _draw_x_axis(self, y, x0, cw, plot_w):
        self.put(y, x0 - 1, "└" + "─" * plot_w, self.attr("c_axis"))
        # BLE channel numbers, adv channels highlighted.
        step = 1 if cw >= 3 else (2 if cw == 2 else 4)
        for rf in range(NUM_CHANNELS):
            ble = rf_to_ble(rf)
            x = x0 + rf * cw
            if ble >= 37:
                self.put(y + 1, x, f"{ble}"[:max(cw, 2)], self.attr("c_mag") | curses.A_BOLD)
            elif rf % step == 0 and (cw >= 3 or rf_to_ble(rf + 1) < 37 if rf + 1 < NUM_CHANNELS else True):
                self.put(y + 1, x, f"{ble}"[:max(cw, 2)], self.attr("c_axis"))
        self.put(y + 1, 0, "BLE", curses.A_DIM)
        if self.sel is not None:
            self.put(y, x0 + self.sel * cw, "▲" * max(1, cw - 1 if cw >= 3 else cw),
                     self.attr("c_yel") | curses.A_BOLD)
            self.put(y + 1, x0 + self.sel * cw, f"{rf_to_ble(self.sel):<{cw}}"[:max(cw, 2)],
                     self.attr("c_yel") | curses.A_BOLD | curses.A_REVERSE)
        mhz_step = 5 if cw >= 2 else 10
        for rf in range(0, NUM_CHANNELS, mhz_step):
            self.put(y + 2, x0 + rf * cw, f"{rf_to_mhz(rf)}", curses.A_DIM)
        self.put(y + 2, 0, "MHz", curses.A_DIM)
        return y + 3

    def _draw_busy(self, top, x0, cw, height):
        self.put(top, 0, f"busy % (> {self.threshold} dBm)" if self.src.controllable
                 else "busy % (device threshold)", self.attr("c_title"))
        top += 1
        rep = self.latest
        bottom = top + height - 1
        self.put(top, 0, "100", curses.A_DIM)
        self.put(bottom, 0, "  0", curses.A_DIM)
        for i in range(height):
            self.put(top + i, x0 - 1, "│", self.attr("c_axis"))
        if not rep:
            return bottom + 1
        col_w = max(1, cw - 1) if cw >= 3 else cw
        for rf in range(NUM_CHANNELS):
            b = rep.stats[rf][3]
            level = b / 100 * height * 8
            a = self.attr("c_red") if b >= 10 else self.attr("c_yel")
            if rf == self.sel:
                a |= curses.A_BOLD
            for i in range(height):
                lo = i * 8
                if level >= lo + 8:
                    ch = "█"
                elif level > lo:
                    ch = EIGHTHS[max(1, int(level - lo))]
                else:
                    continue
                self.put(bottom - i, x0 + rf * cw, ch * col_w, a)
            if b and height * 8 * b / 100 < 1:
                self.put(bottom, x0 + rf * cw, "▁" * col_w, a)
        return bottom + 1

    def _draw_waterfall(self, top, x0, cw, height, plot_w):
        names = ("max", "avg", "min")
        title = f"waterfall ({names[self.wf_mode]}, newest on top)"
        self.put(top, 0, title, self.attr("c_title"))
        if self.history:
            n = min(len(self.history), height - 1)
            span = self.history[0].t - self.history[n - 1].t
            cnt = f"{n} rpts / {span:.1f} s"
            cx = x0 + plot_w - len(cnt)
            if cx > len(title) + 1:
                self.put(top, cx, cnt, curses.A_DIM)
        idx = (2, 1, 0)[self.wf_mode]
        for row, rep in enumerate(list(self.history)[:max(0, height - 1)]):
            y = top + 1 + row
            if row % 5 == 0:
                age = (self.history[0].t - rep.t) if self.history else 0
                self.put(y, 0, f"{age:4.0f}s" if age >= 1 else "  now", curses.A_DIM)
            for rf in range(min(NUM_CHANNELS, len(rep.stats))):
                v = rep.stats[rf][idx]
                if self.color:
                    self.put(y, x0 + rf * cw, "█" * cw, self.heat_attr(v))
                else:
                    t = 0 if v <= INVALID_DBM else (min(max(v, DBM_FLOOR), DBM_CEIL) - DBM_FLOOR) / (DBM_CEIL - DBM_FLOOR)
                    self.put(y, x0 + rf * cw, SHADES[min(4, int(t * 5))] * cw)

    def _draw_footer(self, y, w):
        if self.msg and time.monotonic() - self.msg_t < 3:
            self.put(y, 0, " " + self.msg + " ", self.attr("c_yel") | curses.A_REVERSE)
            return
        keys = [("q", "quit"), ("←→", "channel"), ("v", "wf"), ("w", "wifi"), ("h", "peak"),
                ("c", "clear"), ("p", "pause"), ("t/T", "thresh")]
        if self.src.controllable:
            keys = [("spc", "start/stop"), ("+/-", "passes"), ("[ ]", "avg us"),
                    ("i", "interval")] + keys
        x = 0
        for k, d in keys:
            if x + len(k) + len(d) + 2 >= w:
                break
            self.put(y, x, k, self.attr("c_title") | curses.A_REVERSE)
            self.put(y, x + len(k), " " + d + " ")
            x += len(k) + len(d) + 3


def run(scr, src, args):
    curses.curs_set(0)
    scr.nodelay(True)
    scr.keypad(True)
    ui = Ui(scr, src, args)
    src.start()
    if args.start and src.controllable:
        src.command("start", {})
    dirty, last_draw = True, 0
    while True:
        k = scr.getch()
        while k != -1:
            if k == curses.KEY_RESIZE:
                dirty = True
            elif not ui.handle_key(k):
                src.stop_evt.set()
                return
            dirty = True
            k = scr.getch()
        if ui.pump():
            dirty = True
        now = time.monotonic()
        if dirty or now - last_draw > 0.5:
            ui.draw()
            dirty, last_draw = False, now
        time.sleep(0.03)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["rcp", "chscan", "demo"], default="rcp")
    ap.add_argument("-p", "--port", help="serial port (rcp / chscan)")
    ap.add_argument("-b", "--baud", type=int, default=115200)
    ap.add_argument("--rtscts", action="store_true", help="RTS/CTS flow control (rcp)")
    ap.add_argument("--poll-ms", type=int, default=100, help="RCP poll period")
    ap.add_argument("--no-reset", action="store_true", help="skip HCI_Reset at connect (rcp)")
    ap.add_argument("--no-start", dest="start", action="store_false",
                    help="don't send Start on launch (rcp)")
    ap.add_argument("--threshold", type=int, default=-71, help="busy threshold, dBm")
    ap.add_argument("--floor", type=int, default=-110, help="bottom of the dBm scale")
    ap.add_argument("--ceil", type=int, default=-20, help="top of the dBm scale")
    args = ap.parse_args()
    global DBM_FLOOR, DBM_CEIL
    DBM_FLOOR, DBM_CEIL = args.floor, args.ceil
    if DBM_CEIL - DBM_FLOOR < 10:
        ap.error("--ceil must be at least 10 dB above --floor")
    if args.source != "demo" and not args.port:
        ap.error("--port is required for this source")
    out = queue.Queue()
    if args.source == "rcp":
        src = RcpSource(out, args.port, args.baud, args.rtscts, args.poll_ms, not args.no_reset)
        src.params["threshold"] = args.threshold
    elif args.source == "chscan":
        src = ChscanSource(out, args.port, args.baud)
    else:
        src = DemoSource(out)
    try:
        curses.wrapper(run, src, args)
    except KeyboardInterrupt:
        pass
    finally:
        src.stop_evt.set()


if __name__ == "__main__":
    main()
