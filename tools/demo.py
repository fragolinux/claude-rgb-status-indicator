#!/usr/bin/env python3
"""Walk a device through its states, for demos and screen recordings.

    python tools/demo.py                 # default sequence, about 40 seconds
    python tools/demo.py --port COM9     # aim at one board of several
    python tools/demo.py 5               # same order, 5 seconds per state

Live sessions are cleared first: a real Claude Code session reporting its own
activity outranks most of these and would mask them for the whole recording.
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

CLIENT = "demo:sim"


def main(argv):
    argv = list(argv)
    for flag in ("--port", "-p"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                c.OPTIONS["port"] = argv[i + 1]
                del argv[i:i + 2]

    override = float(argv[1]) if len(argv) > 1 else None
    cfg = c.load_config()

    try:
        c.talk(cfg, "v1 * reset", wait=0.8, until="OK")
    except Exception as exc:
        print("could not reach the board: %s" % exc)
        return 1

    start = time.time()
    for state, seconds, note in SEQUENCE:
        if state == "idle":
            c.dispatch(cfg, "v1 %s gone" % CLIENT)
        else:
            c.dispatch(cfg, "v1 %s %s" % (CLIENT, state))
        print("[%5.1fs] %-9s %s" % (time.time() - start, state, note), flush=True)
        time.sleep(override or seconds)

    c.dispatch(cfg, "v1 %s gone" % CLIENT)
    print("[%5.1fs] end" % (time.time() - start))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
