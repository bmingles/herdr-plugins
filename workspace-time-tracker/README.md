# workspace-time-tracker

A [Herdr](https://herdr.dev) plugin that records how long you actually spend in each
Space. An entry opens when you start working in a Space, closes when you switch, and
closes **backdated to your last sign of activity** after a minute of quiet.

Python 3.9+, standard library only, no build step, no dependencies.

```
$ track report
2026-08-27  (today)
  herdr-plugins          3h 12m
  deephaven-core         0h 48m
  ------------------------------
  total                  4h 00m
```

## What counts as "activity"

This is the hard part, and it is worth knowing exactly what the plugin can and cannot
see.

| Signal | Used for | Notes |
| --- | --- | --- |
| Agent status | Any pane in the focused Space reporting `working` | Authoritative, free — it rides along with the focus poll |
| Screen hash of the focused pane | Plain shells only | sha256 of `pane.read {source: "visible"}`, ~0.59 ms |
| Focus changes | Any Space, tab or pane change | You cannot navigate without being present |

**Why a screen hash at all?** Because a plain shell emits *no Herdr events whatsoever*
on `cd` or command output — measured, see
[`../docs/herdr-vscode-sync-facts.md`](../docs/herdr-vscode-sync-facts.md) probe 18. A
tracker driven by events alone would call you idle while you worked in a terminal for an
hour. `panes[].revision` looked like a cheaper answer and is not one: it bumps on
structural changes such as cwd, never on output or keystrokes.

**Why agent panes are never hashed.** An animating UI defeats the hash entirely. With
`top -d 1` standing in for an agent's spinner, four samples over eight seconds produced
four different hashes for both `visible` and `detection` — such a pane reads as
permanently active and its entry would never close. Panes with a detected agent have a
better signal anyway (their status), so the hash is reserved for plain shells.

**The residual false positive**, documented rather than solved: a *plain* pane left
running `top`, `htop`, `watch` or a progress bar looks permanently active. If you park
one of those in a Space and walk away, that Space keeps accruing time.

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
  over-count by at most `idleTimeoutSec` — walk away for 50 s, come back, switch — which
  is the deliberate trade for not under-counting ordinary work.

Entries shorter than `minEntrySec` (default 30 s) are discarded, so flipping through five
Spaces looking for one does not leave five stubs behind.

## Install

```sh
herdr plugin install bmingles/herdr-plugins/workspace-time-tracker
```

No config file is required. The daemon starts on **server boot**; after linking
mid-session, switch to a different Space and the `workspace.focused` hook starts it (a
*genuine* change — re-focusing the Space you are already on emits no event).

## Commands

| Command | What it does |
| --- | --- |
| `bin/track report` | Today, grouped by Space label |
| `bin/track report --day yesterday` / `--day 2026-08-01` | One specific day |
| `bin/track report --since 2026-08-01 [--until 2026-08-27]` | An inclusive range |
| `bin/track report --by label\|workspace\|day` | Grouping (default `label`) |
| `bin/track report --json` | Machine-readable; this shape is the contract |
| `bin/track status` | What is being tracked now, elapsed, and time until it closes |
| `bin/track flush` | Close the open entry immediately (also a plugin action) |
| `bin/track stop` | Stop the daemon, writing the open entry first |
| `bin/track doctor` | Resolved config, entry count, live focused Space |

## The data

Entries are appended, one JSON object per line, to `entries.jsonl` in
`$HERDR_PLUGIN_STATE_DIR`. `doctor` prints the path.

```json
{"v":1,"workspace_id":"w1","label":"herdr-plugins","cwd":"/Users/you/code/thing",
 "start":"2026-08-27T09:12:03-05:00","end":"2026-08-27T09:41:55-05:00","seconds":1792,
 "end_reason":"switch","session":"default","host":"your-mac"}
```

- Timestamps are **local** with an offset, because the artefact is a human daily report.
- `cwd` is **omitted, never null**, when unknown.
- `label` is the Space's name at close, so a rename mid-entry uses the new one. Labels
  are not unique in Herdr; `report --by workspace` groups by id instead.
- `workspace_id` values are **reused across sessions** and are not stable identifiers.

The state directory is keyed on plugin id, not on session, so **all your Herdr sessions
append to one file** — which is what makes a single report possible. Each line records
its `session`. Two sessions can legitimately have different Spaces focused at once, so
entries may overlap; the report detects that and says so rather than pretending the
total is wall-clock time.

Appends are a single `write()` to an `O_APPEND` descriptor, so concurrent daemons can
interleave lines but cannot tear one. A malformed line — from a process killed
mid-write — is skipped with a warning rather than breaking the report.

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
poll rather than the whole entry: the next daemon finds it and closes it out as
`recovered`. A failed connect means the Herdr server is gone — a plugin-spawned daemon
outlives its server (measured), so this is required, not optional.

## Tests

```sh
cd workspace-time-tracker && python3 -m unittest discover -s test
```

88 tests, no Herdr server required. The daemon runs as a real subprocess against a fake
Herdr socket server; timing rules live in `src/segments.py`, which takes an injected
clock — the midnight rollover in particular could not be tested any other way.

**Note for the devcontainer in this repo:** it ships `python3-minimal`, with no
`unittest` or `json`. Run `sudo apt-get install -y python3` first.
