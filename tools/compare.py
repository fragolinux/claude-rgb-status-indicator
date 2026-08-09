#!/usr/bin/env python3
"""Drive two boards through the same states at once, side by side.

    python tools/compare.py COM8 COM9
    python tools/compare.py /dev/tty.usbserial-10 /dev/tty.usbmodem101

A test harness, not a feature: the indicator itself only ever talks to one
board. This exists to check that the same state reads the same way on two
different geometries, and to film them together.

Hooks are silenced for the duration and restored on the way out, including
after an error or Ctrl-C -- otherwise the live session would keep writing real
states over the sequence being filmed.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_rgb as c  # noqa: E402

# state, seconds, what the viewer should be looking at
SEQUENCE = [
    ("thinking", 8, "yellow chase - something is running"),
    ("ask", 8, "orange pulse - waiting for you to answer"),
    ("error", 9, "red double beat - something failed"),
    ("done", 8, "green to blue pulse - the turn finished"),
    ("idle", 6, "dim white chase - nothing pending"),
]

LEAD_IN = 3
CLIENT = "demo:sim"


def send(ports, line):
    for port in ports:
        c.OPTIONS["port"] = port
        c.dispatch(c.load_config(), line)
    c.OPTIONS.pop("port", None)


def main(argv):
    ports = argv[1:]
    if len(ports) < 2:
        print(__doc__)
        return 2

    cfg = c.load_config()
    was_enabled = cfg.get("enabled", True)
    cfg["enabled"] = False
    c.save_config(cfg)

    try:
        for port in ports:
            c.OPTIONS["port"] = port
            try:
                c.talk(cfg, "v1 * reset", wait=0.8, until="OK")
            except Exception as exc:
                print("%s did not answer: %s" % (port, exc))
                return 1
        c.OPTIONS.pop("port", None)

        print("boards: %s" % ", ".join(ports))
        print("dark for %ds, then %d states\n" % (LEAD_IN, len(SEQUENCE)))
        # Explicitly `off`, not just cleared: with no sessions reporting, the
        # firmware falls back to `idle` and shows its dim white chase. Only
        # `off` gives a genuinely black lead-in to cut the footage on.
        send(ports, "v1 %s off" % CLIENT)
        time.sleep(LEAD_IN)

        start = time.time()
        for state, seconds, note in SEQUENCE:
            send(ports, "v1 %s %s" % (CLIENT, "gone" if state == "idle" else state))
            print("[%5.1fs] %-9s %s" % (time.time() - start, state, note), flush=True)
            time.sleep(seconds)

        send(ports, "v1 %s gone" % CLIENT)
        print("[%5.1fs] end" % (time.time() - start))
        return 0
    finally:
        cfg = c.load_config()
        cfg["enabled"] = was_enabled
        c.save_config(cfg)
        print("hooks restored to enabled=%s" % was_enabled)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
