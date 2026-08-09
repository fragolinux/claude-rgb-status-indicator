# main.py - Claude activity indicator
# Board: 01Space ESP32-PICO-D4-RGB (5x5 WS2812B on GPIO2, button B on GPIO0)
#
# Accepts status messages over USB serial and, when a known Wi-Fi network is
# in range, over UDP as well. Several Claude Code sessions can report at once;
# the board arbitrates by priority and renders the most urgent one.
#
# All the state and animation logic lives here so the host side stays a
# dependency-free one-line writer.
#
# Wire protocol, one line per message, on either transport:
#   v1 <client_id> <state> [ttl_ms]   report a session's state
#   v1 <client_id> gone               drop a session
#
# Control messages (serial only, used by the setup CLI):
#   v1 * brightness <0-255>           set and persist brightness
#   v1 * reset                        clear every session
#   v1 * status                       print firmware/network status
#   v1 * scan                         print networks in range
#   v1 * netlist                      print saved profiles
#   v1 * netadd <ssid_b64> <pass_b64> save a profile and reconnect
#   v1 * netdel <ssid_b64>            forget a profile
#   v1 * wifi                         retry the connection now
#   v1 * layout <serpentine> <rotate> remap the matrix
#   v1 * selftest                     light pixel 0 red, 4 green, 20 blue
#
# Button B (GPIO0): held at boot -> skip to REPL. Short press at runtime ->
# cycle brightness. Long press -> clear all sessions.

import machine
import neopixel
import network
import socket
import sys
import time
import json
import math

try:
    import uselect as select
except ImportError:
    import select

try:
    import ubinascii as binascii
except ImportError:
    import binascii

LED_PIN = 2
NUM_LEDS = 25
UDP_PORT = 47281
CONFIG_FILE = "config.json"
FRAME_MS = 25
BRIGHTNESS_STEPS = [10, 25, 60, 120, 200]
WIFI_RETRY_MS = 60000

# ---------------------------------------------------------------- config

DEFAULTS = {
    "brightness": 60,
    "serpentine": False,
    "rotate": 0,
    "networks": [],  # [{"ssid": ..., "password": ...}, ...]
    "hostname": "claude-rgb",
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
    except Exception:
        pass


cfg = load_config()

# Kill the blue status LED on GPIO27; the matrix is the only display we want.
# High-Z rather than a driven level, so it stays dark whichever way round the
# LED is wired. The board's other blue LED sits across the 3V3 rail and cannot
# be switched off in software at all.
machine.Pin(27, machine.Pin.IN, None)

# ---------------------------------------------------------------- pixels

np = neopixel.NeoPixel(machine.Pin(LED_PIN, machine.Pin.OUT), NUM_LEDS)


def build_order(serpentine, rotate):
    """Map logical (row, col) to physical strip index."""
    order = []
    for row in range(5):
        for col in range(5):
            r, c = row, col
            for _ in range(rotate % 4):
                r, c = c, 4 - r
            if serpentine and r % 2 == 1:
                order.append(r * 5 + (4 - c))
            else:
                order.append(r * 5 + c)
    return order


ORDER = build_order(cfg["serpentine"], cfg["rotate"])

# Perimeter, clockwise from top-left, in logical indices.
RING = [0, 1, 2, 3, 4, 9, 14, 19, 24, 23, 22, 21, 20, 15, 10, 5]

frame = [(0, 0, 0)] * NUM_LEDS


def clear():
    for i in range(NUM_LEDS):
        frame[i] = (0.0, 0.0, 0.0)


def put(idx, color, scale=1.0):
    """Paint one pixel at `scale` of its colour.

    Kept in floating point: rounding here and again in flush() would quantise
    twice, and at low brightness the second rounding lands channels on each
    other and shifts the hue.

    The gamma curve belongs here, on the animation envelope, and nowhere else.
    """
    if scale <= 0:
        return
    if scale > 1.0:
        scale = 1.0
    eased = scale ** 1.8
    frame[idx] = (color[0] * eased, color[1] * eased, color[2] * eased)


def flush(brightness):
    k = brightness / 255.0
    for logical in range(NUM_LEDS):
        r, g, b = frame[logical]
        out = [int(r * k + 0.5), int(g * k + 0.5), int(b * k + 0.5)]
        if out == [0, 0, 0]:
            # A dim pixel should stay lit, but only its dominant channel may be
            # rounded up: raising all three turns every faint colour into grey.
            peak = max(r, g, b)
            if peak > 0:
                out = [1 if channel == peak else 0 for channel in (r, g, b)]
        np[ORDER[logical]] = (out[0], out[1], out[2])
    np.write()


# ---------------------------------------------------------------- states

# Shared palette. These four meanings carry the same colour on every device in
# this project; only the animation changes to suit the geometry.
#
#   yellow  something is running
#   orange  waiting for you to answer
#   red     something failed
#   green -> blue   the turn finished
#
# White is never a meaning of its own, only the moving highlight on top of a
# held colour. Idle is dim white for the same reason: it has to stay outside
# the vocabulary.
YELLOW = (255, 200, 0)
ORANGE = (255, 90, 0)
RED = (255, 0, 0)
GREEN = (0, 255, 60)
BLUE = (0, 80, 255)
WHITE = (255, 255, 255)

# state -> (priority, colour, animation, default ttl in ms)
#
# Every tool shares one appearance. Which tool is running is not something you
# act on, so spending a colour on it would only dilute the four that matter.
STATES = {
    "off":      (0,  (0, 0, 0), "off",    0),
    "idle":     (10, WHITE,     "idling", 0),
    # Persistent on purpose: walking back after ten minutes should still show
    # that the turn finished. Cleared by the next prompt, or button B.
    "done":     (20, BLUE,      "pulse",  3600000),
    "thinking": (40, YELLOW,    "chase",  300000),
    "tool":     (50, YELLOW,    "chase",  300000),
    "web":      (50, YELLOW,    "chase",  300000),
    "edit":     (50, YELLOW,    "chase",  300000),
    "bash":     (50, YELLOW,    "chase",  300000),
    "error":    (80, RED,       "alarm",  20000),
    "ask":      (90, ORANGE,    "pulse",  900000),
}

# The moving highlight that runs the rim on top of a held colour. Green on the
# blue of `done` so the finished state still reads as green at a glance; white
# on the two that want answering.
HIGHLIGHT = {"ask": WHITE, "error": WHITE, "done": GREEN}

CHASE_PIXELS = 5

# Step timings are scaled so a lap of this 16-pixel rim takes the same wall
# time as one bounce of the 8-pixel strip, which is 2 x (8 - 1) = 14 steps.
# Matching the step interval instead would leave the two devices ticking 14%
# apart, which shows the moment you put them side by side.

FALLBACK = "idle"

# ---------------------------------------------------------- arbitration

clients = {}  # client_id -> [state, expiry_ticks_or_None]


def note(client_id, state, ttl_ms=None):
    if state == "gone":
        clients.pop(client_id, None)
        return
    if state not in STATES:
        return
    if ttl_ms is None:
        ttl_ms = STATES[state][3]
    expiry = time.ticks_add(time.ticks_ms(), ttl_ms) if ttl_ms else None
    clients[client_id] = [state, expiry]


def winner():
    """Highest-priority live state across every reporting session."""
    now = time.ticks_ms()
    best = FALLBACK
    best_pri = -1
    for cid in list(clients.keys()):
        state, expiry = clients[cid]
        if expiry is not None and time.ticks_diff(expiry, now) <= 0:
            # 'done' decays to idle; a stale working state just drops off.
            if state == "done":
                clients[cid] = ["idle", None]
                state = "idle"
            else:
                del clients[cid]
                continue
        pri = STATES[state][0]
        if pri > best_pri:
            best_pri = pri
            best = state
    return best


# ---------------------------------------------------------------- render


def breath(t, period=2000, dark=500):
    """The shared pulse envelope: `dark` ms of nothing, then a sine swell.

    Returns 0 during the gap, so callers draw no pixels at all and the panel
    goes genuinely black rather than resting on flush()'s minimum.
    """
    position = t % period
    if position < dark:
        return 0.0
    return math.sin((position - dark) / float(period - dark) * math.pi)


def double_breath(t, period=3000, dark=500):
    """Two beats instead of one, for the state that must not be mistaken.

    Half a second of black, then two full sine humps back to back. The trough
    between them goes all the way to zero rather than resting on a dim hold:
    the pair only reads as two beats if the colour actually leaves in between.
    """
    position = t % period
    if position < dark:
        return 0.0
    p = (position - dark) / float(period - dark)
    return abs(math.sin(2 * math.pi * p))


def ring_chase(t, color, step_ms=70, pixels=CHASE_PIXELS, gain=1.0):
    """A comet head plus a fading tail, running the rim.

    The head sits at full and each pixel behind it drops a step, so the tail
    reads as motion rather than as a lit block sliding round.
    """
    head = (t // step_ms) % len(RING)
    for k in range(pixels):
        put(RING[(head - k) % len(RING)], color, (1.0 - k / float(pixels)) * gain)


def render(state, t):
    _, color, anim, _ = STATES[state]
    clear()

    if anim == "off":
        return

    if anim == "chase":
        ring_chase(t, color)

    elif anim == "idling":
        # Not much dimmer than this: below roughly a tenth the pixel bottoms out
        # at one bit per channel and you start seeing the three dies inside the
        # package rather than a single soft point of light.
        ring_chase(t, color, step_ms=114, pixels=4, gain=0.45)

    elif anim == "pulse":
        # The whole panel breathing the state's colour, with a contrasting
        # comet running the rim on top. The comet keeps full brightness through
        # the dark half of the breath, so the panel never looks switched off,
        # only quiet.
        level = breath(t)
        if level > 0:
            for i in range(NUM_LEDS):
                put(i, color, level * 0.60)
        ring_chase(t, HIGHLIGHT.get(state, WHITE), step_ms=50)

    elif anim == "alarm":
        # Here the comet follows the envelope too, unlike `pulse`. Two beats
        # only read as two if the panel goes properly dark between them, and a
        # comet still running at full brightness across the trough hides it.
        level = double_breath(t)
        if level > 0:
            for i in range(NUM_LEDS):
                put(i, color, level * 0.60)
            ring_chase(t, HIGHLIGHT.get(state, WHITE), step_ms=50, gain=level)


# ---------------------------------------------------------------- wifi

wlan = None
wifi_ip = None
wifi_next_try = 0


def b64d(s):
    try:
        return binascii.a2b_base64(s).decode()
    except Exception:
        return ""


def b64e(s):
    return binascii.b2a_base64(s.encode()).decode().strip()


def wifi_connect(verbose=False):
    """Join whichever saved network happens to be in range at this location."""
    global wlan, wifi_ip, wifi_next_try

    wifi_next_try = time.ticks_add(time.ticks_ms(), WIFI_RETRY_MS)
    saved = cfg.get("networks") or []
    if not saved:
        wifi_ip = None
        return None

    if wlan is None:
        wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    try:
        wlan.config(dhcp_hostname=cfg.get("hostname", "claude-rgb"))
    except Exception:
        pass

    if wlan.isconnected():
        wifi_ip = wlan.ifconfig()[0]
        return wifi_ip

    try:
        in_range = set()
        for entry in wlan.scan():
            try:
                in_range.add(entry[0].decode())
            except Exception:
                pass
    except Exception:
        in_range = set()

    for profile in saved:
        ssid = profile.get("ssid", "")
        if in_range and ssid not in in_range:
            continue
        if verbose:
            print("WIFI trying %s" % ssid)
        try:
            wlan.connect(ssid, profile.get("password", ""))
        except Exception:
            continue
        for step in range(40):  # ~10s
            if wlan.isconnected():
                wifi_ip = wlan.ifconfig()[0]
                print("IP %s" % wifi_ip)
                return wifi_ip
            clear()
            head = step % len(RING)
            for tail in range(4):
                put(RING[(head - tail) % len(RING)], BLUE, 1.0 - tail * 0.25)
            flush(cfg["brightness"])
            time.sleep_ms(250)

    wifi_ip = None
    if verbose:
        print("WIFI no known network in range")
    return None


def open_udp():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", UDP_PORT))
        sock.setblocking(False)
        return sock
    except Exception as exc:
        print("UDP bind failed: %s" % exc)
        return None


# ---------------------------------------------------------------- input


def control(parts):
    """Handle 'v1 * ...' messages. Replies go to the serial console."""
    global ORDER

    cmd = parts[2]
    arg = parts[3] if len(parts) > 3 else None

    if cmd == "brightness" and arg is not None:
        try:
            cfg["brightness"] = max(0, min(255, int(arg)))
            save_config(cfg)
            print("OK brightness %d" % cfg["brightness"])
        except Exception:
            print("ERR brightness")

    elif cmd == "reset":
        clients.clear()
        print("OK reset")

    elif cmd == "status":
        print("STATUS ip=%s state=%s clients=%d brightness=%d networks=%d" % (
            wifi_ip or "-", winner(), len(clients),
            cfg["brightness"], len(cfg.get("networks") or [])))

    elif cmd == "scan":
        try:
            if wlan is None:
                network.WLAN(network.STA_IF).active(True)
            sta = wlan or network.WLAN(network.STA_IF)
            sta.active(True)
            seen = {}
            for entry in sta.scan():
                try:
                    ssid = entry[0].decode()
                except Exception:
                    continue
                if ssid and (ssid not in seen or entry[3] > seen[ssid]):
                    seen[ssid] = entry[3]
            for ssid in sorted(seen, key=lambda s: -seen[s]):
                print("SCAN %s %d" % (b64e(ssid), seen[ssid]))
        except Exception as exc:
            print("ERR scan %s" % exc)
        print("END scan")

    elif cmd == "netlist":
        for profile in cfg.get("networks") or []:
            print("NET %s" % b64e(profile.get("ssid", "")))
        print("END netlist")

    elif cmd == "netadd" and arg is not None:
        ssid = b64d(arg)
        password = b64d(parts[4]) if len(parts) > 4 else ""
        if not ssid:
            print("ERR netadd")
            return
        nets = [n for n in (cfg.get("networks") or []) if n.get("ssid") != ssid]
        nets.insert(0, {"ssid": ssid, "password": password})
        cfg["networks"] = nets
        save_config(cfg)
        print("OK netadd")
        wifi_connect(verbose=True)

    elif cmd == "netdel" and arg is not None:
        ssid = b64d(arg)
        cfg["networks"] = [
            n for n in (cfg.get("networks") or []) if n.get("ssid") != ssid
        ]
        save_config(cfg)
        print("OK netdel")

    elif cmd == "wifi":
        wifi_connect(verbose=True)
        print("OK wifi %s" % (wifi_ip or "-"))

    elif cmd == "layout" and arg is not None:
        try:
            cfg["serpentine"] = arg in ("1", "true", "True")
            cfg["rotate"] = int(parts[4]) if len(parts) > 4 else 0
            save_config(cfg)
            ORDER = build_order(cfg["serpentine"], cfg["rotate"])
            print("OK layout")
        except Exception:
            print("ERR layout")

    elif cmd == "selftest":
        clear()
        put(0, RED)
        put(4, GREEN)
        put(20, BLUE)
        flush(cfg["brightness"])
        time.sleep(3)
        print("OK selftest")

    else:
        print("ERR unknown %s" % cmd)


def handle_line(line):
    parts = line.strip().split()
    if len(parts) < 3 or parts[0] != "v1":
        return
    if parts[1] == "*":
        control(parts)
        return

    ttl = None
    if len(parts) > 3:
        try:
            ttl = int(parts[3])
        except Exception:
            ttl = None
    note(parts[1], parts[2], ttl)


# ---------------------------------------------------------------- button


class Button:
    def __init__(self):
        self.pin = machine.Pin(0, machine.Pin.IN, machine.Pin.PULL_UP)
        self.down_at = None
        self.fired = False

    def poll(self):
        """Returns 'short', 'long', or None."""
        pressed = self.pin.value() == 0
        now = time.ticks_ms()
        if pressed and self.down_at is None:
            self.down_at = now
            self.fired = False
        elif pressed and not self.fired:
            if time.ticks_diff(now, self.down_at) > 1000:
                self.fired = True
                return "long"
        elif not pressed and self.down_at is not None:
            held = time.ticks_diff(now, self.down_at)
            self.down_at = None
            if not self.fired and held > 40:
                return "short"
        return None


# ---------------------------------------------------------------- main


def main():
    global wifi_next_try

    wifi_connect()
    udp = open_udp() if wifi_ip else None
    print("claude-rgb ready")

    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    buf = ""

    button = Button()
    current = None
    state_since = time.ticks_ms()

    while True:
        # Serial: always available, and the only transport during setup.
        while poll.poll(0):
            ch = sys.stdin.read(1)
            if not ch:
                break
            if ch in ("\n", "\r"):
                if buf:
                    try:
                        handle_line(buf)
                    except Exception as exc:
                        print("ERR %s" % exc)
                    buf = ""
            else:
                buf += ch
                if len(buf) > 400:
                    buf = ""

        # UDP: broadcast from any machine on the current network.
        if udp is not None:
            while True:
                try:
                    data, _ = udp.recvfrom(256)
                except Exception:
                    break
                if not data:
                    break
                try:
                    handle_line(data.decode())
                except Exception:
                    pass

        # Moved location, or the AP came back? Retry quietly in the background.
        if cfg.get("networks") and time.ticks_diff(time.ticks_ms(), wifi_next_try) >= 0:
            if wlan is None or not wlan.isconnected():
                if wifi_connect() and udp is None:
                    udp = open_udp()
            else:
                wifi_next_try = time.ticks_add(time.ticks_ms(), WIFI_RETRY_MS)

        press = button.poll()
        if press == "short":
            steps = BRIGHTNESS_STEPS
            nxt = 0
            for i, s in enumerate(steps):
                if s > cfg["brightness"]:
                    nxt = i
                    break
            cfg["brightness"] = steps[nxt]
            save_config(cfg)
        elif press == "long":
            clients.clear()

        state = winner()
        if state != current:
            current = state
            state_since = time.ticks_ms()

        render(state, time.ticks_diff(time.ticks_ms(), state_since))
        flush(cfg["brightness"])
        time.sleep_ms(FRAME_MS)


# Button B held at boot -> drop to REPL, so a bad edit can never brick the board.
if machine.Pin(0, machine.Pin.IN, machine.Pin.PULL_UP).value() == 0:
    print("claude-rgb: button held, skipping to REPL")
else:
    try:
        main()
    except KeyboardInterrupt:
        clear()
        flush(0)
        print("claude-rgb: interrupted")
    except Exception as exc:
        sys.print_exception(exc)
        clear()
        for _p in RING[:3]:
            put(_p, RED, 1.0)
        flush(30)
