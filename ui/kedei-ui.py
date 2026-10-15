#!/usr/bin/env python3
"""kedei-ui: touch UI for the KeDei LCD (status / graphs / docker / settings pages).

Renders a 480x320 RGB565 frame that the kedei-lcd daemon puts on the screen
and reads taps from the "KeDei Touch" input device."""

import json
import os
import re
import shutil
import socket
import statistics
import struct
import subprocess
import threading
import time
import urllib.request
from collections import deque

from PIL import Image, ImageChops, ImageDraw, ImageFont

OUT = "/run/kedei/fb.raw"
TOUCH_STATE = "/run/kedei/touch"
CAL_PATH = "/etc/kedei-touch.conf"
SETTINGS_PATH = "/etc/kedei-ui.conf"
W, H = 480, 320
TAB_H = 36
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

FG = (230, 230, 230)
DIM = (140, 140, 140)
ACCENT = (0, 200, 255)
OK = (0, 220, 100)
WARN = (255, 190, 0)
BAD = (255, 70, 70)
TAB_BG = (30, 30, 40)

PAGES = ["Status", "Graphs", "Docker", "Settings"]
# Calibration targets: inset corners, then the center for verification.
CAL_TARGETS = [(30, 30), (450, 30), (450, 290), (30, 290), (240, 160)]

THEMES = [("Black", (0, 0, 0)), ("Navy", (8, 16, 40)), ("Graphite", (32, 32, 36)),
          ("Forest", (6, 30, 18)), ("Plum", (34, 10, 34))]
# key: (label, choices, default, format)
SETTINGS = {
    "theme": ("Background", list(range(len(THEMES))), 0, lambda v: THEMES[v][0]),
    "refresh": ("Refresh", [1, 2, 5, 10], 1, lambda v: f"{v} s"),
    "temp_warn": ("Temp warn", list(range(50, 81, 5)), 65, lambda v: f"{v}°C"),
    "temp_bad": ("Temp alert", list(range(55, 86, 5)), 75, lambda v: f"{v}°C"),
    "use_warn": ("Usage warn", list(range(50, 96, 5)), 75, lambda v: f"{v}%"),
    "use_bad": ("Usage alert", list(range(55, 100, 5)), 90, lambda v: f"{v}%"),
}
SET_Y, SET_ROW = 44, 34
SET_BUTTONS = {"calibrate": (10, 252, 232, 282), "test": (248, 252, 470, 282)}

EV_KEY, EV_ABS, BTN_TOUCH, ABS_X, ABS_Y = 1, 3, 0x14A, 0, 1


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


# --- system info -----------------------------------------------------------

def ip_addr():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]
    except OSError:
        return "no network"
    finally:
        s.close()


_cache = {}


def cached(ttl, fn, *args):
    """Returns fn(*args), recomputed at most every ttl seconds."""
    now = time.monotonic()
    hit = _cache.get((fn, args))
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn(*args)
    _cache[(fn, args)] = (now, value)
    return value


def wifi():
    link = sh("iw", "dev", "wlan0", "link")
    ssid = next((l.split(":", 1)[1].strip() for l in link.splitlines() if "SSID" in l), "-")
    m = re.search(r"signal:\s*(-?\d+)", link)
    return ssid, int(m.group(1)) if m else None


def wifi_bars(dbm):
    if dbm is None:
        return 0
    return sum(dbm >= t for t in (-85, -75, -65, -55))


def ping_ms(host="1.1.1.1"):
    m = re.search(r"time=([\d.]+)", sh("ping", "-c1", "-W1", host))
    return float(m.group(1)) if m else None


def wan_info():
    """External IP and country, or None when offline."""
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=5) as r:
            j = json.load(r)
        return j.get("ip", "?"), j.get("country", "")
    except Exception:
        return None


def throttled():
    return sh("vcgencmd", "get_throttled").split("=")[-1]


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    return {k: saved[k] if saved.get(k) in choices else default
            for k, (_, choices, default, _) in SETTINGS.items()}


def save_settings(cfg):
    with open(SETTINGS_PATH + ".tmp", "w") as f:
        json.dump(cfg, f)
    os.replace(SETTINGS_PATH + ".tmp", SETTINGS_PATH)


def cpu_times():
    fields = [int(x) for x in read("/proc/stat").splitlines()[0].split()[1:]]
    return fields[3] + fields[4], sum(fields)


def net_bytes():
    rx = tx = 0
    for line in read("/proc/net/dev").splitlines()[2:]:
        name, data = line.split(":", 1)
        if name.strip() in ("wlan0", "eth0"):
            f = data.split()
            rx, tx = rx + int(f[0]), tx + int(f[8])
    return rx, tx


def temperature():
    return int(read("/sys/class/thermal/thermal_zone0/temp") or 0) / 1000


def cpu_mhz():
    return int(read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") or 0) // 1000


# vcgencmd get_throttled bits: 0-3 happening now, 16-19 happened since boot.
THROTTLE_BITS = [(0, "undervolt"), (3, "temp limit"), (2, "throttled"), (1, "freq capped")]


def power_status(raw):
    try:
        t = int(raw, 16)
    except ValueError:
        return "unknown", DIM
    now = [n for b, n in THROTTLE_BITS if t & (1 << b)]
    if now:
        # "throttled"/"freq capped" only repeat the cause, show them when alone.
        cause = [n for n in now if n in ("undervolt", "temp limit")] or now
        return " + ".join(cause).upper(), BAD
    past = [n for b, n in THROTTLE_BITS[:2] if t & (1 << (b + 16))]
    if past:
        return "OK (was: " + ", ".join(past) + ")", WARN
    return "OK", OK


class Sampler(threading.Thread):
    """Collects CPU/temperature/network history for the Graphs page."""

    PERIOD = 10  # seconds
    SIZE = 180   # 30 minutes

    def __init__(self):
        super().__init__(daemon=True)
        self.cpu = deque(maxlen=self.SIZE)
        self.temp = deque(maxlen=self.SIZE)
        self.rx = deque(maxlen=self.SIZE)
        self.tx = deque(maxlen=self.SIZE)
        self.mhz = 0
        self.ping = None
        self.wan = None
        self.wan_at = -1e9

    def run(self):
        idle0, total0 = cpu_times()
        rx0, tx0 = net_bytes()
        t0 = time.monotonic()
        while True:
            time.sleep(self.PERIOD)
            idle, total = cpu_times()
            rx, tx = net_bytes()
            t = time.monotonic()
            dt, di = total - total0, idle - idle0
            self.cpu.append(100.0 * (1 - di / dt) if dt else 0.0)
            self.temp.append(temperature())
            self.rx.append((rx - rx0) / (t - t0) / 1024)
            self.tx.append((tx - tx0) / (t - t0) / 1024)
            self.mhz = cpu_mhz()
            self.ping = ping_ms()
            # External IP: every 30 min, retry every minute while offline.
            if t - self.wan_at > (1800 if self.wan else 60):
                self.wan, self.wan_at = wan_info(), t
            idle0, total0, rx0, tx0, t0 = idle, total, rx, tx, t


def mem():
    info = {l.split(":")[0]: int(l.split()[1]) for l in read("/proc/meminfo").splitlines()}
    total, avail = info["MemTotal"] // 1024, info["MemAvailable"] // 1024
    return total - avail, total


def uptime():
    s = int(float(read("/proc/uptime").split()[0]))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    return f"{d}d {h}h {s // 60}m" if d else f"{h}h {s // 60}m"


def containers():
    out = sh("docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}")
    return [l.split("\t") for l in out.splitlines() if l.count("\t") == 3]


def level(v, warn, bad):
    return BAD if v >= bad else WARN if v >= warn else OK


def worst(*colors):
    return next((c for c in (BAD, WARN) if c in colors), OK)


# --- touch input -------------------------------------------------------------

def find_touch_device():
    for block in read("/proc/bus/input/devices").split("\n\n"):
        if 'Name="KeDei Touch"' in block:
            for line in block.splitlines():
                if line.startswith("H: Handlers="):
                    ev = next(h for h in line.split("=")[1].split() if h.startswith("event"))
                    return f"/dev/input/{ev}"
    return None


class Touch(threading.Thread):
    """Reads the uinput device; reports taps (on release) to the UI."""

    def __init__(self, on_tap):
        super().__init__(daemon=True)
        self.on_tap = on_tap

    def run(self):
        fmt = "llHHi"
        size = struct.calcsize(fmt)
        while True:
            dev = find_touch_device()
            if not dev:
                time.sleep(2)
                continue
            try:
                x = y = 0
                with open(dev, "rb") as f:
                    while True:
                        _, _, typ, code, val = struct.unpack(fmt, f.read(size))
                        if typ == EV_ABS and code == ABS_X:
                            x = val
                        elif typ == EV_ABS and code == ABS_Y:
                            y = val
                        elif typ == EV_KEY and code == BTN_TOUCH and val == 0:
                            self.on_tap(x, y)
            except OSError:
                time.sleep(1)


def raw_press_sample():
    """Waits for one press on the screen and returns its median raw (x, y)."""
    samples = []
    while True:
        parts = read(TOUCH_STATE).split()
        if len(parts) == 6:
            pressed, rx, ry = int(parts[0]), int(parts[3]), int(parts[4])
            if pressed:
                samples.append((rx, ry))
            elif len(samples) >= 3:
                # The first readings of a press are noisy: drop them.
                good = samples[2:] if len(samples) > 4 else samples
                return (statistics.median(s[0] for s in good),
                        statistics.median(s[1] for s in good))
            else:
                samples = []
        time.sleep(0.03)


def compute_calibration(raw):
    """raw: median raw readings for CAL_TARGETS[:4] -> driver config tuple."""
    tl, tr, br, bl = raw
    # If moving right on screen changes raw Y more than raw X, axes are swapped.
    swap = abs(tr[1] - tl[1]) > abs(tr[0] - tl[0])
    a = 1 if swap else 0  # raw component that follows screen X
    b = 1 - a
    (x0, y0), (x1, _), (_, y1), _ = CAL_TARGETS[:4]
    left = (tl[a] + bl[a]) / 2
    right = (tr[a] + br[a]) / 2
    top = (tl[b] + tr[b]) / 2
    bottom = (bl[b] + br[b]) / 2
    kx = (right - left) / (x1 - x0)
    ky = (bottom - top) / (y1 - y0)
    xmin, xmax = left - kx * x0, left + kx * (W - 1 - x0)
    ymin, ymax = top - ky * y0, top + ky * (H - 1 - y0)
    return round(xmin), round(xmax), round(ymin), round(ymax), int(swap)


# --- UI ------------------------------------------------------------------------

class UI:
    def __init__(self):
        self.font = ImageFont.truetype(FONT, 18)
        self.small = ImageFont.truetype(FONT, 15)
        self.bold = ImageFont.truetype(FONT_BOLD, 22)
        self.page = 0
        self.cfg = load_settings()
        self.touch_test = False  # touch test screen inside Settings
        self.taps = []  # recent taps on the touch test screen
        self.cal_step = None  # index into CAL_TARGETS while calibrating
        self.cal_msg = ""
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.stats = Sampler()
        self.stats.start()

    # Tap handling runs on the touch thread.
    def on_tap(self, x, y):
        with self.lock:
            if self.cal_step is not None:
                return  # calibration reads raw values itself
            if y >= H - TAB_H:
                self.page = min(x * len(PAGES) // W, len(PAGES) - 1)
                self.touch_test = False
            elif self.touch_test:
                self.taps = (self.taps + [(x, y)])[-8:]
            elif PAGES[self.page] == "Settings":
                self.settings_tap(x, y)
        self.wake.set()

    def settings_tap(self, x, y):
        for name, (bx0, by0, bx1, by1) in SET_BUTTONS.items():
            if bx0 <= x <= bx1 and by0 <= y <= by1:
                if name == "calibrate":
                    threading.Thread(target=self.calibrate, daemon=True).start()
                else:
                    self.touch_test, self.taps, self.cal_msg = True, [], ""
                return
        row = (y - SET_Y) // SET_ROW
        keys = list(SETTINGS)
        if 0 <= row < len(keys) and x >= 250:
            key = keys[row]
            choices = SETTINGS[key][1]
            i = choices.index(self.cfg[key]) + (1 if x >= 370 else -1)
            self.cfg[key] = choices[max(0, min(i, len(choices) - 1))]
            save_settings(self.cfg)

    def calibrate(self):
        raw = []
        for i in range(len(CAL_TARGETS)):
            with self.lock:
                self.cal_step = i
            self.wake.set()
            raw.append(raw_press_sample())
        cal = compute_calibration(raw[:4])
        with open(CAL_PATH + ".tmp", "w") as f:
            f.write(" ".join(map(str, cal)) + "\n")
        os.replace(CAL_PATH + ".tmp", CAL_PATH)
        with self.lock:
            self.cal_step = None
            self.taps = []
            self.touch_test = True  # let the user check the result right away
            self.cal_msg = "saved: " + " ".join(map(str, cal))
        self.wake.set()

    def draw_tabs(self, d):
        tw = W // len(PAGES)
        for i, name in enumerate(PAGES):
            x0 = i * tw
            active = i == self.page
            d.rectangle([x0, H - TAB_H, x0 + tw - 1, H - 1], fill=ACCENT if active else TAB_BG)
            d.text((x0 + tw // 2, H - TAB_H // 2), name, font=self.font,
                   fill=self.bg if active else FG, anchor="mm")

    def header(self, d, title):
        d.text((10, 6), title, font=self.bold, fill=ACCENT)
        clock = time.strftime("%H:%M:%S")
        d.text((W - 10, 6), clock, font=self.bold, fill=FG, anchor="ra")
        d.text((W - 18 - d.textlength(clock, font=self.bold), 12), time.strftime("%a %d %b"),
               font=self.small, fill=DIM, anchor="ra")
        d.line([10, 36, W - 10, 36], fill=DIM)

    def bar(self, d, x, y, w, pct, color):
        d.rectangle([x, y, x + w, y + 10], outline=DIM)
        d.rectangle([x + 1, y + 1, x + 1 + int((w - 2) * min(pct, 100) / 100), y + 9], fill=color)

    def signal_icon(self, d, x, y, bars):
        for i in range(4):
            h = 4 + i * 4
            box = [x + i * 6, y + 16 - h, x + i * 6 + 3, y + 16]
            d.rectangle(box, fill=FG if i < bars else TAB_BG)

    def page_status(self, d):
        self.header(d, socket.gethostname())
        c = self.cfg
        st = self.stats
        ssid, dbm = cached(5, wifi)
        temp = temperature()
        cpu = st.cpu[-1] if st.cpu else 0.0
        used, total = mem()
        ram_pct = 100 * used / total
        du = cached(30, shutil.disk_usage, "/")
        disk_pct = 100 * du.used / du.total
        power, power_color = power_status(cached(5, throttled))
        cs = cached(10, containers)
        load = read("/proc/loadavg").split()[0]
        wan = f"{st.wan[0]} {st.wan[1]}" if st.wan else "offline" if st.cpu else "..."
        ping = f"  {st.ping:.0f}ms" if st.ping is not None else ""
        rows = [
            ("IP", cached(30, ip_addr), FG),
            ("WAN", wan + ping, FG),
            ("WiFi", None, FG),
            ("Up", f"{uptime()}  load {load}", FG),
            ("Power", power, power_color),
            ("Docker", f"{sum(x[1] == 'running' for x in cs)}/{len(cs)} running", FG),
        ]
        y = 42
        for label, value, color in rows:
            d.text((10, y), label, font=self.font, fill=DIM)
            if label == "WiFi":
                self.signal_icon(d, 90, y + 1, wifi_bars(dbm))
                sig = f"  {dbm}dBm" if dbm is not None else ""
                d.text((120, y), f"{ssid}{sig}", font=self.font, fill=FG)
            else:
                d.text((90, y), value, font=self.font, fill=color)
            y += 22
        y += 6
        use = (c["use_warn"], c["use_bad"])
        for label, text, pct, color in [
            ("CPU", f"{cpu:3.0f}% {st.mhz or cpu_mhz()}MHz {temp:.0f}°C", cpu,
             worst(level(cpu, *use), level(temp, c["temp_warn"], c["temp_bad"]))),
            ("RAM", f"{ram_pct:3.0f}% {used}/{total}MB", ram_pct, level(ram_pct, *use)),
            ("Disk", f"{disk_pct:3.0f}% {du.used / 2**30:.1f}/{du.total / 2**30:.0f}GB", disk_pct,
             level(disk_pct, *use)),
        ]:
            d.text((10, y), label, font=self.font, fill=DIM)
            self.bar(d, 90, y + 5, 140, pct, color)
            d.text((240, y), text, font=self.font, fill=FG)
            y += 24

    def chart(self, d, top, height, title, series, vmin, vmax, marks=()):
        """Draws one history chart. series: [(values, color)], marks: [(value, color)]."""
        x0, x1 = 10, W - 10
        y0, y1 = top + 18, top + height
        d.text((x0, top), title, font=self.small, fill=FG)
        d.rectangle([x0, y0, x1, y1], outline=TAB_BG)
        span = max(vmax - vmin, 1e-9)

        def py(v):
            return y1 - 1 - (min(max(v, vmin), vmax) - vmin) / span * (y1 - y0 - 2)

        for v, color in marks:
            y = py(v)
            for x in range(x0 + 1, x1, 6):
                d.line([x, y, x + 2, y], fill=color)
        step = (x1 - x0 - 2) / (Sampler.SIZE - 1)
        for values, color in series:
            vals = list(values)
            off = Sampler.SIZE - len(vals)
            pts = [(x0 + 1 + (off + i) * step, py(v)) for i, v in enumerate(vals)]
            if len(pts) > 1:
                d.line(pts, fill=color, width=2)
            elif pts:
                d.point(pts, fill=color)

    def page_graphs(self, d):
        self.header(d, "Last 30 min")
        st = self.stats
        if not st.cpu:
            d.text((10, 50), "collecting...", font=self.font, fill=DIM)
            return
        cpu, temp, rx, tx = st.cpu[-1], st.temp[-1], st.rx[-1], st.tx[-1]
        self.chart(d, 40, 76, f"CPU {cpu:3.0f}%  {st.mhz}MHz  max {max(st.cpu):.0f}%",
                   [(st.cpu, ACCENT)], 0, 100,
                   marks=[(self.cfg["use_bad"], BAD), (self.cfg["use_warn"], WARN)])
        tw, tb = self.cfg["temp_warn"], self.cfg["temp_bad"]
        lo = min(min(st.temp), 40)
        hi = max(max(st.temp), tb + 5)
        self.chart(d, 120, 76, f"Temp {temp:.1f}°C  max {max(st.temp):.1f}°C",
                   [(st.temp, level(temp, tw, tb))], lo, hi, marks=[(tb, BAD), (tw, WARN)])
        net_max = max(max(st.rx), max(st.tx), 16)
        self.chart(d, 200, 78, f"Net \u2193{rx:6.1f} \u2191{tx:6.1f} KB/s",
                   [(st.rx, OK), (st.tx, WARN)], 0, net_max)

    def page_docker(self, d):
        self.header(d, "Docker")
        cs = cached(10, containers)
        if not cs:
            d.text((10, 50), "no containers", font=self.font, fill=DIM)
            return
        y = 44
        for name, state, status, image in cs[: (H - TAB_H - 44) // 38]:
            d.ellipse([10, y + 5, 22, y + 17], fill=OK if state == "running" else BAD)
            d.text((30, y), name, font=self.font, fill=FG)
            d.text((W - 10, y), status[:24], font=self.small, fill=DIM, anchor="ra")
            d.text((30, y + 19), image[:48], font=self.small, fill=DIM)
            y += 38

    def crosshair(self, d, x, y, color, r=12):
        d.line([x - r, y, x + r, y], fill=color, width=2)
        d.line([x, y - r, x, y + r], fill=color, width=2)
        d.ellipse([x - 4, y - 4, x + 4, y + 4], outline=color)

    def page_settings(self, d):
        if self.cal_step is not None:
            tx, ty = CAL_TARGETS[self.cal_step]
            d.text((W // 2, 110), f"Calibration {self.cal_step + 1}/{len(CAL_TARGETS)}",
                   font=self.bold, fill=ACCENT, anchor="mm")
            d.text((W // 2, 140), "press the center of the cross", font=self.font, fill=FG, anchor="mm")
            self.crosshair(d, tx, ty, WARN, r=20)
            return
        if self.touch_test:
            self.page_touch_test(d)
            return
        self.header(d, "Settings")
        for i, (key, (label, _, _, fmt)) in enumerate(SETTINGS.items()):
            y = SET_Y + i * SET_ROW
            d.text((10, y + 15), label, font=self.font, fill=FG, anchor="lm")
            for bx, sym in ((250, "<"), (426, ">")):
                d.rectangle([bx, y, bx + 44, y + 30], outline=ACCENT, width=2)
                d.text((bx + 22, y + 15), sym, font=self.bold, fill=ACCENT, anchor="mm")
            d.text((360, y + 15), fmt(self.cfg[key]), font=self.font, fill=FG, anchor="mm")
            if key == "theme":
                d.rectangle([200, y + 4, 222, y + 26], fill=THEMES[self.cfg[key]][1], outline=DIM)
        for name, label in (("calibrate", "Calibrate touch"), ("test", "Touch test")):
            box = SET_BUTTONS[name]
            d.rectangle(box, outline=ACCENT, width=2)
            d.text(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), label,
                   font=self.font, fill=ACCENT, anchor="mm")

    def page_touch_test(self, d):
        self.header(d, "Touch test")
        d.text((10, 44), "tap anywhere, a tab to exit", font=self.small, fill=DIM)
        if self.cal_msg:
            d.text((10, 64), self.cal_msg, font=self.small, fill=OK)
        for i, (x, y) in enumerate(self.taps):
            last = i == len(self.taps) - 1
            self.crosshair(d, x, y, WARN if last else DIM)
        if self.taps:
            x, y = self.taps[-1]
            d.text((10, H - TAB_H - 26), f"x={x:3d}  y={y:3d}", font=self.bold, fill=WARN)

    def render(self):
        self.bg = THEMES[self.cfg["theme"]][1]
        img = Image.new("RGB", (W, H), self.bg)
        d = ImageDraw.Draw(img)
        with self.lock:
            [self.page_status, self.page_graphs, self.page_docker, self.page_settings][self.page](d)
            if self.cal_step is None:
                self.draw_tabs(d)
        return img


def to_rgb565(img):
    """RGB -> RGB565 little-endian, done with C-level PIL ops."""
    r, g, b = img.split()
    hi = ImageChops.add(r.point(lambda v: v & 0xF8), g.point(lambda v: v >> 5))
    lo = ImageChops.add(g.point(lambda v: (v << 3) & 0xE0), b.point(lambda v: v >> 3))
    return Image.merge("LA", (lo, hi)).tobytes()


def main():
    ui = UI()
    Touch(ui.on_tap).start()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    while True:
        data = to_rgb565(ui.render())
        with open(OUT + ".tmp", "wb") as f:
            f.write(data)
        os.replace(OUT + ".tmp", OUT)
        ui.wake.wait(ui.cfg["refresh"])
        ui.wake.clear()


if __name__ == "__main__":
    main()
