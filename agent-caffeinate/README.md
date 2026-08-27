# agent-caffeinate

A [Herdr](https://herdr.dev) plugin that keeps your machine awake for exactly as long as
your coding agents are working, and lets it sleep a minute after they stop.

Step away mid-task and the agent keeps running. Come back to a finished job rather than a
machine that dozed off thirty seconds after you left.

Python 3.9+, standard library only, no build step, no dependencies.

## What it actually does

A daemon polls the Herdr session. While any pane reports agent status `working`, it holds
a `caffeinate -i -s` process; `idleGraceSec` (default **60**) after the last agent stops
working, it kills it. That is the whole feature.

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

## Install

```sh
herdr plugin install bmingles/herdr-plugins/agent-caffeinate
```

Or, while developing:

```sh
herdr plugin link ./agent-caffeinate
```

**No config file is required.** Unlike its sibling `vscode-workspace-sync`, this plugin
has sensible defaults for everything and starts working as soon as a Herdr server starts.

The daemon starts on **server boot**, not on `plugin link`, so after linking either
restart your session or switch to a different Space — the `workspace.focused` hook starts
it too. That hook needs a *genuine* focus change; re-focusing the Space you are already
on emits no event.

Check it:

```sh
./bin/agent-caffeinate doctor     # resolved config, socket, whether caffeinate resolves
./bin/agent-caffeinate status     # is it holding right now, and what is keeping it up
```

## Configuration

Optional, at `herdr plugin config-dir agent-caffeinate` → `config.json`. Comments and
trailing commas are allowed. See [`config.example.json`](config.example.json) for every
key with its default.

The one people change: `"inhibitorCommand": ["caffeinate", "-d", "-i", "-s"]` to keep the
display awake too.

## Commands

| Command | What it does |
| --- | --- |
| `bin/agent-caffeinate status` | Daemon pid, uptime, whether it is holding, which panes are active, seconds until release |
| `bin/agent-caffeinate status --json` | The same, machine-readable |
| `bin/agent-caffeinate doctor` | Resolved config, session key, whether the inhibitor command exists, live pane statuses |
| `bin/agent-caffeinate stop` | Stop this session's daemon and release the assertion |
| `bin/agent-caffeinate daemon --restart` | Restart it (also available as the plugin action "Restart caffeinate daemon") |
| `bin/agent-caffeinate daemon --foreground` | Run attached with the log on stderr, for debugging |

There is deliberately **no `status` plugin action**: a plugin action's output reaches you
only through `herdr plugin log list`, JSON-escaped, which is the least readable channel
available. Run the command in a terminal instead.

## How it works, and why it is built this way

Everything here follows from measurements in
[`../docs/herdr-daemon-facts.md`](../docs/herdr-daemon-facts.md).

**It polls; it does not subscribe.** The obvious design is to subscribe to
`pane.agent_status_changed`. You cannot: that subscription requires a concrete, existing
`pane_id` — `*` and `""` are both rejected — so covering every agent would mean
maintaining one subscription per pane and rebuilding them as panes come and go. Since a
whole-session snapshot costs **0.35 ms**, polling every 2 s costs ~0.02% of a core and is
both simpler and more robust.

**Polling makes a missed event impossible.** Every poll reads the complete state, so it
is a re-seed by construction. An agent that exits without a final status event, or a pane
killed mid-task, simply stops appearing — where an event-driven design would leave that
pane stuck at `working` and hold the assertion forever.

**It watches for the server dying.** A plugin-spawned daemon *outlives its Herdr server*
(measured). Without handling that, killing your Herdr session would strand a `caffeinate`
process holding your Mac awake indefinitely, with nothing left running to explain why. A
failed connect is read as "server gone": release, then exit.

**One daemon per Herdr server.** Plugin registration is global across sessions, so every
session's server starts one. The singleton lock, log and state are namespaced by a hash
of `$HERDR_SOCKET_PATH`.

**If the inhibitor command does not exist** — Linux without `systemd-inhibit`, a
container — the daemon logs one warning and runs in **dry mode**: it tracks agents and
logs every transition it would have acted on, but spawns nothing.

## Logs

```sh
tail -f "$(herdr plugin config-dir agent-caffeinate | sed 's|/config/|/../../state/herdr/plugins/|')"/*/daemon.log
```

Or just read the path that `status` and `doctor` print. Transitions are greppable:

```
2026-08-27T09:12:03-05:00 info  inhibitor start pid=44120 argv=caffeinate -i -s trigger=w4:p2
2026-08-27T09:41:55-05:00 info  inhibitor stop pid=44120 reason=idle-grace idle_for=60.4s
2026-08-27T09:41:57-05:00 info  server gone (...); releasing and exiting
```

The log is capped at 1 MB with a single rollover to `daemon.log.1`.

### Choosing `idleGraceSec` from data, not vibes

The default is **60 s**. The number that matters is not "how long until I want it to
stop" — it is **how long a working agent falsely reads as idle**. Claude's detection
carries a rule literally named `default_known_agent_idle_fallback`: identity known, no
rule matched. `idle` there is an absence of evidence, not evidence the agent stopped. If
`idleGraceSec` is shorter than the longest such gap, the machine can sleep mid-task —
the exact thing this plugin exists to prevent.

At `"logLevel": "debug"` the daemon records every status change with the duration of the
status it left:

```
2026-08-27T09:12:03-05:00 debug status w4:p2 working -> idle (was working for 41.2s)
2026-08-27T09:12:11-05:00 debug status w4:p2 idle -> working (was idle for 8.4s)
```

A `idle -> working (was idle for Ns)` line **inside** a task you know was running the
whole time is a false-idle gap of N seconds. Run a few real tasks, then:

```sh
grep 'idle -> working' daemon.log | sed 's/.*was idle for //' | sort -rn | head
```

Set `idleGraceSec` comfortably above the largest value you see. If your gaps are all
small, a shorter grace such as 30 is safe; the default is 60 because the cost of being
too long is nearly zero (we hold `-i -s`, so the display still sleeps and locks, and all
you delay is the start of macOS's own multi-minute idle countdown) while the cost of
being too short is an interrupted agent.

Note `blocked` is not an active status, so an agent waiting at a permission prompt is
already counting down. That is deliberate — nothing is in flight — but it means the grace
also covers "I stepped away while it was asking me something".

## Tests

```sh
cd agent-caffeinate && python3 -m unittest discover -s test
```

58 tests, no network, no Herdr server, no macOS required. The daemon runs as a real
subprocess against a fake Herdr socket server (`test/fake_server.py`) and a fake
inhibitor (`test/fake-caffeinate`) that logs its own start/stop — so the spawn, kill,
grace-period and server-death paths are all exercised for real. Timing-sensitive logic
lives in `src/tracker.py`, which takes an injected clock and never sleeps.

**Note for the devcontainer in this repo:** it ships `python3-minimal`, which has no
`unittest` or `json`. Run `sudo apt-get install -y python3` first.
