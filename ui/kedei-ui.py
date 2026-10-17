#!/usr/bin/env python3
"""kedei-ui: touch UI for the KeDei LCD (status / graphs / home / docker / settings pages).

Renders a 480x320 RGB565 frame that the kedei-lcd daemon puts on the screen
and reads taps from the "KeDei Touch" input device."""

import calendar
import hashlib
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
PIN_PATH = "/etc/kedei-ui.pin"
HA_PATH = "/etc/kedei-ui.ha.json"
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

# Home is shown only when HA_PATH is configured, Docker only when docker is installed.
PAGES = ["Status", "Graphs", "Home", "Docker", "Settings"]
# Calibration targets: inset corners, then the center for verification.
CAL_TARGETS = [(30, 30), (450, 30), (450, 290), (30, 290), (240, 160)]

THEMES = [("Black", (0, 0, 0)), ("Navy", (8, 16, 40)), ("Graphite", (32, 32, 36)),
          ("Forest", (6, 30, 18)), ("Plum", (34, 10, 34))]

def minutes(v, zero):
    return zero if v == 0 else f"{v} min"


# key: (label, choices, default, format, settings page)
SETTINGS = {
    "theme": ("Background", list(range(len(THEMES))), 0, lambda v: THEMES[v][0], 0),
    "refresh": ("Refresh", [1, 2, 5, 10], 1, lambda v: f"{v} s", 0),
    "night": ("Night mode", ["off", "auto", "on"], "auto", str.capitalize, 0),
    "night_from": ("Night from", [18, 19, 20, 21, 22, 23, 0, 1], 0, lambda v: f"{v:02d}:00", 0),
    "night_to": ("Night until", [5, 6, 7, 8, 9, 10], 7, lambda v: f"{v:02d}:00", 0),
    "sleep": ("Screen off", [0, 1, 2, 5, 10, 30], 0, lambda v: minutes(v, "Never"), 0),
    "lock": ("PIN lock", [0, 1, 5, 15, 60], 0, lambda v: minutes(v, "Off"), 1),
    "night_dim": ("Night bright", [10, 20, 30, 50, 70], 30, lambda v: f"{v}%", 1),
    "temp_warn": ("Temp warn", list(range(50, 81, 5)), 65, lambda v: f"{v}°C", 1),
    "temp_bad": ("Temp alert", list(range(55, 86, 5)), 75, lambda v: f"{v}°C", 1),
    "use_warn": ("Usage warn", list(range(50, 96, 5)), 75, lambda v: f"{v}%", 1),
    "use_bad": ("Usage alert", list(range(55, 100, 5)), 90, lambda v: f"{v}%", 1),
}
SET_PAGES = 2
SET_Y, SET_ROW = 44, 34
SET_BUTTONS = {"page": (10, 252, 150, 282), "calibrate": (160, 252, 315, 282),
               "test": (325, 252, 470, 282)}

PIN_LEN = 4
PIN_TRIES, PIN_BLOCK_S = 5, 30
# Lock screen keypad: 3 x 4 keys below the PIN dots.
KEYPAD = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "Esc", "0", "Del"]
KP_X, KP_Y, KP_W, KP_H = 90, 82, 100, 58
DOCKER_Y, DOCKER_ROW = 44, 40
DOCKER_BUTTON_X = W - 110
CONFIRM_S = 3  # a second tap within this time confirms start/stop
# Home Assistant page: 4 x 4 grid of tiles.
HA_COLS, HA_ROWS = 4, 4
HA_Y, HA_GAP = 42, 4

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
            for k, (_, choices, default, _, _) in SETTINGS.items()}


def save_settings(cfg):
    with open(SETTINGS_PATH + ".tmp", "w") as f:
        json.dump(cfg, f)
    os.replace(SETTINGS_PATH + ".tmp", SETTINGS_PATH)


def pin_hash(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 20000).hex()


def pin_is_set():
    return os.path.exists(PIN_PATH)


def save_pin(pin):
    salt = os.urandom(16)
    fd = os.open(PIN_PATH + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"salt": salt.hex(), "hash": pin_hash(pin, salt)}, f)
    os.replace(PIN_PATH + ".tmp", PIN_PATH)


def check_pin(pin):
    try:
        with open(PIN_PATH) as f:
            data = json.load(f)
        return pin_hash(pin, bytes.fromhex(data["salt"])) == data["hash"]
    except (OSError, ValueError, KeyError):
        return False


def night_active(cfg):
    if cfg["night"] != "auto":
        return cfg["night"] == "on"
    h, start, end = time.localtime().tm_hour, cfg["night_from"], cfg["night_to"]
    return start <= h < end if start < end else h >= start or h < end


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


def docker_action(action, name):
    try:
        subprocess.run(["docker", action, name], capture_output=True, timeout=60)
    except Exception:
        pass
    _cache.pop((containers, ()), None)


def containers():
    out = sh("docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}")
    return [l.split("\t") for l in out.splitlines() if l.count("\t") == 3]


def level(v, warn, bad):
    return BAD if v >= bad else WARN if v >= warn else OK


def worst(*colors):
    return next((c for c in (BAD, WARN) if c in colors), OK)


# --- touch input -------------------------------------------------------------

class HomeAssistant(threading.Thread):
    """Polls entity states from the Home Assistant REST API and calls services.

    Configured by HA_PATH:
      {"url": "http://localhost:8123", "token_file": "/etc/kedei-ui.ha-token",
       "tiles": [{"entity": "light.kitchen", "name": "Kitchen", "tap": true,
                  "sub": "sensor.kitchen_temperature"},
                 {"name": "All off", "service": "light.turn_off", "target": "all"}]}
    """

    FAST, SLOW = 3, 30  # poll period with the page open / hidden, seconds
    TOGGLE = ("light", "switch", "input_boolean", "fan", "humidifier", "automation")

    def __init__(self, on_update):
        super().__init__(daemon=True)
        self.on_update = on_update
        self.visible = False
        self.states = {}
        self.error = None
        self.poke = threading.Event()
        try:
            with open(HA_PATH) as f:
                cfg = json.load(f)
            self.url = cfg.get("url", "http://localhost:8123").rstrip("/")
            self.token = read(cfg.get("token_file", "/etc/kedei-ui.ha-token"))
            self.tiles = cfg.get("tiles", [])[: HA_COLS * HA_ROWS]
        except (OSError, ValueError) as e:
            self.url, self.token, self.tiles = "", "", []
            self.error = f"no config: {HA_PATH}" if isinstance(e, OSError) else "bad config"

    def request(self, path, body=None):
        req = urllib.request.Request(self.url + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + self.token,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)

    def run(self):
        if not self.tiles:
            return
        while True:
            try:
                self.states = {e["entity_id"]: e for e in self.request("/api/states")}
                self.error = None
            except Exception as e:
                self.error = str(getattr(e, "reason", e))[:40]
            self.on_update()
            self.poke.wait(self.FAST if self.visible else self.SLOW)
            self.poke.clear()

    def action(self, tile):
        """Service call for a tapped tile, or None when the tile is read-only."""
        if "service" in tile:
            domain, service = tile["service"].split(".", 1)
            return domain, service, {"entity_id": tile.get("target", tile.get("entity"))}
        entity = tile.get("entity", "")
        domain = entity.split(".")[0]
        if not tile.get("tap"):
            return None
        if domain in self.TOGGLE:
            return "homeassistant", "toggle", {"entity_id": entity}
        if domain in ("scene", "script"):
            return domain, "turn_on", {"entity_id": entity}
        if domain == "vacuum":
            busy = self.state(entity) in ("cleaning", "returning")
            return "vacuum", "return_to_base" if busy else "start", {"entity_id": entity}
        return None

    def call(self, tile):
        act = self.action(tile)
        if not act:
            return False

        def run():
            try:
                self.request(f"/api/services/{act[0]}/{act[1]}", act[2])
            except Exception as e:
                self.error = str(getattr(e, "reason", e))[:40]
            time.sleep(0.5)  # let the state settle before re-reading it
            self.poke.set()

        threading.Thread(target=run, daemon=True).start()
        return True

    def state(self, entity):
        return self.states.get(entity, {}).get("state", "")

    def text(self, entity):
        """Short human readable state of an entity."""
        e = self.states.get(entity)
        if not e:
            return "?"
        st, attr = e["state"], e.get("attributes", {})
        if st in ("unavailable", "unknown"):
            return "n/a"
        domain = entity.split(".")[0]
        if domain == "weather":
            t = attr.get("temperature")
            return f"{round(t) if isinstance(t, (int, float)) else '?'}° {st.replace('partlycloudy', 'cloudy')}"
        if domain == "binary_sensor" and attr.get("device_class") in ("door", "window", "opening"):
            return "open" if st == "on" else "closed"
        if domain == "light" and st == "on" and attr.get("brightness"):
            return f"on {round(attr['brightness'] * 100 / 255)}%"
        if domain == "alarm_control_panel":
            return st.replace("armed_", "").replace("_", " ")
        if re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+(\+00:00|Z)?", st):
            # Timestamp sensors, e.g. "program finishes at".
            try:
                t = calendar.timegm(time.strptime(st[:19], "%Y-%m-%dT%H:%M:%S"))
                return time.strftime("%H:%M", time.localtime(t))
            except ValueError:
                return st[:10]
        try:
            v = float(st)
            unit = attr.get("unit_of_measurement", "")
            return f"{v:.0f}{unit}" if abs(v) >= 10 or v == int(v) else f"{v:.1f}{unit}"
        except ValueError:
            return st.replace("_", " ")

    def active(self, entity):
        st = self.state(entity)
        return st in ("on", "open", "cleaning", "playing", "run", "heating") or st.startswith("armed")


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
        self.big = ImageFont.truetype(FONT_BOLD, 30)
        self.page = 0
        self.set_page = 0  # settings sub-page
        self.cfg = load_settings()
        self.last_touch = time.monotonic()
        self.sleeping = False
        # PIN screen: None, "unlock", "new" or "repeat" (setting a new PIN).
        self.pin_mode = "unlock" if self.cfg["lock"] and pin_is_set() else None
        self.pin_buf = ""
        self.pin_first = ""
        self.pin_msg = ""
        self.pin_fails = 0
        self.pin_block_until = 0.0
        self.pin_lock_value = 0  # "lock" setting applied once the new PIN is confirmed
        self.touch_test = False  # touch test screen inside Settings
        self.confirm = None  # (container, deadline) waiting for the second tap
        self.busy = {}  # container -> "starting" / "stopping"
        self.taps = []  # recent taps on the touch test screen
        self.cal_step = None  # index into CAL_TARGETS while calibrating
        self.cal_msg = ""
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.stats = Sampler()
        self.stats.start()
        self.ha = HomeAssistant(self.wake.set)
        self.ha.start()
        self.ha_pressed = None  # (tile index, until) for the tap highlight
        self.pages = [p for p in PAGES
                      if (p != "Home" or self.ha.tiles) and (p != "Docker" or shutil.which("docker"))]

    # Tap handling runs on the touch thread.
    def on_tap(self, x, y):
        with self.lock:
            self.last_touch = time.monotonic()
            if self.cal_step is not None:
                return  # calibration reads raw values itself
            if self.sleeping:
                self.sleeping = False  # this tap only wakes the screen up
            elif self.pin_mode:
                self.pin_tap(x, y)
            elif y >= H - TAB_H:
                self.page = min(x * len(self.pages) // W, len(self.pages) - 1)
                self.touch_test = False
            elif self.touch_test:
                self.taps = (self.taps + [(x, y)])[-8:]
            elif self.pages[self.page] == "Settings":
                self.settings_tap(x, y)
            elif self.pages[self.page] == "Docker":
                self.docker_tap(x, y)
            elif self.pages[self.page] == "Home":
                self.ha_tap(x, y)
            self.ha.visible = self.pages[self.page] == "Home"
            if self.ha.visible:
                self.ha.poke.set()
        self.wake.set()

    def docker_tap(self, x, y):
        if x < DOCKER_BUTTON_X:
            return
        row = (y - DOCKER_Y) // DOCKER_ROW
        cs = cached(10, containers)
        if not 0 <= row < len(cs) or cs[row][0] in self.busy:
            return
        name, state = cs[row][0], cs[row][1]
        if not self.confirm or self.confirm[0] != name or time.monotonic() > self.confirm[1]:
            self.confirm = (name, time.monotonic() + CONFIRM_S)
            return
        self.confirm = None
        action = "stop" if state == "running" else "start"
        self.busy[name] = "stopping" if action == "stop" else "starting"

        def run():
            docker_action(action, name)
            with self.lock:
                self.busy.pop(name, None)
            self.wake.set()

        threading.Thread(target=run, daemon=True).start()

    def ha_cell(self, i):
        tw = (W - 10 - HA_GAP * (HA_COLS - 1)) // HA_COLS
        th = (H - TAB_H - HA_Y - 4 - HA_GAP * (HA_ROWS - 1)) // HA_ROWS
        x, y = 5 + (i % HA_COLS) * (tw + HA_GAP), HA_Y + (i // HA_COLS) * (th + HA_GAP)
        return [x, y, x + tw, y + th]

    def ha_tap(self, x, y):
        for i, tile in enumerate(self.ha.tiles):
            x0, y0, x1, y1 = self.ha_cell(i)
            if x0 <= x <= x1 and y0 <= y <= y1:
                if self.ha.call(tile):
                    self.ha_pressed = (i, time.monotonic() + 1.5)
                return

    def settings_keys(self):
        return [k for k, v in SETTINGS.items() if v[4] == self.set_page]

    def settings_tap(self, x, y):
        for name, (bx0, by0, bx1, by1) in SET_BUTTONS.items():
            if bx0 <= x <= bx1 and by0 <= y <= by1:
                if name == "page":
                    self.set_page = (self.set_page + 1) % SET_PAGES
                elif name == "calibrate":
                    threading.Thread(target=self.calibrate, daemon=True).start()
                else:
                    self.touch_test, self.taps, self.cal_msg = True, [], ""
                return
        row = (y - SET_Y) // SET_ROW
        keys = self.settings_keys()
        if 0 <= row < len(keys) and x >= 250:
            key = keys[row]
            choices = SETTINGS[key][1]
            i = choices.index(self.cfg[key]) + (1 if x >= 370 else -1)
            value = choices[max(0, min(i, len(choices) - 1))]
            if key == "lock" and value and (not self.cfg["lock"] or not pin_is_set()):
                # Turning the lock on: set a PIN first, apply the setting after.
                self.pin_mode, self.pin_buf, self.pin_msg = "new", "", ""
                self.pin_lock_value = value
                return
            self.cfg[key] = value
            save_settings(self.cfg)

    def pin_tap(self, x, y):
        col, row = (x - KP_X) // KP_W, (y - KP_Y) // KP_H
        if not (x >= KP_X and 0 <= col < 3 and 0 <= row < 4):
            return
        key = KEYPAD[row * 3 + col]
        if self.pin_mode == "unlock" and time.monotonic() < self.pin_block_until:
            return
        if key == "Del":
            self.pin_buf = self.pin_buf[:-1]
        elif key == "Esc":
            self.pin_buf = ""
            if self.pin_mode != "unlock":
                self.pin_mode, self.pin_msg = None, ""  # cancel setting a PIN
        elif len(self.pin_buf) < PIN_LEN:
            self.pin_buf += key
            if len(self.pin_buf) == PIN_LEN:
                self.pin_entered()

    def pin_entered(self):
        pin, self.pin_buf = self.pin_buf, ""
        if self.pin_mode == "unlock":
            if check_pin(pin):
                self.pin_mode, self.pin_msg, self.pin_fails = None, "", 0
                return
            self.pin_fails += 1
            self.pin_msg = "wrong PIN"
            if self.pin_fails >= PIN_TRIES:
                self.pin_fails = 0
                self.pin_block_until = time.monotonic() + PIN_BLOCK_S
        elif self.pin_mode == "new":
            self.pin_first, self.pin_mode, self.pin_msg = pin, "repeat", ""
        elif pin == self.pin_first:
            save_pin(pin)
            self.cfg["lock"] = self.pin_lock_value
            save_settings(self.cfg)
            self.pin_mode, self.pin_msg = None, ""
        else:
            self.pin_mode, self.pin_msg = "new", "PINs differ, try again"

    def update_idle(self):
        """Locks and blanks the screen after the configured idle time."""
        idle = time.monotonic() - self.last_touch
        if self.cal_step is not None:
            return
        if self.cfg["lock"] and not self.pin_mode and pin_is_set() and idle > self.cfg["lock"] * 60:
            self.pin_mode, self.pin_buf, self.pin_msg = "unlock", "", ""
            self.page, self.touch_test = 0, False
        if self.cfg["sleep"] and idle > self.cfg["sleep"] * 60:
            self.sleeping = True

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
        tw = W // len(self.pages)
        for i, name in enumerate(self.pages):
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
        if self.confirm and time.monotonic() > self.confirm[1]:
            self.confirm = None
        y = DOCKER_Y
        for name, state, status, image in cs[: (H - TAB_H - DOCKER_Y) // DOCKER_ROW]:
            running = state == "running"
            d.ellipse([10, y + 5, 22, y + 17], fill=OK if running else BAD)
            d.text((30, y), name[:20], font=self.font, fill=FG)
            d.text((30, y + 20), status[:30], font=self.small, fill=DIM)
            box = [DOCKER_BUTTON_X, y + 2, W - 10, y + DOCKER_ROW - 6]
            center = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
            if name in self.busy:
                d.rectangle(box, outline=DIM, width=2)
                d.text(center, self.busy[name] + "...", font=self.small, fill=DIM, anchor="mm")
            elif self.confirm and self.confirm[0] == name:
                d.rectangle(box, fill=WARN)
                d.text(center, "sure?", font=self.font, fill=self.bg, anchor="mm")
            else:
                color = BAD if running else OK
                d.rectangle(box, outline=color, width=2)
                d.text(center, "Stop" if running else "Start", font=self.font, fill=color, anchor="mm")
            y += DOCKER_ROW

    def page_home(self, d):
        self.header(d, "Home")
        ha = self.ha
        if not ha.tiles or (ha.error and not ha.states):
            d.text((10, 50), ha.error or "connecting...", font=self.font, fill=DIM)
            return
        if ha.error:
            d.text((110, 12), "offline", font=self.small, fill=BAD)
        pressed = self.ha_pressed[0] if self.ha_pressed and time.monotonic() < self.ha_pressed[1] else None
        for i, tile in enumerate(ha.tiles):
            box = self.ha_cell(i)
            entity = tile.get("entity", "")
            on = bool(entity) and ha.active(entity)
            na = bool(entity) and ha.text(entity) == "n/a"
            tappable = ha.action(tile) is not None
            if i == pressed:
                d.rectangle(box, fill=WARN)
            elif on:
                d.rectangle(box, fill=(0, 70, 90), outline=ACCENT, width=2)
            else:
                d.rectangle(box, fill=TAB_BG, outline=ACCENT if tappable else TAB_BG)
            fg = self.bg if i == pressed else DIM if na else FG
            name = tile.get("name") or ha.states.get(entity, {}).get("attributes", {}).get("friendly_name", entity)
            d.text((box[0] + 6, box[1] + 4), name[:11], font=self.small,
                   fill=self.bg if i == pressed else DIM)
            value = ha.text(entity) if entity else "tap"
            d.text((box[0] + 6, box[1] + 22), value[:10], font=self.font, fill=fg)
            if tile.get("sub"):
                d.text((box[2] - 5, box[1] + 5), ha.text(tile["sub"])[:6], font=self.small,
                       fill=fg, anchor="ra")

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
        for i, key in enumerate(self.settings_keys()):
            label, _, _, fmt, _ = SETTINGS[key]
            y = SET_Y + i * SET_ROW
            d.text((10, y + 15), label, font=self.font, fill=FG, anchor="lm")
            for bx, sym in ((250, "<"), (426, ">")):
                d.rectangle([bx, y, bx + 44, y + 30], outline=ACCENT, width=2)
                d.text((bx + 22, y + 15), sym, font=self.bold, fill=ACCENT, anchor="mm")
            d.text((360, y + 15), fmt(self.cfg[key]), font=self.font, fill=FG, anchor="mm")
            if key == "theme":
                d.rectangle([200, y + 4, 222, y + 26], fill=THEMES[self.cfg[key]][1], outline=DIM)
        page_label = f"Page {self.set_page + 1}/{SET_PAGES}"
        for name, label in (("page", page_label), ("calibrate", "Calibrate"), ("test", "Touch test")):
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

    def page_pin(self, d):
        title = {"unlock": "Locked", "new": "New PIN", "repeat": "Repeat PIN"}[self.pin_mode]
        self.header(d, title)
        wait = self.pin_block_until - time.monotonic()
        msg = f"too many tries, wait {wait:.0f} s" if wait > 0 else self.pin_msg
        if msg:
            d.text((10, 40), msg, font=self.small, fill=BAD)
        for i in range(PIN_LEN):
            cx = W // 2 + (i - (PIN_LEN - 1) / 2) * 30
            box = [cx - 8, 60, cx + 8, 76]
            if i < len(self.pin_buf):
                d.ellipse(box, fill=FG)
            else:
                d.ellipse(box, outline=DIM, width=2)
        for i, key in enumerate(KEYPAD):
            if key == "Esc" and self.pin_mode == "unlock" and not self.pin_buf:
                continue
            col, row = i % 3, i // 3
            box = [KP_X + col * KP_W + 3, KP_Y + row * KP_H + 3,
                   KP_X + (col + 1) * KP_W - 3, KP_Y + (row + 1) * KP_H - 3]
            d.rectangle(box, outline=ACCENT if key.isdigit() else DIM, width=2)
            d.text(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), key,
                   font=self.big if key.isdigit() else self.font,
                   fill=FG if key.isdigit() else DIM, anchor="mm")

    def render(self):
        self.bg = THEMES[self.cfg["theme"]][1]
        with self.lock:
            self.update_idle()
            if self.sleeping:
                return Image.new("RGB", (W, H), (0, 0, 0))
            img = Image.new("RGB", (W, H), self.bg)
            d = ImageDraw.Draw(img)
            if self.pin_mode:
                self.page_pin(d)
            else:
                getattr(self, "page_" + self.pages[self.page].lower())(d)
                if self.cal_step is None:
                    self.draw_tabs(d)
        if night_active(self.cfg):
            k = self.cfg["night_dim"] / 100
            img = img.point(lambda v: int(v * k))
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
