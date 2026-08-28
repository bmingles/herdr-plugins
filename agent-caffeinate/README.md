# agent-caffeinate

A [Herdr](https://herdr.dev) plugin that keeps your machine awake for exactly as long as
your coding agents are working, and lets it sleep a minute after they stop.

Step away mid-task and the agent keeps running. Come back to a finished job rather than a
machine that dozed off thirty seconds after you left.

## Setup

Needs Herdr 0.8.0+ and a Python 3.9+ on the machine. Nothing else — no config file, no
build step, no dependencies. macOS and Linux.

### 1. Install

```sh
herdr plugin install bmingles/herdr-plugins/agent-caffeinate
```

### 2. Start it

The daemon starts itself whenever a Herdr server boots, so it is live the next time you
start Herdr. To start it right now without restarting, **switch to a different Space** —
that fires the hook that starts it. It has to be a real switch; re-focusing the Space you
are already on emits no event.

### 3. Check it

Every command lives at one fixed path, which the daemon writes when it starts:

```sh
~/.local/state/herdr/plugins/agent-caffeinate/agent-caffeinate doctor
```

Look for `daemon : running (pid …)`. If that file does not exist yet, the daemon has not
started — see [When nothing happens](#when-nothing-happens).

The rest of this README writes `agent-caffeinate` as shorthand for that path.

**That is the whole install** — three steps, no config file, nothing added to your `PATH`.
Everything below is optional.

> **Why the state directory and not the plugin directory?** A plugin installed from
> GitHub lives at `~/.config/herdr/plugins/github/agent-caffeinate-<hash>/…`, where the
> hash changes when you reinstall. The state directory never moves, so the daemon keeps a
> one-line shim there that points at wherever the plugin currently is. It is rewritten on
> every daemon start, so the path above keeps working after an upgrade.
>
> That path is the XDG default. If you set `XDG_STATE_HOME`, Herdr and this plugin both
> follow it; `doctor` prints the paths it actually resolved.

## Status indicator (optional)

An `☕ caffeinate` in the Herdr tab bar, shown only while the machine is actually being
held awake.

Herdr's config file is `~/.config/herdr/config.toml`. **Create it if it does not exist**
(`herdr --default-config > ~/.config/herdr/config.toml` writes the fully commented
default — do this only if you have no config file yet, it overwrites). Add:

```toml
[ui]
tab_bar_right = [
  { type = "command", command = "~/.local/state/herdr/plugins/agent-caffeinate/agent-caffeinate indicator", interval_seconds = 5, timeout_seconds = 2 },
]
tab_bar_right_separator = " · "
```

Then `herdr server reload-config`.

Paste that path verbatim — it is the same fixed shim from step 3, and Herdr runs the entry
through `/bin/sh -lc`, so `~` expands. Keep each `{ … }` entry on **one line**: TOML
inline tables cannot span newlines, and splitting one yields
``invalid inline table / expected `}` ``.

`tab_bar_right` defaults to `[]`, so this adds a status area rather than replacing one. To
show other things alongside it, list them in render order — `zoom`, `hostname`,
`datetime` (`format` defaults to `%H:%M`), `text` (`text = "…"`) and `command`, up to 16
entries:

```toml
tab_bar_right = [
  { type = "zoom" },
  { type = "command", command = "~/.local/state/herdr/plugins/agent-caffeinate/agent-caffeinate indicator", interval_seconds = 5, timeout_seconds = 2 },
  { type = "hostname" },
  { type = "datetime", format = "%H:%M" },
]
```

What it renders:

| Daemon | Renders |
| --- | --- |
| holding the assertion | `☕ caffeinate` |
| holding, but in dry mode (inhibitor command not on `PATH`) | `☕ caffeinate (dry)` |
| running, not holding | nothing — `--show-idle` renders `○ caffeinate` |
| wedged (alive, no longer making progress) | `⚠ caffeinate` |
| not running, or one poll old and yet to report | nothing |

"Nothing" is a real state rather than a blank gap: Herdr clears the entry on empty output,
and separators appear only between *visible* entries. That is why silence is the default —
the indicator answers "why is my machine awake", and a permanent `caffeinate: off` answers
nothing.

Flags: `--label TEXT` (default `caffeinate`), `--icon GLYPH` for the holding state,
`--show-idle`, and `--json` for the whole state object.

**It costs one file read.** The daemon already writes `daemon.json` every poll, so the
indicator reads that and the lock file and makes **no socket call**. Measured at ~32 ms per
run, essentially all Python interpreter startup — about 0.6% of a core at
`interval_seconds = 5`. Raise the interval if that bothers you; the underlying state
changes at most once per `pollIntervalSec` anyway.

Two more things worth knowing:

- **The entry resolves on the Herdr server**, with the same context a custom command
  keybinding gets — including `HERDR_SOCKET_PATH`, which is how the indicator finds the
  right session's state.
- **Plain text only, and this is enforced.** ANSI escapes in a `tab_bar_right` command's
  output make Herdr **hide the entry entirely** — tested by emitting SGR colour from an
  otherwise-working entry, which then vanished while the same script without escapes
  rendered fine. So colour has to come from the glyph: emoji carry their own. Use `--icon`
  for a louder one, e.g. `--icon 🔴 --label AWAKE`, if `☕ caffeinate` is easy to miss.

## Configuration (optional)

The plugin works with no config file. To change something, create:

```
~/.config/herdr/plugins/config/agent-caffeinate/config.json
```

The directory is created for you at install; the file is not. Comments and trailing commas
are allowed. [`config.example.json`](config.example.json) lists every key at its default.

| Key | Default | Meaning |
| --- | --- | --- |
| `idleGraceSec` | `60` | Seconds with no working agent before the assertion is released. See [Choosing `idleGraceSec`](#choosing-idlegracesec-from-data-not-vibes). |
| `pollIntervalSec` | `2` | How often the whole session is polled (one ~0.35 ms socket call). |
| `activeStatuses` | `["working"]` | Agent statuses that count as "working". |
| `inhibitorCommand` | `null` → platform default | The long-running command held while agents work. |
| `logLevel` | `"info"` | `error` \| `warn` \| `info` \| `debug`. |

The one people change is `inhibitorCommand`, to keep the display awake too:

```json
{ "inhibitorCommand": ["caffeinate", "-d", "-i", "-s"] }
```

A daemon holds its settings from the moment it started, and there is one per Herdr
session, so apply a config change everywhere:

```sh
for s in ~/.config/herdr/herdr.sock ~/.config/herdr/sessions/*/herdr.sock; do
  HERDR_SOCKET_PATH="$s" agent-caffeinate daemon --restart
done
```

(The registered "Restart caffeinate daemon" action only reaches the session that invokes
it.)

## Commands

All of these are `~/.local/state/herdr/plugins/agent-caffeinate/agent-caffeinate
<subcommand>`, written below as `agent-caffeinate`. None are needed in normal use — the
daemon runs on its own — so these are for when something looks wrong. If you do reach for
them often, symlink that shim into a directory on your `PATH`.

| Command | What it does |
| --- | --- |
| `agent-caffeinate status` | Daemon pid, uptime, whether it is holding, which panes are active, seconds until release |
| `agent-caffeinate status --json` | The same, machine-readable |
| `agent-caffeinate indicator` | One status line for a `ui.tab_bar_right` entry; empty when there is nothing to show |
| `agent-caffeinate indicator --json` | The same state, machine-readable |
| `agent-caffeinate doctor` | Resolved config, session key, whether the inhibitor command exists, live pane statuses |
| `agent-caffeinate stop` | Stop this session's daemon and release the assertion |
| `agent-caffeinate daemon --restart` | Restart it (also the plugin action "Restart caffeinate daemon") |
| `agent-caffeinate daemon --foreground` | Run attached with the log on stderr, for debugging |

There is deliberately **no `status` plugin action**: a plugin action's output reaches you
only through `herdr plugin log list`, JSON-escaped, which is the least readable channel
available. Run the command in a terminal instead.

## When nothing happens

| Symptom | Check |
| --- | --- |
| `~/.local/state/herdr/plugins/agent-caffeinate/agent-caffeinate` does not exist | The daemon has never started. `herdr plugin log list --plugin agent-caffeinate` is the only place hook output goes. |
| `doctor` says `daemon : not running` | Switch Spaces once, or restart Herdr. |
| `doctor` says `argv[0] resolves : NO` | Dry mode — no `caffeinate`/`systemd-inhibit` on the server's `PATH`. It tracks and logs but holds nothing. |
| The machine still sleeps | `status` names the panes it considers active. `blocked` does not count; see below. |

## What it actually does

A daemon polls the Herdr session. While any pane reports agent status `working`, it holds a
`caffeinate -i -s` process; `idleGraceSec` (default **60**) after the last agent stops
working, it kills it. That is the whole feature.

On Linux the default is
`systemd-inhibit --what=idle:sleep --why="herdr agent working" --mode=block sleep infinity`.

### Why `-i -s` and not `-dimsu`

The goal is "my agent's work is not interrupted while I step away" — nothing more.

| Flag | In the default? | Why |
| --- | --- | --- |
| `-i` | **yes** | Prevents system idle sleep. This is the flag that matters. |
| `-s` | **yes** | Prevents system sleep on AC power. Harmless alongside `-i`. |
| `-d` | no | Display only. A sleeping display never suspends a process, so it buys the agent nothing — and it leaves an unlocked screen unattended. Opt in via config if you want to watch progress from across the room. |
| `-m` | no | Disk idle sleep: a spinning-platter assertion, a no-op on an SSD. |
| `-u` | no | Without `-t` it declares the user active for a default of **5 seconds**, then expires, and wakes the display to do it. Useless as a long-lived assertion. |

`blocked` does not count as working, for the same reason: an agent waiting on a human
answer is not doing work that a sleeping machine would interrupt. Change that with
`activeStatuses` if you disagree.

## How it works, and why it is built this way

Everything here follows from measurements in
[`../docs/herdr-daemon-facts.md`](../docs/herdr-daemon-facts.md).

**It polls; it does not subscribe.** The obvious design is to subscribe to
`pane.agent_status_changed`. You cannot: that subscription requires a concrete, existing
`pane_id` — `*` and `""` are both rejected — so covering every agent would mean maintaining
one subscription per pane and rebuilding them as panes come and go. Since a whole-session
snapshot costs **0.35 ms**, polling every 2 s costs ~0.02% of a core and is both simpler
and more robust.

**Polling makes a missed event impossible.** Every poll reads the complete state, so it is
a re-seed by construction. An agent that exits without a final status event, or a pane
killed mid-task, simply stops appearing — where an event-driven design would leave that
pane stuck at `working` and hold the assertion forever.

**It watches for the server dying.** A plugin-spawned daemon *outlives its Herdr server*
(measured). Without handling that, killing your Herdr session would strand a `caffeinate`
process holding your Mac awake indefinitely, with nothing left running to explain why. A
failed connect is read as "server gone": release, then exit.

**One daemon per Herdr server.** Plugin registration is global across sessions, so every
session's server starts one. The singleton lock, log and state are namespaced by a hash of
`$HERDR_SOCKET_PATH`.

**It recovers from its own failures.** Two things can be left behind:

- *An orphaned `caffeinate`*, if a daemon is `kill -9`'d. Its pid is recorded to disk
  before anything else, so the next daemon kills it — but only if the pid is still alive
  *and* still matches our argv, so a recycled pid is never killed by mistake.
- *A wedged daemon* — alive but no longer making progress. This one matters more than it
  looks: the lock is keyed on the socket path, so on a Herdr restart the new daemon would
  find the lock held, exit silently, and leave that server with **no daemon at all,
  indefinitely**. So a starting daemon checks the holder's `updated_at`, which is rewritten
  every poll; if it is several intervals stale it waits to confirm, then displaces the
  holder and reaps its inhibitor.

  The confirm step exists because a laptop returning from sleep leaves a status file hours
  old and then catches up within one poll. Killing a daemon that was merely behind would be
  worse than the problem being fixed, so it looks twice and leaves anything that recovers
  alone. A holder that reports no status at all is never displaced.

`doctor` names all of this: it reports whether the daemon is running, wedged, or absent.

**If the inhibitor command does not exist** — Linux without `systemd-inhibit`, a
container — the daemon logs one warning and runs in **dry mode**: it tracks agents and logs
every transition it would have acted on, but spawns nothing.

### Why the tab bar and not the sidebar

Herdr's other custom-text surfaces — `pane.report_metadata` and
`workspace.report_metadata` tokens, rendered as `$name` in `ui.sidebar.*.rows` — are
per-pane and per-Space. Caffeinate state is one fact per Herdr server, so putting it on
every Space row would be duplicated noise. The tab bar status area is the only genuinely
global text surface, so that is where it goes.

Reusing Herdr's own agent `state_icon` is not possible, and deliberately not attempted: the
only way to obtain one is `pane.report_agent`, which makes the reporter a **full lifecycle
authority** and disables screen detection for that pane. This plugin reads `agent_status`
for a living, so poisoning it to draw a glyph would break the feature.

## Logs

One log per Herdr session, under the state directory:

```sh
tail -f ~/.local/state/herdr/plugins/agent-caffeinate/*/daemon.log
```

`status` and `doctor` print the exact path for the session you are in. Transitions are
greppable:

```
2026-08-27T09:12:03-05:00 info  inhibitor start pid=44120 argv=caffeinate -i -s trigger=w4:p2
2026-08-27T09:41:55-05:00 info  inhibitor stop pid=44120 reason=idle-grace idle_for=60.4s
2026-08-27T09:41:57-05:00 info  server gone (...); releasing and exiting
```

The log is capped at 1 MB with a single rollover to `daemon.log.1`.

### Choosing `idleGraceSec` from data, not vibes

The default is **60 s**. The number that matters is not "how long until I want it to stop" —
it is **how long a working agent falsely reads as idle**. Claude's detection carries a rule
literally named `default_known_agent_idle_fallback`: identity known, no rule matched. `idle`
there is an absence of evidence, not evidence the agent stopped. If `idleGraceSec` is
shorter than the longest such gap, the machine can sleep mid-task — the exact thing this
plugin exists to prevent.

The daemon records every status change with the duration of the status it left, at `info` —
so a normal run collects this with no configuration at all:

```
2026-08-27T09:12:03-05:00 info  status w4:p2 working -> idle (was working for 41.2s)
2026-08-27T09:12:11-05:00 info  status w4:p2 idle -> working (was idle for 8.4s)
```

A return to `working` after a stretch of not-working is a candidate false-idle gap.

These were originally `debug`, which turned out to be a mistake: the measurement only
existed when someone remembered to enable it, so the first five hours of real use logged 28
grace releases and not one gap. They are one line per pane status change — a few hundred a
day against a 1 MB cap — so they are worth having always.

Work normally for a day, then run the report from a checkout of this repo:

```sh
python3 tools/gap-report.py
```

```
   secs  kind        assn pane      session        composition            flags
  937.0s  idle-only   REL  w9:p4     abc123def456   done:869s idle:68s
  254.0s  prompt-wait REL  w9:p1     abc123def456   done:44s blocked:210s
   44.0s  idle-only   held w9:p1     abc123def456   done:44s               SUB-GRACE
    2.0s  idle-only   held w9:p1     abc123def456   done:2s                SUB-GRACE,poll-floor

idle-only gaps above the poll floor: 2 of 4
  [30,60): 1
held straight through the grace -- FALSE IDLES THE CURRENT SETTING SURVIVES: 1
  largest: 44.0s (w9:p1). This is the floor: do not set idleGraceSec below it.

THE DECISION BAND -- candidates in [30,60), where 30 differs from 60: 1
  44.0s  w9:p1  abc123def456  ended 2026-08-28 09:04:44
```

Read that as: the 937 s gap is lunch, the 254 s one is a permission prompt you took three
minutes to answer, the 2 s one is noise — and the **44 s** one is the whole finding. It is
idle-only, it was held straight through, and it sits in `[30, 60)`. Under a 30 s default the
machine would have been free to sleep there. That single row is why 30 would be unsafe on
this data, and why the floor is 44 rather than anything the other three rows suggest.

Three columns carry the argument:

- **`composition`** — which statuses the gap passed through. A `blocked` leg means *you*
  were the bottleneck, not detection; that is a prompt-wait and tells you nothing about
  `idleGraceSec`. Only `idle`/`done`-only gaps are candidates.
- **`assn`** — `held` means the assertion survived the gap; `REL` means it was released.
- **`SUB-GRACE`** — shorter than the grace, so detection lost the agent while the machine
  correctly stayed awake anyway. **These set the floor.** The largest one is the number that
  matters; go below it and you re-open a gap you currently cover.

**Read the durations, not just the flags.** A 900 s `idle-only` gap is you at lunch, which
is exactly what the plugin should treat as idle. Only gaps *within* a task you know was
running continuously are false idles.

**Expect `done`, not `idle`.** Herdr's `done` means "idle whose tab has not been seen in the
focused UI", and CLI/socket reads do not mark a tab seen — so this daemon, which only polls
the socket, sees `done` for any finished agent in a tab you have not personally clicked
into. `done` is the normal case here and `idle` the exception, and it is not a success
signal: it says no more than `idle` about whether the agent really stopped. Any
`grep 'idle -> working'` finds nothing. Details in
[`docs/herdr-daemon-facts.md`](../docs/herdr-daemon-facts.md) § C4.

**And mind the 2 s poll floor.** `pollIntervalSec` is 2, so brief gaps all read as exactly
`2.0s`. The report flags those `poll-floor`; ignore them.

Set `idleGraceSec` comfortably above the largest value you see. The cost of being too long
is nearly zero — we hold `-i -s`, so the display still sleeps and locks, and all you delay
is the start of macOS's own multi-minute idle countdown — while the cost of being too short
is an interrupted agent. So err generous.

**Why the default is 60.** A five-hour run on 2026-08-27 across two Herdr sessions produced
28 grace releases, 27 of them followed by an agent working again. Two of those gaps were
**62 s**: the assertion was released on the 60 s deadline and re-taken on the very next
poll. Nothing in that data suggests 60 is too generous, and two observations suggest it is
close to the edge, so 60 stands. That run could not measure gaps *shorter* than the grace,
because sub-grace gaps produced no log line — which is exactly why the status lines are now
at `info`. Whether a lower value such as 30 is safe is still unmeasured; do not lower it
without gaps to point at.

A daemon restart clears the transition journal's in-memory state, so gaps in flight across
it are lost — which is why this wants a day's data, not an hour's.

**A trap worth naming.** `workspace-time-tracker`'s `entries.jsonl` looks like independent
corroboration — a caffeinate gap falling inside a tracked entry looks like proof the agent
was busy. It is not. The tracker's active statuses default to `working` too, and it
deliberately never screen-hashes agent panes, so during a caffeinate gap its entry can only
be kept alive by a plain shell's changing screen or by a focus change. An entry spanning a
gap is evidence **you were at the keyboard**, not that the agent was working — and being at
the keyboard answering a permission prompt is precisely the `blocked` case. Use the status
lines, which name the status.

Note `blocked` is not an active status, so an agent waiting at a permission prompt is
already counting down. That is deliberate — nothing is in flight — but it means the grace
also covers "I stepped away while it was asking me something".

## Where things live

| What | Path |
| --- | --- |
| Config (optional, you create it) | `~/.config/herdr/plugins/config/agent-caffeinate/config.json` |
| Command shim, lock, log, daemon state | `~/.local/state/herdr/plugins/agent-caffeinate/` |
| Herdr's own config, for the tab bar entry | `~/.config/herdr/config.toml` |
| Plugin source (installed) | `~/.config/herdr/plugins/github/agent-caffeinate-<hash>/agent-caffeinate/` |

`herdr plugin uninstall agent-caffeinate` removes the plugin and its checkout. Config and
state are left in place; delete the two directories above by hand, plus any symlink or
shell function you pointed at the shim.

## Development

```sh
herdr plugin link ./agent-caffeinate       # run a working tree instead of an install
cd agent-caffeinate && python3 -m unittest discover -s test
```

91 tests, no network, no Herdr server, no macOS required. The daemon runs as a real
subprocess against a fake Herdr socket server (`test/fake_server.py`) and a fake inhibitor
(`test/fake-caffeinate`) that logs its own start/stop — so the spawn, kill, grace-period and
server-death paths are all exercised for real. Timing-sensitive logic lives in
`src/tracker.py`, which takes an injected clock and never sleeps.

The floor is **Python 3.9** (stock macOS ships 3.9.6): no `match`, no PEP 604 `X | Y`
runtime annotations, no `tomllib`. Nothing in the loop catches those but the target
interpreter.

**Note for the devcontainer in this repo:** it ships `python3-minimal`, which has no
`unittest` or `json`. Run `sudo apt-get install -y python3` first.
