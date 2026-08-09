# main.py - Claude activity indicator, 8-pixel strip edition
# Board: ESP32-C3 SuperMini + an 8x1 WS2812B strip on GPIO4
#
# Wiring: strip GND -> GND, strip VCC -> 3V3 (not 5V: the WS2812B wants a data
# high of 0.7 x VCC, and the C3 can only drive 3.3V, so a 5V-powered strip sits
# out of spec and glitches), strip DIN -> GPIO4.
#
# Same wire protocol and same arbitration as the 5x5 matrix version, so the
# host side is identical and the states mean the same thing. The animations are
# reworked for a line instead of a grid: what circles the rim on the matrix
# sweeps end to end here.
#
# Wire protocol, one line per message, over serial or UDP:
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
#   v1 * reverse <0|1>                flip which end the strip starts from
#   v1 * selftest                     pixel 0 red, pixel 7 blue
#
# Hold BOOT (GPIO9) during power-up to skip this and land in the REPL.

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

LED_PIN = 4
NUM_LEDS = 8
BOOT_PIN = 9
UDP_PORT = 47281
CONFIG_FILE = "config.json"
FRAME_MS = 25
WIFI_RETRY_MS = 60000

# ---------------------------------------------------------------- config

DEFAULTS = {
    "brightness": 45,
    "reverse": False,
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

# ---------------------------------------------------------------- pixels

np = neopixel.NeoPixel(machine.Pin(LED_PIN, machine.Pin.OUT), NUM_LEDS)

frame = [(0.0, 0.0, 0.0)] * NUM_LEDS


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
    if idx < 0 or idx >= NUM_LEDS or scale <= 0:
        return
    if scale > 1.0:
        scale = 1.0
    eased = scale ** 1.8
    frame[idx] = (color[0] * eased, color[1] * eased, color[2] * eased)


def put_all(color, scale=1.0):
    for i in range(NUM_LEDS):
        put(i, color, scale)


def flush(brightness):
    reverse = cfg.get("reverse")
    k = brightness / 255.0
    for i in range(NUM_LEDS):
        r, g, b = frame[i]
        out = [int(r * k + 0.5), int(g * k + 0.5), int(b * k + 0.5)]
        if out == [0, 0, 0]:
            # A dim pixel should stay lit, but only its dominant channel may be
            # rounded up: raising all three turns every faint colour into grey.
            peak = max(r, g, b)
            if peak > 0:
                out = [1 if channel == peak else 0 for channel in (r, g, b)]
        np[(NUM_LEDS - 1 - i) if reverse else i] = (out[0], out[1], out[2])
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
    # that the turn finished. Cleared by the next prompt.
    "done":     (20, GREEN,     "finish", 3600000),
    "thinking": (40, YELLOW,    "chase",  300000),
    "tool":     (50, YELLOW,    "chase",  300000),
    "web":      (50, YELLOW,    "chase",  300000),
    "edit":     (50, YELLOW,    "chase",  300000),
    "bash":     (50, YELLOW,    "chase",  300000),
    "error":    (80, RED,       "alarm",  20000),
    "ask":      (90, ORANGE,    "pulse",  900000),
}

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


def head_at(step):
    """Where the comet is on step `step`, bouncing between the two ends."""
    span = 2 * (NUM_LEDS - 1)
    position = step % span
    return position if position < NUM_LEDS else span - position


def chase(t, color, step_ms=80, trail=5, gain=1.0):
    """A comet head plus a fading tail, bouncing end to end.

    The tail is the head's own recent path, not a straight run of pixels
    behind it. On a strip that distinction is visible: a straight tail falls
    off the end at a turn, so one bounce reads as a reflection and the other
    as the comet blinking out and restarting. Following the path makes it fold
    back on itself and both ends behave alike.

    Drawn dimmest first so that where the path crosses itself the brighter
    sample wins. Whole pixels, no sub-pixel blending: across eight LEDs a hard
    edge reads sharper than a smeared one.
    """
    step = t // step_ms
    for k in range(trail - 1, -1, -1):
        put(head_at(step - k), color, (1.0 - k / float(trail)) * gain)


def render(state, t):
    _, color, anim, _ = STATES[state]
    clear()

    if anim == "off":
        return

    if anim == "chase":
        chase(t, color)

    elif anim == "idling":
        # Not much dimmer than this: below roughly a tenth the pixel bottoms out
        # at one bit per channel and you start seeing the three dies inside the
        # package rather than a single soft point of light.
        chase(t, color, step_ms=130, trail=4, gain=0.45)

    elif anim == "pulse" or anim == "alarm":
        # Half a second of nothing, then the breath. `alarm` beats twice.
        #
        # The dark half is a real gap rather than the bottom of a curve on
        # purpose: flush() floors any lit pixel at 1 so faint shades don't
        # vanish, which also means a curve that merely touches zero never
        # actually goes out. Drawing nothing is the only true black.
        level = double_breath(t) if anim == "alarm" else breath(t)
        if level > 0:
            put_all(color, level)

    elif anim == "finish":
        # Same shape as `pulse`, but travelling through the palette as it goes:
        # up through green, blue at the peak, back down through green.
        #
        # The hue lags the brightness. Moving them together would put green
        # only where the strip is nearly dark, and all you would see is blue.
        level = breath(t)
        if level > 0:
            mix = level ** 1.6
            put_all((
                GREEN[0] + (BLUE[0] - GREEN[0]) * mix,
                GREEN[1] + (BLUE[1] - GREEN[1]) * mix,
                GREEN[2] + (BLUE[2] - GREEN[2]) * mix,
            ), level)


# ---------------------------------------------------------------- input


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
            chase(step * 250, BLUE, step_ms=250, trail=3)
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


def control(parts):
    global wifi_ip

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
        print("STATUS ip=%s state=%s clients=%d brightness=%d networks=%d"
              % (wifi_ip or "-", winner(), len(clients),
                 cfg["brightness"], len(cfg.get("networks") or [])))

    elif cmd == "scan":
        try:
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

    elif cmd == "wifioff":
        # Radio down now, without a reboot. Saved profiles are left alone, so
        # this lasts until the next power cycle -- delete the profiles to make
        # it permanent.
        try:
            if wlan is not None:
                wlan.active(False)
        except Exception:
            pass
        wifi_ip = None
        print("OK wifioff")

    elif cmd == "reverse" and arg is not None:
        cfg["reverse"] = arg in ("1", "true", "True")
        save_config(cfg)
        print("OK reverse %s" % cfg["reverse"])

    elif cmd == "selftest":
        clear()
        put(0, RED)
        put(NUM_LEDS - 1, BLUE)
        flush(cfg["brightness"])
        print("SELFTEST red should be at the DIN end, blue at the far end")
        time.sleep(4)
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


# ---------------------------------------------------------------- main


def main():
    global wifi_next_try, wifi_ip

    wifi_connect()
    udp = open_udp() if wifi_ip else None
    print("claude-rgb strip ready")

    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    buf = ""

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

        # Keep the network side live. The connection and the socket are checked
        # separately, and they have to be: joining a network after boot -- which
        # is exactly what `netadd` does -- leaves the board connected and
        # pingable with nothing listening on the port, right up until the next
        # reboot quietly papers over it.
        connected = wlan is not None and wlan.isconnected()
        if not connected and cfg.get("networks"):
            if time.ticks_diff(time.ticks_ms(), wifi_next_try) >= 0:
                connected = bool(wifi_connect())
        if connected and udp is None:
            udp = open_udp()
        elif not connected and udp is not None:
            try:
                udp.close()
            except Exception:
                pass
            udp = None
            wifi_ip = None

        state = winner()
        if state != current:
            current = state
            state_since = time.ticks_ms()

        render(state, time.ticks_diff(time.ticks_ms(), state_since))
        flush(cfg["brightness"])
        time.sleep_ms(FRAME_MS)


# BOOT held at power-up -> drop to the REPL, so a bad edit can never lock us out.
if machine.Pin(BOOT_PIN, machine.Pin.IN, machine.Pin.PULL_UP).value() == 0:
    print("claude-rgb: BOOT held, skipping to REPL")
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
        put(0, RED, 1.0)
        flush(30)
