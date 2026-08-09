#!/usr/bin/env python3
"""Host side of the Claude activity indicator (01Space ESP32-PICO-D4-RGB).

One self-contained file, standard library only. Copy it to any machine --
Windows, macOS, Linux, WSL -- and it works with no pip install. If pyserial
happens to be present it is used; otherwise a small ctypes/termios shim
talks to the port directly.

Usage:
    claude_rgb.py doctor           check every link in the chain on this machine
    claude_rgb.py hook <state>     report a state, reading hook JSON on stdin
    claude_rgb.py send <state>     report a state manually (for testing)
    claude_rgb.py deploy [device]  upload <device>/main.py and restart the board
    claude_rgb.py setup            configure the board's Wi-Fi profiles
    claude_rgb.py status           ask the board how it's doing
    claude_rgb.py ports            list candidate serial ports
    claude_rgb.py brightness <n>   set panel brightness (0-255)
    claude_rgb.py selftest         verify the matrix orientation
    claude_rgb.py install          merge the hooks into ~/.claude/settings.json
    claude_rgb.py uninstall        remove only our hooks, leave the rest

Per-machine settings live in ~/.claude/claude-rgb.json:
    {"port": "COM8", "host": null, "transport": "auto", "enabled": true}
"""

import glob
import json
import os
import socket
import sys
import time

BAUD = 115200
UDP_PORT = 47281
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".claude", "claude-rgb.json")

CH340_IDS = [(0x1A86, 0x7523), (0x1A86, 0x55D4), (0x10C4, 0xEA60), (0x0403, 0x6001)]

DEFAULT_CONFIG = {
    "port": None,         # None = autodetect. Set explicitly to pin it down.
    "host": None,         # None = UDP broadcast. Set an IP for WSL/NAT.
    "transport": "auto",  # auto | serial | udp | off
    "enabled": True,
    "device": "01space",  # default firmware directory for `deploy`
}

# Filled in from a global --port flag, when one is given.
OPTIONS = {}


# ------------------------------------------------------------------ config


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ------------------------------------------------------- serial transport
#
# pyserial when available, otherwise a minimal shim. Both deliberately leave
# DTR and RTS deasserted: on boards that do wire the auto-reset circuit,
# asserting them would reboot the ESP32 on every single hook.


class SerialError(Exception):
    pass


def _open_pyserial(port):
    import serial  # noqa: F401  (only reached when it is installed)

    # Configure before open(): pyserial asserts DTR on open otherwise, which
    # would reboot the board on every hook if the reset circuit is wired.
    conn = serial.Serial()
    conn.port = port
    conn.baudrate = BAUD
    conn.timeout = 0.4
    conn.dsrdtr = False
    conn.rtscts = False
    conn.dtr = False
    conn.rts = False
    conn.open()
    return conn


class _WindowsSerial:
    def __init__(self, port):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32 = k32

        class DCB(ctypes.Structure):
            _fields_ = [
                ("DCBlength", wintypes.DWORD),
                ("BaudRate", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("wReserved", wintypes.WORD),
                ("XonLim", wintypes.WORD),
                ("XoffLim", wintypes.WORD),
                ("ByteSize", ctypes.c_byte),
                ("Parity", ctypes.c_byte),
                ("StopBits", ctypes.c_byte),
                ("XonChar", ctypes.c_char),
                ("XoffChar", ctypes.c_char),
                ("ErrorChar", ctypes.c_char),
                ("EofChar", ctypes.c_char),
                ("EvtChar", ctypes.c_char),
                ("wReserved1", wintypes.WORD),
            ]

        class COMMTIMEOUTS(ctypes.Structure):
            _fields_ = [
                ("ReadIntervalTimeout", wintypes.DWORD),
                ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
                ("ReadTotalTimeoutConstant", wintypes.DWORD),
                ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
                ("WriteTotalTimeoutConstant", wintypes.DWORD),
            ]

        handle = k32.CreateFileW(
            "\\\\.\\" + port, 0xC0000000, 0, None, 3, 0, None
        )
        if handle == -1 or handle == 0xFFFFFFFFFFFFFFFF:
            raise SerialError("cannot open %s (in use?)" % port)
        self.handle = handle

        dcb = DCB()
        dcb.DCBlength = ctypes.sizeof(DCB)
        if not k32.GetCommState(handle, ctypes.byref(dcb)):
            self.close()
            raise SerialError("GetCommState failed on %s" % port)
        dcb.BaudRate = BAUD
        dcb.ByteSize = 8
        dcb.Parity = 0
        dcb.StopBits = 0
        dcb.flags = 0x0001  # fBinary only: DTR_DISABLE, RTS_DISABLE, no flow control
        if not k32.SetCommState(handle, ctypes.byref(dcb)):
            self.close()
            raise SerialError("SetCommState failed on %s" % port)

        timeouts = COMMTIMEOUTS(0xFFFFFFFF, 0, 400, 0, 1000)
        k32.SetCommTimeouts(handle, ctypes.byref(timeouts))

    def write(self, data):
        written = self.ctypes.wintypes.DWORD(0)
        ok = self.k32.WriteFile(
            self.handle, data, len(data), self.ctypes.byref(written), None
        )
        if not ok:
            raise SerialError("write failed")
        return written.value

    def read(self, size=256):
        buf = self.ctypes.create_string_buffer(size)
        got = self.ctypes.wintypes.DWORD(0)
        ok = self.k32.ReadFile(
            self.handle, buf, size, self.ctypes.byref(got), None
        )
        if not ok:
            return b""
        return buf.raw[: got.value]

    def close(self):
        try:
            self.k32.CloseHandle(self.handle)
        except Exception:
            pass


class _PosixSerial:
    def __init__(self, port):
        import termios
        import fcntl

        self.termios = termios
        # O_NONBLOCK so open() doesn't hang waiting for DCD on a bare port.
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(self.fd)
            raise SerialError("%s is busy" % port)

        attrs = termios.tcgetattr(self.fd)
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        speed = getattr(termios, "B%d" % BAUD, termios.B115200)
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        iflag = 0
        oflag = 0
        lflag = 0
        cc = list(cc)
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 4  # 0.4s read timeout
        termios.tcsetattr(
            self.fd,
            termios.TCSANOW,
            [iflag, oflag, cflag, lflag, speed, speed, cc],
        )

    def write(self, data):
        return os.write(self.fd, data)

    def read(self, size=256):
        import select as _select

        ready, _, _ = _select.select([self.fd], [], [], 0.4)
        if not ready:
            return b""
        try:
            return os.read(self.fd, size)
        except Exception:
            return b""

    def close(self):
        try:
            os.close(self.fd)
        except Exception:
            pass


def open_serial(port):
    try:
        return _open_pyserial(port)
    except ImportError:
        pass
    except Exception as exc:
        raise SerialError(str(exc))

    if os.name == "nt":
        return _WindowsSerial(port)
    return _PosixSerial(port)


# --------------------------------------------------------- port discovery


def candidate_ports():
    """Best guesses first."""
    try:
        from serial.tools import list_ports

        ranked = []
        for info in list_ports.comports():
            score = 0
            if (info.vid, info.pid) in CH340_IDS:
                score = 2
            elif info.vid is not None:
                score = 1
            ranked.append((score, info.device))
        return [d for score, d in sorted(ranked, reverse=True) if score]
    except ImportError:
        pass

    if os.name == "nt":
        found = []
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM"
            )
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                # Skip Bluetooth virtual ports; they are never the board.
                if "BthModem" in name or "Bluetooth" in name:
                    continue
                found.append(value)
        except Exception:
            pass
        return found

    patterns = [
        # macOS. usbmodem covers anything with native USB, which is how the
        # ESP32-C3 presents itself; the rest are USB-to-serial bridges.
        "/dev/tty.usbmodem*",
        "/dev/tty.usbserial*",
        "/dev/tty.wchusbserial*",
        "/dev/tty.SLAB*",
        # Linux, including WSL when a device is passed through.
        "/dev/ttyACM*",
        "/dev/ttyUSB*",
    ]
    found = []
    for pattern in patterns:
        found.extend(sorted(glob.glob(pattern)))
    return found


def resolve_port(cfg, remember=False):
    """The board to talk to: --port, else the config, else autodetect.

    Autodetection deliberately does not write its answer back to the config.
    Caching it would pin the first board ever seen and quietly break the case
    this is for: unplugging one device and plugging in another.
    """
    if OPTIONS.get("port"):
        return OPTIONS["port"]
    if cfg.get("port"):
        return cfg["port"]
    found = candidate_ports()
    if not found:
        return None
    if remember:
        cfg["port"] = found[0]
        try:
            save_config(cfg)
        except Exception:
            pass
    return found[0]


# ------------------------------------------------------------- transports


def send_udp(cfg, line):
    payload = line.encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        target = cfg.get("host") or "255.255.255.255"
        sock.sendto(payload, (target, UDP_PORT))
        return True
    except Exception:
        return False
    finally:
        sock.close()


def send_serial(cfg, line, attempts=3):
    port = resolve_port(cfg)
    if not port:
        return False
    for attempt in range(attempts):
        try:
            conn = open_serial(port)
        except Exception:
            # Another hook fired at the same moment and holds the port.
            time.sleep(0.04 * (attempt + 1))
            continue
        try:
            conn.write((line + "\n").encode())
            return True
        except Exception:
            return False
        finally:
            conn.close()
    return False


def dispatch(cfg, line):
    # `enabled` is checked by the hook path alone: switching the indicator off
    # should stop it reacting to the session, not stop you driving it by hand.
    mode = cfg.get("transport", "auto")
    if mode == "off":
        return False
    if mode == "udp":
        return send_udp(cfg, line)
    if mode == "serial":
        return send_serial(cfg, line)
    # auto: the datagram is free, the cable is authoritative. Do both.
    ok = send_udp(cfg, line)
    return send_serial(cfg, line) or ok


# ------------------------------------------------------------ hook mapping

TOOL_STATES = {
    "Bash": "bash",
    "BashOutput": "bash",
    "PowerShell": "bash",
    "Edit": "edit",
    "Write": "edit",
    "NotebookEdit": "edit",
    "WebFetch": "web",
    "WebSearch": "web",
}

EVENT_STATES = {
    "SessionStart": "idle",
    "UserPromptSubmit": "thinking",
    "PostToolUse": "thinking",
    "PostToolBatch": "thinking",
    "SubagentStop": "thinking",
    "PermissionRequest": "ask",
    "PostToolUseFailure": "error",
    "StopFailure": "error",
    "Stop": "done",
    "SessionEnd": "gone",
}


def client_id(payload):
    host = socket.gethostname().split(".")[0]
    session = str(payload.get("session_id") or "cli")[:8]
    token = "%s:%s" % (host, session)
    return "".join(c for c in token if c.isalnum() or c in ":-_") or "unknown"


def derive_state(payload, override=None):
    event = payload.get("hook_event_name", "")

    if override and override != "auto":
        return override

    if event == "PreToolUse":
        return TOOL_STATES.get(payload.get("tool_name", ""), "tool")

    if event == "Notification":
        kind = payload.get("notification_type", "")
        return "ask" if kind in ("permission_prompt", "elicitation_dialog") else "idle"

    return EVENT_STATES.get(event, "thinking")


def read_payload():
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    try:
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


# -------------------------------------------------------------- commands


def cmd_hook(args):
    cfg = load_config()
    if not cfg.get("enabled", True):
        return 0
    payload = read_payload()
    state = derive_state(payload, args[0] if args else None)
    dispatch(cfg, "v1 %s %s" % (client_id(payload), state))
    return 0


def cmd_send(args):
    if not args:
        print("usage: claude_rgb.py send <state>")
        return 2
    cfg = load_config()
    cid = "%s:manual" % socket.gethostname().split(".")[0]
    ok = dispatch(cfg, "v1 %s %s" % (cid, args[0]))
    print("sent" if ok else "no transport available")
    return 0 if ok else 1


def talk(cfg, line, wait=2.0, until=None, port=None):
    """Send a control line over serial and collect the reply."""
    port = port or resolve_port(cfg)
    if not port:
        raise SerialError("no serial port found; set 'port' in %s" % CONFIG_PATH)
    conn = open_serial(port)
    try:
        conn.write((line + "\n").encode())
        deadline = time.time() + wait
        buf = b""
        while time.time() < deadline:
            chunk = conn.read(512)
            if chunk:
                buf += chunk
                if until and until.encode() in buf:
                    break
        return buf.decode(errors="replace")
    finally:
        conn.close()


def cmd_ports(args):
    cfg = load_config()
    in_use = resolve_port(cfg, remember=False)
    found = candidate_ports()
    if not found:
        print("no candidate serial ports found")
    for port in found:
        print("%s%s" % (port, "  <- in use" if port == in_use else ""))
    print("\nconfig: %s" % CONFIG_PATH)
    return 0


def cmd_status(args):
    cfg = load_config()
    print(talk(cfg, "v1 * status", wait=1.5, until="STATUS").strip())
    return 0


def cmd_brightness(args):
    if not args:
        print("usage: claude_rgb.py brightness <0-255>")
        return 2
    cfg = load_config()
    print(talk(cfg, "v1 * brightness %d" % int(args[0]), wait=1.0, until="OK").strip())
    return 0


def cmd_selftest(args):
    cfg = load_config()
    print("Pixel 0 should be RED (top-left), 4 GREEN (top-right), 20 BLUE (bottom-left).")
    talk(cfg, "v1 * selftest", wait=4.0, until="OK")
    answer = input("Does that match what you see? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        print("Layout is correct.")
        return 0
    print("\nPick the option that matches:")
    print("  1  red top-right, green top-left      -> rotate 1")
    print("  2  red bottom-right, green bottom-left -> rotate 2")
    print("  3  red bottom-left, green bottom-right -> rotate 3")
    print("  4  rows alternate direction            -> serpentine")
    choice = input("choice [1-4]: ").strip()
    if choice == "4":
        talk(cfg, "v1 * layout 1 0", wait=1.0, until="OK")
    elif choice in ("1", "2", "3"):
        talk(cfg, "v1 * layout 0 %s" % choice, wait=1.0, until="OK")
    else:
        print("skipped")
        return 1
    print("Layout updated. Run selftest again to confirm.")
    return 0


def cmd_setup(args):
    """Manage the board's Wi-Fi profiles over the serial cable.

    The password is typed by you into this terminal and goes straight down the
    cable to the board. It is never echoed, logged, or stored on this machine.
    """
    import base64
    import getpass

    cfg = load_config()
    port = resolve_port(cfg)
    if not port:
        print("No serial port found. Plug the board in, or set 'port' in %s"
              % CONFIG_PATH)
        return 1
    print("Using %s\n" % port)

    def b64(text):
        return base64.b64encode(text.encode()).decode()

    def unb64(text):
        try:
            return base64.b64decode(text).decode()
        except Exception:
            return "?"

    saved = []
    for line in talk(cfg, "v1 * netlist", wait=2.0, until="END netlist").splitlines():
        if line.startswith("NET "):
            saved.append(unb64(line[4:].strip()))

    print("Saved networks on the board:")
    if saved:
        for i, ssid in enumerate(saved, 1):
            print("  %d. %s" % (i, ssid))
    else:
        print("  (none)")

    print("\n  a  add a network")
    print("  d  delete a network")
    print("  q  quit")
    action = input("\nchoice: ").strip().lower()

    if action == "d":
        which = input("number to delete: ").strip()
        try:
            ssid = saved[int(which) - 1]
        except Exception:
            print("bad choice")
            return 1
        talk(cfg, "v1 * netdel %s" % b64(ssid), wait=1.5, until="OK")
        print("Removed %s" % ssid)
        return 0

    if action != "a":
        return 0

    print("\nScanning (takes a few seconds)...")
    found = []
    for line in talk(cfg, "v1 * scan", wait=8.0, until="END scan").splitlines():
        if line.startswith("SCAN "):
            parts = line.split()
            if len(parts) >= 3:
                found.append((unb64(parts[1]), parts[2]))

    if found:
        for i, (ssid, rssi) in enumerate(found, 1):
            print("  %2d. %-32s %s dBm" % (i, ssid, rssi))
        print("   0. type an SSID manually (hidden network)")
        pick = input("\nnetwork: ").strip()
        if pick == "0":
            ssid = input("SSID: ").strip()
        else:
            try:
                ssid = found[int(pick) - 1][0]
            except Exception:
                print("bad choice")
                return 1
    else:
        print("  (no networks found)")
        ssid = input("SSID: ").strip()

    if not ssid:
        return 1

    password = getpass.getpass("Password for %s (input hidden): " % ssid)
    reply = talk(cfg, "v1 * netadd %s %s" % (b64(ssid), b64(password)),
                 wait=14.0, until="IP ")
    del password

    if "IP " in reply:
        ip = reply.split("IP ", 1)[1].split()[0]
        print("\nConnected. Board IP on this network: %s" % ip)
        print("UDP broadcast will reach it from now on -- no IP needed in the config.")
        print("(For WSL, which is behind NAT, set \"host\": \"%s\" in %s)"
              % (ip, CONFIG_PATH))
    else:
        print("\nSaved, but it did not connect. Check the password and retry.")
        print(reply.strip())
    return 0


# ------------------------------------------------------------- deployment
#
# MicroPython's raw REPL, driven over the same serial layer as everything
# else. Saves depending on mpremote just to copy one file.


def _read_until(conn, token, timeout=6.0):
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = conn.read(512)
        if chunk:
            buf += chunk
            if token in buf:
                return buf
    raise SerialError("timed out waiting for %r (last saw %r)" % (token, buf[-60:]))


def _raw_enter(conn):
    conn.write(b"\r\x03\x03")  # two Ctrl-C: stop main.py if it is running
    time.sleep(0.3)
    conn.read(4096)            # drain whatever it printed on the way out
    conn.write(b"\r\x01")
    # The banner is "raw REPL; CTRL-B to exit\r\n>", so the prompt usually
    # lands in the same read. Only wait again if it genuinely hasn't.
    buf = _read_until(conn, b"raw REPL", 5.0)
    if b">" not in buf.split(b"raw REPL", 1)[1]:
        _read_until(conn, b">", 3.0)


def _raw_exec(conn, code):
    conn.write(code.encode() + b"\x04")
    resp = _read_until(conn, b"\x04>", 15.0)
    if not resp.startswith(b"OK"):
        raise SerialError("raw REPL refused the command: %r" % resp[:80])
    out, _, err = resp[2:-2].partition(b"\x04")
    if err.strip():
        raise SerialError(err.decode(errors="replace").strip())
    return out


def cmd_deploy(args):
    """deploy [device_dir_or_file] [remote_name]

    With no argument, uses the `device` key from the config. Pair it with
    --port when more than one board is plugged in.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = load_config()
    wanted = args[0] if args else cfg.get("device", "01space")

    if os.path.isfile(wanted):
        source = wanted
    elif os.path.isdir(os.path.join(here, wanted)):
        source = os.path.join(here, wanted, "main.py")
    else:
        source = wanted
    target = args[1] if len(args) > 1 else "main.py"

    if not os.path.exists(source):
        available = sorted(
            d for d in os.listdir(here)
            if os.path.isfile(os.path.join(here, d, "main.py"))
        )
        print("no such firmware: %s" % source)
        if available:
            print("known devices: %s" % ", ".join(available))
        return 1

    with open(source, "rb") as f:
        blob = f.read()

    port = resolve_port(cfg)
    if not port:
        print("no serial port found; set 'port' in %s" % CONFIG_PATH)
        return 1

    print("%s -> %s:%s  (%d bytes)" % (
        os.path.basename(source), port, target, len(blob)))

    conn = open_serial(port)
    try:
        _raw_enter(conn)
        _raw_exec(conn, "f = open(%r, 'wb')" % target)
        chunk_size = 512
        total = (len(blob) + chunk_size - 1) // chunk_size
        for i in range(total):
            chunk = blob[i * chunk_size:(i + 1) * chunk_size]
            _raw_exec(conn, "f.write(%r)" % chunk)
            done = int((i + 1) / total * 30)
            sys.stdout.write("\r  [%s%s] %d%%" % (
                "=" * done, " " * (30 - done), int((i + 1) / total * 100)))
            sys.stdout.flush()
        _raw_exec(conn, "f.close()")
        print()

        conn.write(b"\x02")   # leave raw REPL
        time.sleep(0.2)
        conn.write(b"\r\x04")  # soft reset: reruns boot.py and main.py
        time.sleep(0.3)
    finally:
        conn.close()

    time.sleep(1.5)
    try:
        print(talk(cfg, "v1 * status", wait=2.0, until="STATUS").strip())
    except Exception:
        print("uploaded; board did not answer a status query yet")
    return 0


HOOK_EVENTS = [
    ("SessionStart", None),
    ("UserPromptSubmit", None),
    ("PreToolUse", "*"),
    ("PostToolUse", "*"),
    ("PostToolUseFailure", "*"),
    ("PermissionRequest", "*"),
    ("Notification", None),
    ("Stop", None),
    ("StopFailure", None),
    ("SessionEnd", None),
]


def _is_ours(block):
    return any(
        "claude_rgb" in str(h.get("command", ""))
        for h in block.get("hooks", [])
    )


def _load_settings(settings_path):
    if not os.path.exists(settings_path):
        return {}
    with open(settings_path) as f:
        return json.load(f)


def cmd_install(args):
    """Merge our hooks into settings.json, leaving everything else alone.

    Never overwrites the file wholesale: other people's hooks, other events,
    and every other top-level key are carried through untouched. Re-running is
    safe -- our own previous entries are replaced rather than duplicated.
    """
    settings_path = args[0] if args else os.path.join(
        os.path.expanduser("~"), ".claude", "settings.json")
    script = os.path.abspath(__file__)
    interpreter = sys.executable or "python3"
    command = '"%s" "%s" hook' % (interpreter, script)

    try:
        settings = _load_settings(settings_path)
    except Exception:
        print("%s is not valid JSON; fix it before installing." % settings_path)
        return 1

    if os.path.exists(settings_path):
        backup = settings_path + ".bak"
        with open(backup, "w") as f:
            json.dump(settings, f, indent=2)
        print("Backed up existing settings to %s" % backup)

    hooks = settings.setdefault("hooks", {})
    kept = 0
    replaced = 0

    for event, matcher in HOOK_EVENTS:
        block = {"hooks": [
            {"type": "command", "command": command, "async": True, "timeout": 5}
        ]}
        if matcher:
            block["matcher"] = matcher

        preserved = []
        for existing in hooks.get(event, []):
            if _is_ours(existing):
                replaced += 1
            else:
                preserved.append(existing)
                kept += 1
        hooks[event] = preserved + [block]

    untouched_events = [e for e in hooks if e not in dict(HOOK_EVENTS)]
    other_keys = [k for k in settings if k != "hooks"]

    os.makedirs(os.path.dirname(os.path.abspath(settings_path)), exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    if not os.path.exists(CONFIG_PATH):
        save_config(dict(DEFAULT_CONFIG))

    print("Installed %d hooks into %s" % (len(HOOK_EVENTS), settings_path))
    if replaced:
        print("  replaced %d earlier claude-rgb entries" % replaced)
    print("  kept %d existing hook entries on those same events" % kept)
    if untouched_events:
        print("  left %d other event(s) untouched: %s"
              % (len(untouched_events), ", ".join(sorted(untouched_events))))
    if other_keys:
        print("  left %d other setting(s) untouched: %s"
              % (len(other_keys), ", ".join(sorted(other_keys))))
    print("Restart Claude Code to pick them up.")
    return 0


def cmd_uninstall(args):
    """Remove only our hooks, leaving every other entry in place."""
    settings_path = args[0] if args else os.path.join(
        os.path.expanduser("~"), ".claude", "settings.json")
    try:
        settings = _load_settings(settings_path)
    except Exception:
        print("%s is not valid JSON." % settings_path)
        return 1
    if not settings:
        print("nothing to do: %s does not exist" % settings_path)
        return 0

    backup = settings_path + ".bak"
    with open(backup, "w") as f:
        json.dump(settings, f, indent=2)

    hooks = settings.get("hooks") or {}
    removed = 0
    for event in list(hooks):
        kept = [b for b in hooks[event] if not _is_ours(b)]
        removed += len(hooks[event]) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
    print("Removed %d claude-rgb entries from %s" % (removed, settings_path))
    print("Backup at %s" % backup)
    return 0


def cmd_doctor(args):
    """Check every link in the chain, on whichever machine you're standing at."""
    ok = True

    def line(good, label, detail=""):
        print("  %s  %-34s %s" % ("OK  " if good else "FAIL", label, detail))

    print("\nconfig")
    cfg = load_config()
    line(True, "config file", CONFIG_PATH if os.path.exists(CONFIG_PATH)
         else "(not created yet, using defaults)")
    line(cfg.get("enabled", True), "enabled", str(cfg.get("enabled", True)))
    line(True, "transport", cfg.get("transport", "auto"))
    if not cfg.get("enabled", True):
        ok = False

    print("\nserial")
    found = candidate_ports()
    line(bool(found), "candidate ports", ", ".join(found) or "none found")
    port = resolve_port(cfg, remember=False)
    line(bool(port), "port in use", port or "-")

    board_seen = False
    if port:
        try:
            reply = talk(cfg, "v1 * status", wait=2.0, until="STATUS")
            board_seen = "STATUS" in reply
            line(board_seen, "board responds",
                 reply.strip().splitlines()[-1] if board_seen else "no reply")
        except Exception as exc:
            line(False, "board responds", str(exc))

    print("\nnetwork")
    if cfg.get("host"):
        line(True, "udp target", "%s:%d" % (cfg["host"], UDP_PORT))
    else:
        line(True, "udp target", "255.255.255.255:%d (broadcast)" % UDP_PORT)
    if not cfg.get("host"):
        print("       note: WSL is behind NAT and cannot broadcast to the LAN.")
        print("       Put the board on Wi-Fi and set \"host\" to its IP there.")

    print("\nhooks")
    settings_path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    installed = []
    try:
        with open(settings_path) as f:
            settings = json.load(f)
        for event, blocks in (settings.get("hooks") or {}).items():
            for block in blocks:
                for h in block.get("hooks", []):
                    if "claude_rgb" in str(h.get("command", "")):
                        installed.append(event)
    except Exception:
        pass
    line(bool(installed), "installed in settings.json",
         "%d events" % len(installed) if installed
         else "run: python claude_rgb.py install")
    if not installed:
        ok = False

    if installed:
        with open(settings_path) as f:
            raw = f.read()
        same = os.path.abspath(__file__).replace("\\", "\\\\") in raw
        line(same, "hook points at this file",
             "" if same else "settings.json references a different copy")
        if not same:
            ok = False

    print()
    if ok and board_seen:
        print("All good. Nothing to start -- Claude Code runs the hooks itself.")
    elif ok:
        print("Hooks are installed but the board did not answer.")
        print("Check the cable, or set \"port\" in %s" % CONFIG_PATH)
    else:
        print("Some checks failed; see above.")
    return 0 if ok else 1


COMMANDS = {
    "doctor": cmd_doctor,
    "hook": cmd_hook,
    "send": cmd_send,
    "setup": cmd_setup,
    "deploy": cmd_deploy,
    "status": cmd_status,
    "ports": cmd_ports,
    "brightness": cmd_brightness,
    "selftest": cmd_selftest,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
}


def main(argv):
    argv = list(argv)
    # A global --port, so any command can be aimed at one specific board.
    for flag in ("--port", "-p"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                OPTIONS["port"] = argv[i + 1]
                del argv[i:i + 2]

    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    handler = COMMANDS.get(argv[1])
    if handler is None:
        print("unknown command: %s" % argv[1])
        return 2
    try:
        return handler(argv[2:])
    except SerialError as exc:
        print("serial: %s" % exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    # A hook must never break the session, whatever goes wrong down here.
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:
        if len(sys.argv) > 1 and sys.argv[1] == "hook":
            sys.exit(0)
        print("error: %s" % exc)
        sys.exit(1)
