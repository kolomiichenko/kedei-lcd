#!/usr/bin/env python3
"""kedei-ui: touch UI for the KeDei LCD (status / docker / touch pages).

Renders a 480x320 RGB565 frame that the kedei-lcd daemon puts on the screen
and reads taps from the "KeDei Touch" input device."""

import os
import shutil
import socket
import statistics
import struct
import subprocess
import threading
import time

from PIL import Image, ImageChops, ImageDraw, ImageFont

OUT = "/run/kedei/fb.raw"
TOUCH_STATE = "/run/kedei/touch"
CAL_PATH = "/etc/kedei-touch.conf"
W, H = 480, 320
TAB_H = 36
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

BG = (0, 0, 0)
FG = (230, 230, 230)
DIM = (140, 140, 140)
ACCENT = (0, 200, 255)
OK = (0, 220, 100)
WARN = (255, 190, 0)
BAD = (255, 70, 70)
TAB_BG = (30, 30, 40)

PAGES = ["Status", "Docker", "Touch"]
# Calibration targets: inset corners, then the center for verification.
CAL_TARGETS = [(30, 30), (450, 30), (450, 290), (30, 290), (240, 160)]
CAL_BUTTON = (W - 150, 50, W - 10, 90)

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


def wifi():
    link = sh("iw", "dev", "wlan0", "link")
    ssid = next((l.split(":", 1)[1].strip() for l in link.splitlines() if "SSID" in l), "-")
    sig = next((l.split(":", 1)[1].strip() for l in link.splitlines() if "signal" in l), "")
    return ssid, sig


_cpu_prev = None


def cpu_usage():
    global _cpu_prev
    fields = [int(x) for x in read("/proc/stat").splitlines()[0].split()[1:]]
    idle, total = fields[3] + fields[4], sum(fields)
    usage = 0.0
    if _cpu_prev:
        di, dt = idle - _cpu_prev[0], total - _cpu_prev[1]
        usage = 100.0 * (1 - di / dt) if dt else 0.0
    _cpu_prev = (idle, total)
    return usage


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
        self.taps = []  # recent taps on the Touch page
        self.cal_step = None  # index into CAL_TARGETS while calibrating
        self.cal_msg = ""
        self.wake = threading.Event()
        self.lock = threading.Lock()

    # Tap handling runs on the touch thread.
    def on_tap(self, x, y):
        with self.lock:
            if self.cal_step is not None:
                return  # calibration reads raw values itself
            if y >= H - TAB_H:
                self.page = min(x * len(PAGES) // W, len(PAGES) - 1)
            elif PAGES[self.page] == "Touch":
                bx0, by0, bx1, by1 = CAL_BUTTON
                if bx0 <= x <= bx1 and by0 <= y <= by1:
                    threading.Thread(target=self.calibrate, daemon=True).start()
                else:
                    self.taps = (self.taps + [(x, y)])[-8:]
        self.wake.set()

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
            self.cal_msg = "saved: " + " ".join(map(str, cal))
        self.wake.set()

    def draw_tabs(self, d):
        tw = W // len(PAGES)
        for i, name in enumerate(PAGES):
            x0 = i * tw
            active = i == self.page
            d.rectangle([x0, H - TAB_H, x0 + tw - 1, H - 1], fill=ACCENT if active else TAB_BG)
            d.text((x0 + tw // 2, H - TAB_H // 2), name, font=self.font,
                   fill=BG if active else FG, anchor="mm")

    def header(self, d, title):
        d.text((10, 6), title, font=self.bold, fill=ACCENT)
        d.text((W - 10, 6), time.strftime("%H:%M:%S"), font=self.bold, fill=FG, anchor="ra")
        d.line([10, 36, W - 10, 36], fill=DIM)

    def bar(self, d, x, y, w, pct, color):
        d.rectangle([x, y, x + w, y + 10], outline=DIM)
        d.rectangle([x + 1, y + 1, x + 1 + int((w - 2) * min(pct, 100) / 100), y + 9], fill=color)

    def page_status(self, d):
        self.header(d, socket.gethostname())
        ssid, sig = wifi()
        temp = int(read("/sys/class/thermal/thermal_zone0/temp") or 0) / 1000
        cpu = cpu_usage()
        used, total = mem()
        du = shutil.disk_usage("/")
        disk_pct = 100 * du.used / du.total
        throttled = sh("vcgencmd", "get_throttled").split("=")[-1]
        cs = containers()
        power_ok = throttled in ("0x0", "")
        y = 42
        for label, value, color in [
            ("IP", ip_addr(), FG),
            ("WiFi", f"{ssid} {sig}", FG),
            ("Up", uptime(), FG),
            ("Power", "OK" if power_ok else f"throttled {throttled}", OK if power_ok else BAD),
            ("Docker", f"{sum(c[1] == 'running' for c in cs)}/{len(cs)} running", FG),
        ]:
            d.text((10, y), label, font=self.font, fill=DIM)
            d.text((90, y), value, font=self.font, fill=color)
            y += 22
        y += 4
        for label, text, pct, color in [
            ("CPU", f"{cpu:3.0f}%  {temp:.0f}°C", cpu, level(max(cpu, temp), 70, 85)),
            ("RAM", f"{used}/{total}MB", 100 * used / total, level(100 * used / total, 75, 90)),
            ("Disk", f"{du.used / 2**30:.1f}/{du.total / 2**30:.0f}GB", disk_pct, level(disk_pct, 80, 90)),
        ]:
            d.text((10, y), label, font=self.font, fill=DIM)
            self.bar(d, 90, y + 5, 150, pct, color)
            d.text((252, y), text, font=self.font, fill=FG)
            y += 22

    def page_docker(self, d):
        self.header(d, "Docker")
        cs = containers()
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

    def page_touch(self, d):
        if self.cal_step is not None:
            tx, ty = CAL_TARGETS[self.cal_step]
            d.text((W // 2, 110), f"Calibration {self.cal_step + 1}/{len(CAL_TARGETS)}",
                   font=self.bold, fill=ACCENT, anchor="mm")
            d.text((W // 2, 140), "press the center of the cross", font=self.font, fill=FG, anchor="mm")
            self.crosshair(d, tx, ty, WARN, r=20)
            return
        self.header(d, "Touch test")
        box = CAL_BUTTON
        d.rectangle(box, outline=ACCENT, width=2)
        d.text(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), "Calibrate",
               font=self.font, fill=ACCENT, anchor="mm")
        d.text((10, 50), "tap anywhere", font=self.font, fill=DIM)
        if self.cal_msg:
            d.text((10, 74), self.cal_msg, font=self.small, fill=OK)
        for i, (x, y) in enumerate(self.taps):
            last = i == len(self.taps) - 1
            self.crosshair(d, x, y, WARN if last else DIM)
        if self.taps:
            x, y = self.taps[-1]
            d.text((10, H - TAB_H - 26), f"x={x:3d}  y={y:3d}", font=self.bold, fill=WARN)

    def render(self):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        with self.lock:
            [self.page_status, self.page_docker, self.page_touch][self.page](d)
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
        ui.wake.wait(5)
        ui.wake.clear()


if __name__ == "__main__":
    main()
