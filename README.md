# claude-rgb

Claude Code activity indicator on a cheap ESP32 and some addressable LEDs.
Sits next to the monitor and shows, at a glance, what every Claude Code
session on the desk is doing — and shouts when one is waiting on you.

▶ **[THE SAME STATES ON BOTH BOARDS, SIDE BY SIDE](https://www.youtube.com/watch?v=2GypnoOGhAY)**
— 5×5 matrix and 8×1 strip, driven together.

---

## What it is trying to be

Five decisions shape the whole thing. They are worth stating up front, because
most of what looks like a limitation below is one of them.

**Readable without thinking.** Only the states you would actually act on get a
colour. There is no signal for which tool is running, how many turns have
passed, or which model is loaded. An indicator you have to decode is a
dashboard, and a dashboard on your desk is just more work — the moment you
need a legend, it has failed. Four meanings, four colours, one of them
reserved for "answer me".

**The same language on every device.** A 5×5 grid and an 8-pixel strip cannot
animate the same way, but they can mean the same thing. Colours are shared
exactly; timings are matched in milliseconds rather than in pixels, so two
boards side by side tick together. Learn it once.

**Two ways in: serial or Wi-Fi.** The cable always works and needs no setup —
plug it in and it runs. Wi-Fi frees the board from the machine, so it can live
on a charger and take reports from several computers at once. Serial is
required at least once, to tell the board about a network; after that either
path works, and the board falls back to the cable whenever no known network is
in range.

**Driven by Claude Code hooks, and nothing else.** No daemon, no polling, no
wrapper around your session. The harness already emits the events; the host
side is one script that turns an event into one line of text. It installs by
**merging** into your `settings.json`, never overwriting, so whatever hooks
you already run keep running.

**Portable across machines by default.** No `pip install` anywhere: `pyserial`
is used when present, otherwise a built-in `ctypes` / `termios` shim. The
serial port is autodetected rather than configured. Moving to another machine
is copying one file and running one command.

---

## Layout

```
claude_rgb.py        the host side: one stdlib-only file, shared by all boards
01space/main.py      firmware for the 5x5 matrix
supermini/main.py    firmware for the 8x1 strip
tools/demo.py        walk one board through every state
tools/compare.py     drive two boards through them at once, side by side
parked/              firmware for hardware not currently in use
```

The host never needs to know which board is plugged in: both firmwares speak
the same wire protocol and mean the same thing by every state. Only the
animations differ, to suit the shape of the display.

---

## The hardware

Either of these, or anything close to them.

### 01Space ESP32-PICO-D4-RGB

A [25×20 mm board](https://github.com/01Space/ESP32-PICO-D4-RGB) sold for a
few euros, with everything already on it — no wiring, no soldering.

| | |
|---|---|
| MCU | ESP32-PICO-D4, 240 MHz dual core, Wi-Fi + BT, 4 MB embedded flash |
| Display | 5×5 WS2812B-1515 matrix on **GPIO2** |
| USB | Type-C via a CH340E serial bridge |
| Buttons | **B** on GPIO0, **R** on EN |

Two quirks worth knowing before you flash it, neither documented upstream:

- **There is no auto-reset circuit.** DTR and RTS are not wired to EN and
  GPIO0, so esptool cannot enter the bootloader on its own — hold **B**, tap
  **R**, release **B**. The upside is the one this project depends on: opening
  the serial port never reboots the board, so hooks can write to it freely.
- **The vendor's pinout illustration mislabels the LEDs.** It points
  "LED IO27" at the blue LED on the front top corner, but that one sits across
  the 3V3 rail and cannot be switched off in software at all. The real GPIO27
  LED is on the back, beside the antenna. The
  [schematic PDF](https://github.com/01Space/ESP32-PICO-D4-RGB/tree/main/Schematic)
  is correct; the drawing is not.

### ESP32-C3 SuperMini + an 8×1 WS2812B strip

Three wires. The C3 exposes its USB natively, so it flashes and talks over the
same connector with no serial bridge in between.

| Strip | Board |
|---|---|
| GND | GND |
| VCC | **3V3** |
| DIN | **GPIO4** |

**Power the strip from 3.3 V, not 5 V.** A WS2812B wants a data high of
0.7 × VCC, and the C3 can only drive 3.3 V. At 5 V the threshold is 3.5 V and
the strip is out of spec: it usually works, then intermittently doesn't, with
flickering pixels and wrong colours that are miserable to diagnose. At 3.3 V
there is margin to spare. The cost is slightly less headroom at full
brightness, which an indicator never uses.

Avoid **GPIO2, 8 and 9**: they are boot strapping pins that must sit high at
reset, and an LED pulling one down drops the chip into download mode instead
of booting. **GPIO18/19** are the USB and **GPIO20/21** are UART0. That leaves
0, 1, 3, 4, 5, 6, 7 and 10.

### Anything else

Nothing here is specific to either board beyond `LED_PIN` and `NUM_LEDS` at
the top of each firmware. Any ESP32 running MicroPython with WS2812s attached
will do; pick whichever firmware has the closer geometry and adjust.

---

## There is nothing to start

This is the short answer to "how do I launch it?".

No daemon, no background process, no service, no autostart entry. The board
runs its own firmware and lights up as soon as it has power. The host side is
a script that Claude Code itself runs, because it is registered as a **hook**
in `~/.claude/settings.json`.

So: open a new Claude Code session, on any project, in the terminal or the
desktop app — the indicator just works. Close it, reboot, come back in a
month: still works. The only thing that has to be true is that the board is
plugged in.

If the board is unplugged, or you're on a laptop far from it, the hooks fail
silently and cost nothing. They are all `"async": true`, so they never block
or slow a session down whatever happens.

---

## Quick start on a new machine

Three steps, about a minute.

```bash
python claude_rgb.py install
```

```bash
python claude_rgb.py doctor
```

Then restart Claude Code, if it was already running.

`install` writes the hooks into `~/.claude/settings.json` (backing up any
existing file to `settings.json.bak`) and creates
`~/.claude/claude-rgb.json` with defaults. `doctor` verifies every link in
the chain and tells you exactly what is wrong if something is.

**No `pip install` on any machine.** `pyserial` is used if it happens to be
there; otherwise a built-in `ctypes` (Windows) / `termios` (macOS, Linux,
WSL) shim talks to the port directly. Only Python 3.6+ is required.

### Do not copy `settings.json` between machines

Copy **`claude_rgb.py`** — plus the firmware directory of whichever board you
use, if you want to edit or deploy it — then run `install` there. Never copy
`settings.json` across.

Two reasons. First, the hook command records the **absolute path of this
file** and the **absolute path of the Python interpreter** that ran it — both
correct on the machine that generated them, wrong everywhere else. Second,
your other machines have their own hooks in that file, and overwriting it
would destroy them.

If you later move `claude_rgb.py` somewhere else, re-run `install`. `doctor`
catches that case and says so.

### `install` merges, it does not overwrite

It reads the existing `settings.json`, adds its own entries, and writes the
result back. Specifically:

- Hooks belonging to anything else are **kept**, including on the same events
  claude-rgb uses. Both end up registered and both run.
- Events claude-rgb doesn't use (`PreCompact`, `SubagentStop`, …) are **not
  touched**.
- Every other top-level key (`model`, `permissions`, `env`, `statusLine`, …)
  is **carried through unchanged**.
- Re-running replaces claude-rgb's own previous entries rather than
  duplicating them, so it is safe to run as many times as you like.
- The previous file is copied to `settings.json.bak` first, every time.

`install` prints what it preserved so you can check:

```
Installed 10 hooks into /home/me/.claude/settings.json
  kept 2 existing hook entries on those same events
  left 1 other event(s) untouched: PreCompact
  left 3 other setting(s) untouched: env, model, permissions
```

To see exactly what it would do to a real file without risking it, run it
against a copy — both `install` and `uninstall` take an optional path:

```bash
cp ~/.claude/settings.json /tmp/probe.json
python claude_rgb.py install /tmp/probe.json
```

### Removing it

```bash
python claude_rgb.py uninstall
```

Strips only claude-rgb's entries and leaves every other hook, event and
setting exactly as it was.

---

## Per-machine notes

Support status, honestly: **Windows is tested and in daily use. macOS is
written for but not yet verified on hardware.** Linux should behave as macOS
does — both take the POSIX path — but has not been tried either. If macOS
gives you trouble, `python claude_rgb.py doctor` names the failing link.

### Windows

Works out of the box over USB. The port is autodetected: by VID/PID when
`pyserial` is present, otherwise by reading the `HARDWARE\DEVICEMAP\SERIALCOMM`
registry key and skipping Bluetooth virtual ports. Serial itself goes through
`ctypes` and the Win32 `DCB` structure when `pyserial` is absent.

Both boards enumerate differently and both are handled: the 01Space appears as
a **CH340** bridge, the C3 SuperMini as a native **USB Serial/JTAG** device
(VID `303A`).

### macOS

Autodetects `/dev/tty.usbserial*`, `/dev/tty.wchusbserial*`, `/dev/tty.SLAB*`
and `/dev/tty.usbmodem*`. Serial goes through `termios` when `pyserial` is
absent, with `flock` so two hooks firing at once cannot interleave on the port.

Two things to watch on a first run:

- The **CH340** bridge on the 01Space board may want
  [WCH's driver](http://www.wch-ic.com/downloads/CH341SER_EXE.html) on older
  macOS releases; recent ones include one. The C3 SuperMini needs no driver at
  all, being native USB.
- Use the `/dev/tty.*` node, not `/dev/cu.*`. Autodetection already picks
  `tty.*`; only relevant if you pin `port` by hand.

Everything else — hook installation, config, `deploy`, `setup` — is the same
as on Windows, and `install` writes the right absolute paths for the machine
it runs on.

### VS Code, Cursor and the other IDE extensions

Nothing to do. Hooks are a harness feature, not a terminal one, and the
extension reads the same `~/.claude/settings.json` as the CLI. Install once and
it works in the chat panel, the integrated terminal, and anywhere else Claude
Code runs on that machine.

### Dev containers and Codespaces

Treat the container as a separate machine, because that is what it is: its own
`~/.claude`, its own filesystem, its own network stack. Three things follow.

**Hooks have to live in the repository, not the home directory.** A container's
home is discarded on rebuild, so `install` into `~/.claude` has to be redone
every time. Install into the project instead:

```bash
python claude_rgb.py install --project
```

That writes `.claude/settings.json` in the repository, using
`${CLAUDE_PROJECT_DIR}` and a bare `python3` rather than absolute paths. Commit
it and the indicator survives rebuilds, and works for anyone else who opens the
same container.

**The container needs Python 3.** Most base images have it; a Node-only image
does not. Add it in the Dockerfile, or use a base image that includes it.

**Serial is not available, so plan the transport.**

| Host | What works |
|---|---|
| Linux | Pass the device through: `"runArgs": ["--device=/dev/ttyUSB0"]` in `devcontainer.json`. The cable then behaves normally. |
| Docker Desktop on Windows or macOS | No USB passthrough exists. Wi-Fi is the practical route. |
| GitHub Codespaces | Remote machine, no LAN access to your desk at all. The indicator cannot be reached; leave it uninstalled there. |

For the Wi-Fi route, the container reaches the LAN through Docker's NAT, so
**unicast works and broadcast does not** — the datagram never leaves the
bridge. Set the address explicitly, in the config *inside* the container:

```json
{ "host": "192.168.1.50", "transport": "udp" }
```

Reserve that address for the board on your router, or DHCP will move it.

### WSL

WSL is the one case that needs thought. It is a separate environment with its
own `~/.claude`, and it is behind NAT:

- **USB serial does not reach it** without `usbipd-win` passthrough.
- **UDP broadcast does not leave it** — the datagram stays on WSL's virtual
  switch and never reaches the LAN.
- **Outbound UDP unicast does work.**

So the WSL recipe is: put the board on Wi-Fi (see below), find its IP, and
set it explicitly.

```json
{ "host": "192.168.1.50", "transport": "udp" }
```

in `~/.claude/claude-rgb.json` **inside WSL**. Set a DHCP reservation for the
board on your router, or the IP will drift and you'll have to update this.

---

## Per-machine config — `~/.claude/claude-rgb.json`

```json
{
  "port": null,
  "host": null,
  "transport": "auto",
  "enabled": true
}
```

| Key | Meaning |
|---|---|
| `port` | Serial port. `null` autodetects and caches the result. Set `"COM8"` or `"/dev/tty.usbserial-10"` to pin it. |
| `host` | UDP target. `null` broadcasts, which needs no IP and survives moving between sites. Set an IP for WSL. |
| `transport` | `auto` (broadcast **and** cable), `serial`, `udp`, or `off`. |
| `enabled` | `false` silences the indicator without touching the hooks. |

---

## What the colours mean

Four meanings, four colours. They are the same on every device; only the
animation changes to suit the shape of the display.

| Meaning | Colour | Fires on |
|---|---|---|
| Something is running | **yellow** | `UserPromptSubmit`, `PreToolUse`, `PostToolUse` |
| Waiting for you to answer | **orange** | `PermissionRequest`, `Notification` |
| Something failed | **red** | `PostToolUseFailure`, `StopFailure` |
| The turn finished | **green → blue** | `Stop` |
| Nothing pending | dim white | `SessionStart`, idle |

Which tool is running is not shown. Bash, an edit and a web fetch all mean the
same thing to you — that Claude is busy — so spending a colour on the
difference would only dilute the four that matter.

White is never a meaning of its own, only the moving highlight on top of a
held colour. Idle is dim white for the same reason: it has to stay outside the
vocabulary.

### How each device says it

| Meaning | 8×1 strip | 5×5 matrix |
|---|---|---|
| Running | yellow comet, 5 lit and 3 dark, bouncing end to end | yellow comet of 5 running the rim |
| Waiting | 0.5s dark, then a 1.5s swell to full orange and back | orange panel breathing, white comet on the rim |
| Failed | 0.5s dark, then two full red beats through zero | red panel, white comet, both beating twice |
| Finished | 0.5s dark, then dark → green → blue → green → dark | blue panel breathing, green comet on the rim |
| Idle | dim white comet, slow | dim white comet on the rim, slow |

Step timings are scaled per geometry rather than shared, so a lap of the
16-pixel rim takes the same wall time as one bounce of the 14-step strip
cycle. Matching the step interval instead leaves the two ticking 14% apart,
which shows the moment you put them side by side.

Two shapes carry the emphasis. Every pulse starts from **real black** — the
firmware draws nothing at all for half a second rather than resting at the
bottom of a curve, because a curve that merely touches zero never quite goes
out. And **red beats twice**, through zero, where the others beat once: a
rhythm is recognised before a colour is.

### How the board decides what to show

It does not hold a current state that each message overwrites. It holds a
**list of sessions**, each with its own state and expiry, and draws the most
urgent one:

```
ask (90) > error (80) > running (50) > done (20) > idle (10)
```

So with several windows open — on several machines, if they share a board over
Wi-Fi — you always see the one that most needs you, and a permission prompt in
a window you had forgotten still gets through.

Two consequences worth knowing:

**`idle` cannot be forced.** It sits at the bottom of the scale, so
`send idle` adds a session that can never outrank a live one. To quiet the
panel you empty the list rather than adding a calm entry to it:

```bash
python claude_rgb.py reset
```

A long press on **B** does the same on the board itself.

**`done` is deliberately persistent.** It keeps pulsing until your next prompt,
so walking back after ten minutes still tells you the turn finished. Every
other state carries a TTL — five minutes for the working states — so a session
that dies never leaves the panel stuck.

---

## Everyday commands

```bash
python claude_rgb.py doctor         # is everything wired up on this machine?
python claude_rgb.py status         # what the board currently thinks
python claude_rgb.py send ask       # force a state, to see it
python claude_rgb.py reset          # forget every session; back to idle
python claude_rgb.py brightness 25  # 0-255, persisted on the board
python claude_rgb.py ports          # candidate serial ports
python claude_rgb.py selftest       # check and fix matrix orientation
python claude_rgb.py setup          # manage the board's Wi-Fi profiles
python claude_rgb.py wifi test      # prove datagrams really reach the board
python claude_rgb.py wifi off       # radio down now, back to the cable
python claude_rgb.py deploy 01space # push firmware and restart the board
python claude_rgb.py install        # merge the hooks into settings.json
python claude_rgb.py install --project   # ... into the repo, for dev containers
python claude_rgb.py uninstall      # remove only our hooks
```

Add `--port COM9` (or a device path) to any of them when more than one board
is plugged in.

### Going back to the cable

Four ways, from lightest to most permanent.

**Nothing at all.** With `transport` on `auto` the cable is always written
alongside the datagram. Take the board to a site whose network it does not
know and serial simply carries on.

**Radio off now, without a reboot:**

```bash
python claude_rgb.py wifi off
```

The saved profiles are untouched, so the board rejoins on its next power
cycle. Useful for a quick test or to rule Wi-Fi out of a diagnosis.

**Host-side only**, leaving the board on the network: set
`"transport": "serial"` in `~/.claude/claude-rgb.json`.

**Permanent**, the board forgets the network: run `setup`, choose `d`, pick
the entry. It will not rejoin on any boot.

### Turning it off

- Temporarily: set `"enabled": false` in `~/.claude/claude-rgb.json`. Takes
  effect on the next event, no restart. Manual `send` still works — the flag
  stops the indicator reacting to your session, not you driving it by hand.
- Permanently: `python claude_rgb.py uninstall`.
- Just this board: press and hold **B** to clear all sessions.

---

## Demo sequences

To see every state in order, or to record one:

```bash
python tools/demo.py
```

About 40 seconds, one state after another. Pass `--port` to aim at a
particular board, or a number to override the per-state duration.

To film two boards together and check that the same state reads the same way
on both geometries:

```bash
python tools/compare.py COM8 COM9
```

Three seconds of black, then both boards walk the sequence in step. It
silences the hooks for the duration and restores them on the way out, error or
Ctrl-C included — otherwise the live session writes real states over whatever
you are filming.

Both tools clear live sessions first. A real session reporting activity
outranks most of these states and would mask them for the whole recording.

---

## Security notes

Nothing secret belongs in this repository, and `.gitignore` is set up to keep
it that way. Two things to be aware of:

- **Wi-Fi credentials live on the board**, in `config.json` on its MicroPython
  filesystem, in plain text. That is the only place they are stored — `setup`
  reads the password with `getpass` and writes it straight down the cable, so
  it is never echoed, logged, or saved on the host. If you pull that file off
  the device, do not commit it; `.gitignore` already covers it. Anyone with
  physical access to the board can read it, so treat a shared board the way
  you would treat a shared router.
- **`~/.claude/settings.json` contains absolute paths**, including your home
  directory and username. It is outside this repository and should stay there.
  Don't paste it into issues without a look first.

---

## Serial or Wi-Fi

Both firmwares accept the same messages over either transport, and listen on
both at once. Which one carries a given message is decided by the host's
`transport` setting: `auto` (the default) sends the UDP datagram *and* writes
the cable, so whichever is live gets through.

| | Serial | Wi-Fi |
|---|---|---|
| Setup needed | none, plug it in | one-time, over serial |
| Board must be | attached to this machine | anywhere on the same network |
| Reporting machines | one | several at once |
| Survives moving between sites | yes | yes, if the site's network is saved |
| Works from WSL | no (USB is not passed through) | yes, with an explicit IP |

**Serial is required at least once.** A board straight out of the box knows no
networks, and the only way to tell it about one is down the cable.

### Setting up Wi-Fi, step by step

1. Connect the board by USB and check it answers:

```bash
python claude_rgb.py doctor
```

2. Run the setup tool:

```bash
python claude_rgb.py setup
```

3. It lists the networks already saved on the board, then offers to add one.
   Choose `a`.

4. It scans over the cable and prints what is in range, strongest first. Pick
   a number, or `0` to type a hidden SSID by hand.

5. It asks for the password with `getpass`, so nothing is echoed to the
   screen. What you type goes straight down the cable into the board's own
   `config.json`. It is never written on the host, never logged, and never
   passes through the shell history.

6. On success it prints the board's IP, then **tests that datagrams actually
   arrive** and tells you what to do about the result. See below.

Repeat once per site. The board keeps a **list** of profiles and joins
whichever one is in range at boot, so moving between home, office and
elsewhere needs no further action. With no known network in range it falls
back to serial silently. `status` reports the current IP, or `-` when offline.

To review or prune the list later, run `setup` again: it shows what is stored
and offers to delete an entry.

### Why setup tests, and does not just report an IP

An IP address proves almost nothing. The board can hold a perfectly good
address, answer pings, and still have nothing listening on the port — and
because UDP is unacknowledged, the sending side cannot tell the difference.
Any "sent" message from the host is a statement about the host, not about
delivery.

So the check crosses the two transports: it sends over UDP only, then reads
the board's own client list back **over the cable**. If the board registered
the datagram, it arrived. That is the only honest confirmation available, and
it is exactly the test that caught the bug this feature was born from.

Both paths are checked, and each result means something different:

| Result | What it means | What happens |
|---|---|---|
| broadcast reaches the board | Wi-Fi fully working | Nothing to configure. Broadcast needs no address, so moving between sites keeps working by itself |
| only unicast reaches it | The network isolates its clients — common on guest Wi-Fi and some phone hotspots | Setup offers to write `"host": "<ip>"` into the config. Reserve that address on your router, or DHCP will move it |
| neither reaches it | Wi-Fi is not usable here | Nothing breaks: `transport` is `auto`, so the cable carries on exactly as before. The profile is kept and retried at the next boot or site |

Run the same check at any time:

```bash
python claude_rgb.py wifi test
```

---

## Changing how it looks

Edit the firmware, then push it:

```bash
python claude_rgb.py deploy 01space
```

```bash
python claude_rgb.py deploy supermini
```

That's the whole cycle. It stops the running program, copies the file over
MicroPython's raw REPL, restarts the board, and prints its status to confirm
it came back. No `mpremote`, no extra install. With no argument it uses the
`device` key from the config; add `--port` when more than one board is
plugged in.

To push some other file, or under another name:

```bash
python claude_rgb.py deploy path/to/file.py remote_name.py
```

**`deploy` does not check syntax.** It copies bytes. A file that does not parse
will be installed happily, and the board then boots to a bare REPL with the
LEDs dark. Recovery is just deploying a fixed file — the raw REPL works whether
or not `main.py` runs — but it is worth gating the push on a compile:

```bash
python -m py_compile 01space/main.py && python claude_rgb.py deploy 01space
```

The knobs worth knowing, in each firmware:

| What | Where |
|---|---|
| Colours | the palette constants near the top |
| Which colour and animation a state uses | the `STATES` table |
| How long a state survives without an update | the TTL column of `STATES` |
| Animation shapes and speeds | `render()` — one branch per animation |
| Matrix orientation | `serpentine` / `rotate` in `config.json`, or `selftest` |
| Strip direction | `v1 * reverse 1`, or `selftest` |

If an edit ever leaves the board unable to boot, hold **B** while powering up:
`main.py` is skipped, you land in the REPL, and `deploy` still works.

---

## Board buttons

- **B held at boot** — skip `main.py` and drop to the REPL. A broken edit can
  never brick the board.
- **B short press** — cycle brightness.
- **B long press** — clear all sessions.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Panel dark, `doctor` says the board doesn't answer | Cable, or the wrong port. `ports` lists candidates; set `port` explicitly. |
| `doctor` says the hook points at a different copy | You moved `claude_rgb.py`. Re-run `install`. |
| Nothing happens in a new session | Claude Code was running when you installed. Restart it. |
| Panel stuck on one state | A session died mid-flight. `reset`, or long-press **B**, or wait for the TTL. |
| `send idle` does nothing | By design — `idle` is the lowest priority and cannot outrank a live session. Use `reset`. |
| Colours right, chase spins the wrong way | Run `selftest` and answer two questions. |
| Works on Windows, not in WSL | Expected — see the WSL note above. |
| `status` shows an IP, `send` says sent, board ignores it | Run `wifi test`. If broadcast fails and unicast works, the network isolates clients — set `host`. If both fail, the board holds an address with nothing listening. |
| Panel dark and unresponsive right after a `deploy` | The file you pushed does not parse. Deploy a fixed one; the raw REPL still works. Compile-check before pushing. |
| Wi-Fi joined but nothing arrives until a reboot | Fixed. If you are running firmware from before that fix, reboot the board and update it. |

---

## Reflashing MicroPython from scratch

Only needed if the board's filesystem is wrecked. This board has **no
auto-reset circuit**, so the bootloader is entered by hand: **hold B → press
and release R → release B**.

```bash
pip install esptool
python -m esptool --port COM8 --before no-reset --after no-reset \
    write-flash --erase-all 0x1000 ESP32_GENERIC-*.bin
```

Firmware from [micropython.org/download/ESP32_GENERIC](https://micropython.org/download/ESP32_GENERIC/).
Stay at the default 115200 baud — the CH340 on this board does not survive
the switch to 460800 mid-session, and you'll have to redo the button dance.

Then `python claude_rgb.py deploy` to put `main.py` back.

To return to the factory Arduino demo, flash any sketch from the
[01Space repo](https://github.com/01Space/ESP32-PICO-D4-RGB).

---

## How it fits together

```
any machine                                  the board
-----------                                  ---------
Claude Code
  |  fires a hook (async, non-blocking)
  v
claude_rgb.py hook
  |  maps event + tool name -> state
  |  "v1 <host>:<session> bash"
  |
  +--UDP broadcast :47281 ------------->  main.py
  +--serial 115200 -------------------->    arbitrates by priority
                                            renders on the 5x5
```

All the logic lives on the board: state machine, priority arbitration, TTLs,
animations, Wi-Fi profiles. The host side is one stdlib-only file whose whole
job is to write a single line.

### Wire protocol

```
v1 <client_id> <state> [ttl_ms]
v1 <client_id> gone
```

Control messages, addressed to client `*`. Serial only in practice, since they
answer on the console:

| Message | Effect |
|---|---|
| `brightness <0-255>` | set and persist panel brightness |
| `reset` | forget every reporting session |
| `status` | print `ip`, current state, session count, brightness, profile count |
| `scan` | print networks in range, base64 SSID and dBm |
| `netlist` | print saved profiles |
| `netadd <ssid_b64> <pass_b64>` | save a profile and reconnect |
| `netdel <ssid_b64>` | forget a profile |
| `wifi` | retry the connection now |
| `wifioff` | radio down until the next power cycle |
| `selftest` | light known pixels, to check orientation |
| `layout <serpentine> <rotate>` | remap the matrix (5×5 only) |
| `reverse <0\|1>` | flip which end the strip starts from (strip only) |

SSIDs and passwords are base64 so spaces and non-ASCII survive a
whitespace-delimited protocol.

---

## Technology

Chosen for the same reason throughout: the fewest moving parts that still do
the job.

| Layer | What | Why this one |
|---|---|---|
| Firmware language | **MicroPython** 1.28 | Push a file and reset — no toolchain, no compile, no upload cycle. On the ESP32-C3 that is a two-second edit-to-see loop. CircuitPython was the alternative but barely supports ESP32 "classic" parts like the PICO-D4 |
| Host language | **Python 3.6+, standard library only** | Every machine here already has it. No `pip install` means nothing to keep in sync across four computers |
| Serial | `pyserial` when present, otherwise `ctypes` + Win32 `DCB`, or `termios` + `flock` | Keeps the zero-dependency promise without giving up `pyserial`'s VID/PID detection when it happens to be installed |
| Firmware upload | MicroPython's **raw REPL**, driven over the same serial layer | `mpremote` would have been one more thing to install on every machine to copy one file |
| Network transport | **UDP broadcast**, port 47281 | Needs no address, so moving between sites changes nothing. Fire-and-forget: no handshake to block a hook, no connection to fail |
| Integration | **Claude Code hooks**, `command` type, `async: true` | Async is the whole point — the hook cannot slow a session down, whatever the state of the cable or the network. `http` hooks were the obvious alternative but do not support `async`, so an absent board would stall every event until timeout |
| LEDs | **WS2812B**, driven by the `neopixel` module | One data pin for any number of pixels, and 24-bit colour makes the whole vocabulary a table of constants |

The board does the thinking. State machine, priority arbitration between
sessions, expiry timers and every animation live in the firmware. The host
side maps a hook event to one word and writes a single line — which is what
makes it small enough to be dependency-free, and what lets both firmwares
share one host without knowing anything about each other.

---

## Credits

The board is the **ESP32-PICO-D4-RGB** by [01Space]. Their repository is where
the hardware documentation lives — schematic, pinout, the original Arduino
example sketches, and the CH340 driver link:

**https://github.com/01Space/ESP32-PICO-D4-RGB**

This project only replaces what runs on the board. To go back to the factory
Arduino demo, flash any sketch from that repository.

Built on [MicroPython](https://micropython.org/) and driven by
[Claude Code](https://claude.com/claude-code) hooks.

[01Space]: https://github.com/01Space

## Licence

MIT — see [LICENSE](LICENSE).
