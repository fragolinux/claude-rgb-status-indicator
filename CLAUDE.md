# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Claude Code activity indicator on an ESP32 with addressable LEDs. There is no
daemon and nothing to start: Claude Code's own hooks run `claude_rgb.py hook`,
which turns a hook event into one line of text sent to the board over serial
and/or UDP. The board's firmware holds the state and animates it.

## Commands

There is no test suite, no linter, and no build step. Verification is done
against real hardware.

```bash
python -m py_compile 01space/main.py     # ALWAYS before deploying: `deploy` copies
                                         # bytes without parsing them
python claude_rgb.py deploy 01space      # push firmware, soft-reset, print status
python claude_rgb.py deploy supermini
python claude_rgb.py deploy path/to/file.py remote_name.py

python claude_rgb.py doctor              # checks every link in the chain; the
                                         # first thing to run when something is off
python claude_rgb.py status              # what the board currently thinks
python claude_rgb.py send ask            # force a state by hand
python claude_rgb.py reset               # clear all sessions (see "idle" below)
python claude_rgb.py ports               # candidate serial ports

python tools/demo.py                     # walk one board through every state (~40s)
python tools/compare.py COM8 COM9        # drive two boards in step, for filming
```

`--port <path>` works on any subcommand when more than one board is attached.

Install/uninstall take an optional settings path, so changes to the merge logic
can be exercised against a copy instead of the real file:

```bash
cp ~/.claude/settings.json /tmp/probe.json && python claude_rgb.py install /tmp/probe.json
```

## Architecture

Three layers, joined by one text protocol.

**Host — `claude_rgb.py`** (single file, stdlib only, Python 3.6+). Registered
as a hook on ten events. `derive_state()` maps `hook_event_name` (plus
`tool_name` for `PreToolUse`) to a state name; `client_id()` builds
`<hostname>:<session_id[:8]>`; `dispatch()` writes the line over every enabled
transport. `transport: auto` sends UDP *and* serial, so whichever is live wins.

**Wire protocol** — newline-terminated ASCII, identical over serial and UDP
(port 47281), identical on both firmwares:

```
v1 <client_id> <state> [ttl_ms]     report a state for one session
v1 * <command> [args...]            control: brightness, reset, status, scan,
                                    netlist, netadd, netdel, wifi, wifioff,
                                    layout, selftest
```

SSIDs and passwords in `netadd`/`netdel`/`netlist`/`scan` are base64 (`b64e`/`b64d`)
so spaces and non-ASCII survive the line-oriented parser. Control replies are
printed on serial only (`OK …`, `ERR …`, `STATUS …`, or a block terminated by
`END <cmd>`), which is why UDP verification has to read the result back over the
cable — see `verify_udp()`.

**Firmware — `01space/main.py` (5×5 matrix, GPIO2) and `supermini/main.py`
(8×1 strip, GPIO4).** MicroPython. The board does *not* hold a single current
state: `clients` maps client id → `[state, expiry]`, and `winner()` draws the
highest-priority live entry. Several sessions on several machines can share one
board over Wi-Fi and the most urgent one shows.

### Invariants that span files

- **`STATES` priority ordering is the arbitration.** `ask (90) > error (80) >
  running (50/40) > done (20) > idle (10)`. Consequence: `send idle` can never
  outrank a live session — to quiet the panel you clear the list (`reset`, or a
  long press on **B**), never by adding a calm state.
- **The palette is shared exactly across firmwares; timings are matched in
  milliseconds, not in steps.** A 16-pixel rim lap and a 14-step strip bounce
  are tuned to the same wall time; matching `step_ms` instead leaves two boards
  ticking 14% apart, visible side by side. Any change to a colour or a state's
  meaning must land in *both* `01space/main.py` and `supermini/main.py`.
- **`done` is deliberately persistent** (1h TTL) — walking back later must still
  show the turn finished. Working states carry a 5-minute TTL so a dead session
  never leaves the panel stuck.
- **Every pulse starts from real black**, and red beats *twice* where the others
  beat once. Both are deliberate: a curve that merely touches zero never reads
  as "out", and a rhythm is recognised before a colour is.
- **No `pip install`, anywhere.** `pyserial` is used if present; otherwise the
  `_WindowsSerial` (ctypes/DCB) and `_PosixSerial` (termios + flock) shims in
  `claude_rgb.py` talk to the port directly. Keep both paths working.
- **Only four meanings get a colour.** Resist adding a signal for which tool is
  running, turn counts, model, etc. — `TOOL_STATES` collapses every tool to the
  same yellow on purpose.

### Host-side subtleties worth not re-deriving

- `broadcast_sources()` probes the route table for a handful of private
  destinations instead of enumerating interfaces: a bare broadcast leaves by
  whichever interface routing prefers, which on a machine with a VPN or WSL
  bridge is usually the wrong one. `gethostbyname_ex` measured 50 ms — far too
  slow for something that runs on every hook.
- `dispatch()` returns `(transport, ok)` pairs, and UDP "ok" means *sent*, not
  *delivered*. Never report a UDP send as confirmation the board got it.
- `send_serial()` retries three times: concurrent hooks contend for the port.
- `install` **merges** into `settings.json` (other hooks, other events, other
  top-level keys preserved; own entries replaced, not duplicated; `.bak` written
  every time). `_is_ours()` identifies our entries by `"claude_rgb"` in the
  command string. All hooks are `"async": true` so a missing board costs nothing.
- `install --project` writes `.claude/settings.json` with `${CLAUDE_PROJECT_DIR}`
  and a bare `python3`; plain `install` bakes in absolute paths for both the
  script and the interpreter. Hence: never copy `settings.json` between
  machines, and re-run `install` after moving `claude_rgb.py` (`doctor` catches
  this).

## Working on this repo

- If the hooks are installed on this machine and point at this checkout, editing
  `claude_rgb.py` changes code that fires on every event of the live session. A
  broken edit shows up as hook noise, not as a test failure.
- `.gitattributes` forces LF everywhere. The repo is cloned on Windows and macOS
  and its Python is pushed verbatim onto the board; CRLF churn would be pure diff
  noise.
- `config.json` (board-side, holds Wi-Fi passwords in plain text) and
  `claude-rgb.json` are gitignored. Keep it that way.
- `parked/` holds firmware for hardware not in use — working code, kept for
  reference; not part of the normal deploy path.
- The 01Space board has **no auto-reset circuit**: the bootloader is entered by
  hand (hold **B**, tap **R**, release **B**). The upside the whole design leans
  on — opening the serial port never reboots it, so hooks can write freely.
- Recovery from any bad firmware edit: hold **B** at boot to skip `main.py`; the
  raw REPL, and therefore `deploy`, still works.
