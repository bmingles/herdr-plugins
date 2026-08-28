# workspace-time-tracker

A [Herdr](https://herdr.dev) plugin that records how long you actually spend in each Space.
An entry opens when you start working in a Space, closes when you switch, and closes
**backdated to your last sign of activity** after a minute of quiet.

```
$ track report
2026-08-27  (today)
  herdr-plugins          3h 12m
  deephaven-core         0h 48m
  ------------------------------
  total                  4h 00m
```

## Setup

Needs Herdr 0.8.0+ and a Python 3.9+ on the machine. Nothing else — no config file, no
build step, no dependencies. macOS and Linux.

### 1. Install

```sh
herdr plugin install bmingles/herdr-plugins/workspace-time-tracker
```

### 2. Start it

The daemon starts itself whenever a Herdr server boots, so it is live the next time you
start Herdr. To start it right now without restarting, **switch to a different Space** —
that fires the hook that starts it. It has to be a real switch; re-focusing the Space you
are already on emits no event.

### 3. Check it

The `track` command lives at one fixed path, which the daemon writes when it starts:

```sh
~/.local/state/herdr/plugins/workspace-time-tracker/track doctor
```

Look for a `focused Space` line naming the Space you are in. If that file does not exist
yet, the daemon has not started — see [When nothing happens](#when-nothing-happens).

The rest of this README writes `track` as shorthand for that path.

**That is the whole install** — three steps, no config file, nothing added to your `PATH`.
It starts recording immediately. Everything below is optional.

> **Why the state directory and not the plugin directory?** A plugin installed from GitHub
> lives at `~/.config/herdr/plugins/github/workspace-time-tracker-<hash>/…`, where the hash
> changes when you reinstall. The state directory never moves, so the daemon keeps a
> one-line shim there that points at wherever the plugin currently is. It is rewritten on
> every daemon start, so the path above keeps working after an upgrade.
>
> That path is the XDG default. If you set `XDG_STATE_HOME`, Herdr and this plugin both
> follow it; `doctor` prints the paths it actually resolved.

## Commands

All of these are `~/.local/state/herdr/plugins/workspace-time-tracker/track <subcommand>`,
written below as `track`. `track report` is the one you will run daily, so it is worth
making short: symlink that shim into a directory on your `PATH` —
`ln -s ~/.local/state/herdr/plugins/workspace-time-tracker/track ~/.local/bin/track`. Not
required.

| Command | What it does |
| --- | --- |
| `track report` | Today, grouped by Space label |
| `track report --day yesterday` / `--day 2026-08-01` | One specific day |
| `track report --since 2026-08-01 [--until 2026-08-27]` | An inclusive range |
| `track report --by label\|workspace\|day` | Grouping (default `label`) |
| `track report --json` | Machine-readable; this shape is the contract |
| `track status` | What is being tracked now, elapsed, and time until it closes |
| `track flush` | Close the open entry immediately (also a plugin action) |
| `track stop` | Stop the daemon, writing the open entry first |
| `track doctor` | Resolved config, entry count, live focused Space |

There is deliberately **no `report` plugin action**: its entire value is legible output, and
a plugin action's stdout reaches you only through `herdr plugin log list`, JSON-escaped.
`report` is a terminal command.

## Configuration (optional)

The plugin works with no config file. To change something, create:

```
~/.config/herdr/plugins/config/workspace-time-tracker/config.json
```

The directory is created for you at install; the file is not. Comments and trailing commas
are allowed. [`config.example.json`](config.example.json) lists every key at its default.

| Key | Default | Meaning |
| --- | --- | --- |
| `idleTimeoutSec` | `60` | Quiet time before the open entry closes, backdated to the last activity. |
| `pollIntervalSec` | `10` | How often the focused pane's screen is hashed. Also the accuracy bound on an entry's end time. |
| `snapshotIntervalSec` | `2` | How often focus and agent status are polled. |
| `activeStatuses` | `["working"]` | Agent statuses that count as activity on their own. |
| `minEntrySec` | `30` | Entries shorter than this are discarded. `0` keeps everything. |
| `logLevel` | `"info"` | `error` \| `warn` \| `info` \| `debug`. |

A daemon holds its settings from the moment it started, and there is one per Herdr session,
so apply a config change everywhere:

```sh
for s in ~/.config/herdr/herdr.sock ~/.config/herdr/sessions/*/herdr.sock; do
  HERDR_SOCKET_PATH="$s" track daemon --restart
done
```

## When nothing happens

| Symptom | Check |
| --- | --- |
| `~/.local/state/herdr/plugins/workspace-time-tracker/track` does not exist | The daemon has never started. `herdr plugin log list --plugin workspace-time-tracker` is the only place hook output goes. |
| `track report` prints nothing | Entries under `minEntrySec` (30 s) are discarded, and the current entry is not written until it closes — `track status` shows it. |
| A Space accrues time you did not work | A plain pane running `top`, `htop`, `watch` or a progress bar looks permanently active. See below. |

## What counts as "activity"

This is the hard part, and it is worth knowing exactly what the plugin can and cannot see.

| Signal | Used for | Notes |
| --- | --- | --- |
| Agent status | Any pane in the focused Space reporting `working` | Authoritative, free — it rides along with the focus poll |
| Screen hash of the focused pane | Plain shells only | sha256 of `pane.read {source: "visible"}`, ~0.59 ms |
| Focus changes | Any Space, tab or pane change | You cannot navigate without being present |

**Why a screen hash at all?** Because a plain shell emits *no Herdr events whatsoever* on
`cd` or command output — measured, see
[`../docs/herdr-vscode-sync-facts.md`](../docs/herdr-vscode-sync-facts.md) probe 18. A
tracker driven by events alone would call you idle while you worked in a terminal for an
hour. `panes[].revision` looked like a cheaper answer and is not one: it bumps on structural
changes such as cwd, never on output or keystrokes.

**Why agent panes are never hashed.** An animating UI defeats the hash entirely. With
`top -d 1` standing in for an agent's spinner, four samples over eight seconds produced four
different hashes for both `visible` and `detection` — such a pane reads as permanently
active and its entry would never close. Panes with a detected agent have a better signal
anyway (their status), so the hash is reserved for plain shells.

**The residual false positive**, documented rather than solved: a *plain* pane left running
`top`, `htop`, `watch` or a progress bar looks permanently active. If you park one of those
in a Space and walk away, that Space keeps accruing time.

## When entries close

| Trigger | `end_reason` | Ends at |
| --- | --- | --- |
| You switch to another Space | `switch` | now |
| No activity for `idleTimeoutSec` | `idle` | **the last activity** |
| The Space is closed | `closed` | now |
| Local midnight | `rollover` | 23:59:59, and a new entry opens at 00:00:00 |
| Daemon stops, or the Herdr server does | `shutdown` | the last activity |
| A previous daemon was killed and left an entry open | `recovered` | its last recorded activity |

Two deliberate choices:

- **Idle closes are backdated.** Ending at "now" would silently add `idleTimeoutSec` of
  fiction to nearly every entry. The dead window is never counted.
- **Switches end at now**, because switching is itself proof you were there. This can
  over-count by at most `idleTimeoutSec` — walk away for 50 s, come back, switch — which is
  the deliberate trade for not under-counting ordinary work.

Entries shorter than `minEntrySec` (default 30 s) are discarded, so flipping through five
Spaces looking for one does not leave five stubs behind.

## The data

Entries are appended, one JSON object per line, to:

```
~/.local/state/herdr/plugins/workspace-time-tracker/entries.jsonl
```

```json
{"v":1,"workspace_id":"w1","label":"herdr-plugins","cwd":"/Users/you/code/thing",
 "start":"2026-08-27T09:12:03-05:00","end":"2026-08-27T09:41:55-05:00","seconds":1792,
 "end_reason":"switch","session":"default","host":"your-mac"}
```

- Timestamps are **local** with an offset, because the artefact is a human daily report.
- `cwd` is **omitted, never null**, when unknown.
- `label` is the Space's name at close, so a rename mid-entry uses the new one. Labels are
  not unique in Herdr; `report --by workspace` groups by id instead.
- `workspace_id` values are **reused across sessions** and are not stable identifiers.

The state directory is keyed on plugin id, not on session, so **all your Herdr sessions
append to one file** — which is what makes a single report possible. Each line records its
`session`. Two sessions can legitimately have different Spaces focused at once, so entries
may overlap; the report detects that and says so rather than pretending the total is
wall-clock time.

Appends are a single `write()` to an `O_APPEND` descriptor, so concurrent daemons can
interleave lines but cannot tear one. A malformed line — from a process killed mid-write —
is skipped with a warning rather than breaking the report.

## How it works

Like its sibling `agent-caffeinate`, it **polls rather than subscribes**, for the reasons
measured in [`../docs/herdr-daemon-facts.md`](../docs/herdr-daemon-facts.md): there is no
session-wide agent-status stream (`pane.agent_status_changed` requires a concrete
`pane_id`), the server closes a connection after every non-subscribe request, and a whole
snapshot costs 0.35 ms.

Two cadences share one loop — the snapshot every `snapshotIntervalSec` (focus and agent
status), the screen hash every `pollIntervalSec` (the more expensive read, needing less
resolution).

The open segment is mirrored to disk after every update, so a `kill -9` loses at most one
poll rather than the whole entry: the next daemon finds it and closes it out as `recovered`.
A failed connect means the Herdr server is gone — a plugin-spawned daemon outlives its
server (measured), so this is required, not optional.

## Where things live

| What | Path |
| --- | --- |
| Config (optional, you create it) | `~/.config/herdr/plugins/config/workspace-time-tracker/config.json` |
| `track` shim, `entries.jsonl`, per-session lock, log and daemon state | `~/.local/state/herdr/plugins/workspace-time-tracker/` |
| Plugin source (installed) | `~/.config/herdr/plugins/github/workspace-time-tracker-<hash>/workspace-time-tracker/` |

`herdr plugin uninstall workspace-time-tracker` removes the plugin and its checkout. Config
and state are left in place — **including `entries.jsonl`** — so reinstalling keeps your
history. Delete those directories by hand, plus any symlink or shell function you pointed
at the shim.

## Development

```sh
herdr plugin link ./workspace-time-tracker    # run a working tree instead of an install
cd workspace-time-tracker && python3 -m unittest discover -s test
```

95 tests, no Herdr server required. The daemon runs as a real subprocess against a fake
Herdr socket server; timing rules live in `src/segments.py`, which takes an injected clock —
the midnight rollover in particular could not be tested any other way.

The floor is **Python 3.9** (stock macOS ships 3.9.6): no `match`, no PEP 604 `X | Y`
runtime annotations, no `tomllib`.

**Note for the devcontainer in this repo:** it ships `python3-minimal`, with no `unittest`
or `json`. Run `sudo apt-get install -y python3` first.
