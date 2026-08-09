# main.py - Claude activity indicator, traffic-light edition
# Board: ESP32-C3 SuperMini + a 3-LED module with its own series resistors
#
# Same wire protocol and same arbitration as the 5x5 version, so the host side
# is identical. Three lamps instead of twenty-five pixels, so the states are
# folded down to what a traffic light can actually say:
#
#   green, slow swell   -> the turn finished
#   green, short blips  -> Claude is working
#   red, fast blink     -> Claude is blocked, waiting on you
#   red, double strike  -> a tool failed
#   all dark            -> nothing pending
#
# Yellow is unused; see YELLOW_USABLE below.
#
# Wire protocol, one line per message:
#   v1 <client_id> <state> [ttl_ms]   report a session's state
#   v1 <client_id> gone               drop a session
#   v1 * brightness <0-255>           set and persist brightness
#   v1 * reset                        clear every session
#   v1 * status                       print firmware status
#   v1 * selftest                     green, yellow, red in turn
#
# Hold BOOT (GPIO9) during power-up to skip this and land in the REPL.

import machine
import sys
import time
import json
import math

try:
    import uselect as select
except ImportError:
    import select

# All three on the left edge of the board, skipping GPIO2.
#
# GPIO2, 8 and 9 are boot strapping pins that must sit high at reset -- an LED
# pulling one of them down drops the chip into download mode instead of
# booting. GPIO18/19 are the native USB and GPIO20/21 are UART0. That leaves
# 0, 1, 3, 4, 5, 6, 7 and 10 as safe choices; swap these three numbers to match
# however you wired it.
GREEN_PIN = 1
YELLOW_PIN = 3
RED_PIN = 4

BOOT_PIN = 9
PWM_FREQ = 1000
CONFIG_FILE = "config.json"
FRAME_MS = 25

# ---------------------------------------------------------------- config

DEFAULTS = {"brightness": 255}


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

# ----------------------------------------------------------------- lamps

lamps = {
    "green": machine.PWM(machine.Pin(GREEN_PIN), freq=PWM_FREQ, duty_u16=0),
    "yellow": machine.PWM(machine.Pin(YELLOW_PIN), freq=PWM_FREQ, duty_u16=0),
    "red": machine.PWM(machine.Pin(RED_PIN), freq=PWM_FREQ, duty_u16=0),
}

frame = {"green": 0.0, "yellow": 0.0, "red": 0.0}

# Per-lamp ceiling, applied last. Protects a pin whose LED is short of series
# resistance, and evens out colours whose efficiency differs.
#
# Green is held down because this module carries a 0 ohm link where its
# resistor belongs, putting the LED straight across the GPIO. That also makes
# it the brightest of the three, so the cap costs nothing visually. Raise it to
# 1.0 if a few hundred ohms ever land in that line.
LAMP_CEILING = {"green": 0.35, "yellow": 1.0, "red": 1.0}

# This module shipped with 4.7k in series with the yellow LED instead of a few
# hundred ohms, which starves it to about 0.3 mA -- invisible, and nothing the
# firmware can do about it. So yellow is left out of the mapping and its
# meaning is carried by green rhythms instead. Set this True on a board with a
# sane resistor and the `working` branch below switches back to yellow.
YELLOW_USABLE = False


def clear():
    for name in frame:
        frame[name] = 0.0


def put(name, level):
    frame[name] = 0.0 if level <= 0 else (1.0 if level > 1.0 else level)


def flush(brightness):
    for name, pwm in lamps.items():
        level = frame[name]
        if level <= 0:
            pwm.duty_u16(0)
            continue
        # Gamma on the animation envelope, so slow fades look linear to the eye.
        # Gentler than the 1.8 the WS2812 matrix uses: with three discrete lamps
        # that have to stay readable in daylight, visibility beats perceptual
        # smoothness, and a steep curve spends too much of the cycle near dark.
        duty = int((level ** 1.4) * (brightness / 255.0)
                   * LAMP_CEILING.get(name, 1.0) * 65535)
        pwm.duty_u16(duty if duty > 0 else 1)


# ---------------------------------------------------------------- states

# state -> (priority, animation, default ttl in ms)
STATES = {
    "off":      (0,  "dark",      0),
    "idle":     (10, "dark",      0),
    "done":     (20, "finished",  3600000),
    "thinking": (40, "working",   300000),
    "tool":     (50, "working",   300000),
    "web":      (50, "working",   300000),
    "edit":     (50, "working",   300000),
    "bash":     (50, "working",   300000),
    "error":    (80, "fault",     20000),
    "ask":      (90, "attention", 900000),
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
        ttl_ms = STATES[state][2]
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


def render(state, t):
    _, anim, _ = STATES[state]
    clear()

    if anim == "dark":
        return

    if anim == "finished":
        # 6s swelling to full and back to dark, then 4s of nothing. Slow enough
        # to read from the corner of your eye, sparse enough not to nag.
        period, ramp = 10000, 6000
        position = t % period
        if position < ramp:
            put("green", math.sin(position / ramp * math.pi))

    elif anim == "working":
        if YELLOW_USABLE:
            phase = (t % 2000) / 2000.0
            put("yellow", 0.65 + 0.35 * (1 + math.sin(phase * 2 * math.pi)) / 2)
        else:
            # A short green blip on a steady beat: reads as "alive and busy",
            # and is impossible to confuse with the slow swell of `finished`.
            if (t % 1400) < 130:
                put("green", 1.0)

    elif anim == "attention":
        # Hard blink at full brightness. Red is reserved for this one state, so
        # that seeing red never has to be interpreted.
        put("red", 1.0 if (t // 280) % 2 == 0 else 0.0)

    elif anim == "fault":
        # Two quick red strikes, then a pause. Same colour as `attention`
        # because both want you, but a rhythm you can tell apart at a glance.
        position = t % 1400
        if position < 130 or (260 <= position < 390):
            put("red", 1.0)


# ---------------------------------------------------------------- input


def control(parts):
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
        print("STATUS ip=- state=%s clients=%d brightness=%d networks=0"
              % (winner(), len(clients), cfg["brightness"]))

    elif cmd == "selftest":
        for name in ("green", "yellow", "red"):
            clear()
            put(name, 1.0)
            flush(cfg["brightness"])
            print("SELFTEST %s on GPIO%d" % (name, {
                "green": GREEN_PIN, "yellow": YELLOW_PIN, "red": RED_PIN}[name]))
            time.sleep(1.5)
        clear()
        flush(cfg["brightness"])
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
    print("claude-rgb traffic light ready")

    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    buf = ""

    current = None
    state_since = time.ticks_ms()

    while True:
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
                if len(buf) > 200:
                    buf = ""

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
        put("red", 1.0)
        flush(40)
