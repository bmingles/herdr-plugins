# workspace-time-tracker

A Herdr plugin that records how much time you spend in each Space. A time entry opens
when a Space takes focus, closes when focus moves elsewhere, and closes **backdated to
the last sign of activity** after a minute of quiet.

**Python 3.9+, standard library only, no build step, no dependencies** — same runtime
contract as its two siblings.

> **Prerequisites:**
> 1. [`herdr-daemon-discovery.md`](herdr-daemon-discovery.md) probe **A** returns GO.
> 2. Probe **D2/D3** has named the activity probe (`panes[].revision` vs a screen hash).
> 3. Build [`agent-caffeinate`](agent-caffeinate.md) first. It is the same daemon shape
>    with a much smaller state machine, so it derisks `sock.py` / `daemonize.py`, which
>    this plugin copies.

## The hard part is not timing, it is knowing you are there

Probe 18 established that a plain-shell pane emits **no events at all** on `cd` or on
command output. A tracker driven by events alone would declare you idle while you work in
a terminal for an hour. So activity is the union of two signals:

1. **Agent status** — any pane in the focused Space reporting a status in
   `activeStatuses` (default `["working"]`).
2. **A polled activity token** for the focused pane, sampled every `pollIntervalSec`
   (default **10**). A change in the token is activity.

The token's implementation is chosen by discovery probe D2:

- **If `panes[].revision` increments on terminal output** → the token is that integer,
  read from one `pane list` call. Cheapest possible; no screen reading.
- **Otherwise** → the token is a sha256 of `herdr pane read <pane> --source detection`.

Either way it is one function, `probe_activity_token(pane_id) -> str`, and the tests fake
it. **D3's stability check matters**: if a Claude pane's screen animates while idle, the
hash never settles and idle detection never fires — in that case the hash must be taken
over the pane's **non-animating region** or the token falls back to revision-only, and
the facts doc records which.

Any subscribed event naming a pane or tab inside the focused Space also counts as
activity — free, and it covers gestures the poll might miss between samples.

## Design

### Daemon

Identical shape to `agent-caffeinate`: one daemon per Herdr server, singleton via a
flock keyed on `sha256($HERDR_SOCKET_PATH)[:12]`, double-fork + `setsid` out of
`[[startup]]`, seeded from `herdr api snapshot`, driven by an `events.subscribe`
connection with a `select()` timeout so timers fire without events.

Subscribes to: `workspace.focused`, `workspace.closed`, `workspace.renamed`,
`pane.agent_status_changed`, plus whichever of `pane.focused` / `tab.focused` /
`pane.created` / `pane.updated` probe B3 shows delivers over a subscription.

### Segments

Exactly one segment is open at a time, for the focused Space.

| Trigger | Action | `end_reason` |
| --- | --- | --- |
| `workspace.focused` for a different Space | close current, open new | `switch` |
| No activity for `idleTimeoutSec` (default 60) | close, **backdated to `last_activity`** | `idle` |
| Activity while no segment is open | open a new one starting **now** | — |
| `workspace.closed` for the open Space | close | `closed` |
| Local midnight | close and immediately reopen | `rollover` |
| SIGTERM / socket EOF | close | `shutdown` |

Two decisions that follow from "track time actually worked":

- **Backdating on idle is required.** Closing at `now` would silently add
  `idleTimeoutSec` of fiction to every entry. The dead minute is never counted.
- **A segment shorter than `minEntrySec` (default 30) is discarded**, not written.
  Paging through five Spaces to find one should not produce five entries. Discards are
  logged at `debug` so nothing is invisible.
- **Midnight rollover** keeps every entry inside one calendar day, which is what makes
  `report` a pure filter rather than a splitter.

### Storage

Append-only JSONL at **`$HERDR_PLUGIN_STATE_DIR/entries.jsonl`**. The state dir is
**shared across sessions** (it is keyed on plugin id, not socket), which is what we want:
one file to report from. Concurrency is handled by opening `O_APPEND` and writing each
entry as a **single `write()` of one line**, so interleaving cannot corrupt a record.

Two sessions can legitimately have two different Spaces focused at once, producing
overlapping entries. That is recorded honestly rather than deduped; `report` shows a
`(overlapping)` marker when a day contains overlapping intervals, and the README explains
it.

The open segment is mirrored to `$HERDR_PLUGIN_STATE_DIR/<session-key>/current.json`
after every activity update, so a `kill -9`'d daemon loses at most `pollIntervalSec` of
time: the next daemon start finds it, closes it at its recorded `last_activity` with
`end_reason: "recovered"`, and appends it.

### Entry schema — this is the contract

One JSON object per line, no trailing whitespace:

```json
{"v":1,"workspace_id":"w4","label":"herdr-plugins","cwd":"/Users/bingles/code/tools/herdr-plugins","start":"2026-08-27T09:12:03-05:00","end":"2026-08-27T09:41:55-05:00","seconds":1792,"end_reason":"switch","session":"default","host":"bingles-mbp"}
```

| Field | Rule |
| --- | --- |
| `v` | Schema version, always `1`. |
| `workspace_id` | e.g. `"w4"`. Note these are **reused** across sessions and are not stable identifiers — `label` is the human key for reporting. |
| `label` | The Space's label **as of segment close** (a rename mid-segment uses the new one). Labels are not unique (probe 5); `report` groups by label anyway and says so. |
| `cwd` | Best effort, from `HERDR_PLUGIN_CONTEXT_JSON.workspace_cwd` or `panes[].cwd`. **Omitted, never null**, when unknown. |
| `start`, `end` | ISO 8601 **local time with offset**, second precision. Local because the artefact is a human daily report. |
| `seconds` | Integer, `round(end - start)`. Redundant with the timestamps, kept because every consumer wants it. |
| `end_reason` | One of `switch`, `idle`, `closed`, `rollover`, `shutdown`, `recovered`. |
| `session` | `"default"`, or the named session from the socket path. |
| `host` | `socket.gethostname()`. |

Timestamps are formatted with `datetime.now().astimezone().isoformat(timespec="seconds")`
and parsed with `datetime.fromisoformat` — which handles this exact format on 3.9 and is
why the format is pinned to it rather than to anything hand-rolled.

## Contract

### Layout

```
workspace-time-tracker/
  herdr-plugin.toml
  README.md
  config.example.json
  bin/track                     # sh shim -> src/main.py
  src/main.py                   # CLI dispatch
  src/config.py
  src/jsonc.py                  # copied verbatim, as in agent-caffeinate
  src/sock.py                   # copied from agent-caffeinate, then extended
  src/daemonize.py              # copied from agent-caffeinate
  src/activity.py               # probe_activity_token + the poller
  src/segments.py               # pure state machine: open/close/backdate/discard/rollover
  src/store.py                  # append entry, read entries, current.json
  src/report.py                 # aggregation + formatting
  test/
```

### CLI — `bin/track <command>`

| Command | Behaviour |
| --- | --- |
| `daemon` / `--ensure` / `--foreground` / `--restart` | As in `agent-caffeinate`. |
| `stop` | Close the open segment (`end_reason: "shutdown"`) and stop the daemon. |
| `status` | Open segment's label, elapsed, seconds until the idle timeout, last activity source. |
| `report` | Today, grouped by label. |
| `report --day today\|yesterday\|YYYY-MM-DD` | One day. |
| `report --since YYYY-MM-DD [--until YYYY-MM-DD]` | Inclusive range, local days. |
| `report --by label\|workspace\|day` | Grouping. Default `label`. |
| `report --json` | Machine-readable; **this, not the text layout, is what validation asserts**. |
| `doctor` | Config, session key, entries path and line count, chosen activity-token implementation, whether the socket is reachable. |

`report` text output, illustrative — the shape is fixed, the exact glyphs are not:

```
2026-08-27  (today)
  herdr-plugins          3h 12m
  deephaven-core         0h 48m
  ───────────────────────────────
  total                  4h 00m
```

`report --json`, exact:

```json
{"v":1,"range":{"since":"2026-08-27","until":"2026-08-27"},"by":"label",
 "groups":[{"key":"herdr-plugins","seconds":11520,"entries":7}],
 "total_seconds":14400,"overlapping":false}
```

Exit codes: `0` success, `1` config error, `2` socket unreachable (daemon commands only).
`report` on an empty file prints a `no entries` line and exits **0**.

### Manifest

```toml
id = "workspace-time-tracker"
name = "Workspace Time Tracker"
version = "0.1.0"
min_herdr_version = "0.8.0"
description = "Track time spent in each Herdr Space"
platforms = ["macos", "linux"]

[[startup]]
command = ["./bin/track", "daemon"]

# Self-heal, exactly as in agent-caffeinate. The daemon receives workspace.focused on its
# own subscription; this only recovers a dead daemon on the next navigation.
[[events]]
on = "workspace.focused"
command = ["./bin/track", "daemon", "--ensure"]

[[actions]]
id = "flush"
title = "Close current time entry"
contexts = ["global"]
command = ["./bin/track", "flush"]
```

No `report` action: its whole value is legible output, and action stdout only surfaces
JSON-escaped through `herdr plugin log list`. `report` is a terminal command.

**No `[[panes]]` block.** A live totals pane is the obvious next feature, but
plugin-owned panes are entirely unexercised in this repo's research — that is a separate
plan with its own discovery, not a rider on this one.

### Config — `$HERDR_PLUGIN_CONFIG_DIR/config.json`, entirely optional

```jsonc
{
  // Quiet time before the open entry is closed, backdated to the last activity.
  "idleTimeoutSec": 60,

  // How often the focused pane's activity token is sampled. Also the accuracy bound:
  // an entry's end time is correct to within one interval.
  "pollIntervalSec": 10,

  // Agent statuses that count as activity on their own.
  "activeStatuses": ["working"],

  // Entries shorter than this are discarded rather than written.
  "minEntrySec": 30,

  // Keep accruing time while no client is attached to the session. Only meaningful if
  // discovery probe D4 found an observable attach/detach signal; ignored (with a warning
  // in `doctor`) if it did not.
  "trackDetached": false,

  // "error" | "warn" | "info" | "debug"
  "logLevel": "info"
}
```

Env overrides, tests only: `HERDR_TRACK_IDLE_TIMEOUT_SEC`, `HERDR_TRACK_POLL_INTERVAL_SEC`,
`HERDR_TRACK_MIN_ENTRY_SEC`, `HERDR_TRACK_ENTRIES_PATH`, `HERDR_TRACK_LOG_LEVEL`.

## Faking it in the devcontainer

Same three fakes as `agent-caffeinate`, plus one more:

- **`test/fake_server.py`** — scripted `workspace_focused` / `pane_agent_status_changed`
  frames.
- **`test/fake-herdr`** — canned `api snapshot` and `pane list` responses; the `pane list`
  response's `revision` is **driven from a file** the test mutates, which is how polled
  activity is simulated without any terminal.
- **A frozen clock.** `segments.py` takes `now()` as a constructor argument, so backdating,
  the `minEntrySec` discard and the midnight rollover are all tested deterministically
  rather than by sleeping. Only the end-to-end tests use real time, with
  `idleTimeoutSec=1` and `pollIntervalSec=0.2`.

The midnight rollover in particular is **only** testable with an injected clock, since a
real test cannot wait for midnight. That is the reason the state machine is pure.

## Checklist

- [x] `workspace-time-tracker/` skeleton: manifest, `bin/track` shim, `src/` layout
- [x] `src/jsonc.py`, `src/sock.py`, `src/daemonize.py` copied from the built
      `agent-caffeinate`, headers noting the duplication and why
- [x] `src/config.py`: defaults, optional file, validation, env overrides
- [x] `src/activity.py`: the screen-hash token (D2 ruled `revision` out), the
      agent-pane carve-out, and the poller
- [x] `src/segments.py`: pure state machine — open, switch, idle-backdate, discard under
      `minEntrySec`, midnight rollover, `next_deadline()`; clock injected, **no I/O**
- [x] `src/store.py`: single-`write()` append, `current.json` mirror, recovery of a
      stranded `current.json` at daemon start, entries reader that skips malformed lines
      with a warning
- [x] `src/report.py`: day/range filtering in local time, grouping by label/workspace/day,
      overlap detection, text and `--json` renderers
- [x] `src/main.py`: CLI dispatch, seed, subscribe, poll+select loop, signals
- [x] `test/fake_server.py`, `test/fake-herdr`, fixtures
- [x] Unit tests for `segments`: switch, idle backdating, activity re-opening after idle,
      short-entry discard, rollover at midnight, close on `workspace.closed`, shutdown
- [x] Unit tests for `store`: append format byte-exact, malformed line tolerated,
      recovery path, concurrent appends from two processes stay one-line-per-record
- [x] Unit tests for `report`: empty file, one day, range, each `--by`, overlap flag,
      entries spanning a day boundary already split by rollover
- [x] Unit tests for `config`
- [x] End-to-end: focus A → work → focus B → assert two entries with the right labels
- [x] End-to-end: activity stops → entry closes backdated, not at `now`
- [x] End-to-end: `kill -9` mid-segment → next start recovers it as `recovered`
- [x] `workspace-time-tracker/README.md`: install, what counts as activity **and what
      does not**, accuracy bound, the multi-session overlap caveat, report examples
- [x] Root `README.md`: plugin table row
- [x] `.plans/PLAN.md` updated

## Validation

### Offline — must pass in the devcontainer

> **Interpreter gotcha — read before running any test command.** This devcontainer ships
> **`python3-minimal`**: `json`, `unittest`, `socket` and most of the stdlib are *absent*
> (`ls /usr/lib/python3.12 | wc -l` is 91, and `import json` raises
> `ModuleNotFoundError`). The sibling plugin's "147 tests on `/usr/bin/python3` 3.9.6"
> were run on the **macOS host**, whose system Python is 3.9.6 — not in here. Before
> running this plan's offline suite, either
> `sudo apt-get update && sudo apt-get install -y python3` in the container (and record it
> in `.devc/devc.jsonc` so it survives a rebuild), or run the suite on the host. The 3.9
> floor comes from the host interpreter and stays regardless.

- [x] `cd workspace-time-tracker/test && python3 -m unittest discover -s .` — **88 tests pass**
- [x] `./bin/track doctor` with no server: prints config, entries path, and the chosen
      activity-token implementation; exit 0
- [x] `./bin/track report` on an absent entries file prints `no entries`, exit **0**
- [x] `./bin/track report --json` on a fixture file emits **exactly** the documented
      envelope keys: `v`, `range`, `by`, `groups`, `total_seconds`, `overlapping`
- [x] Every emitted entry line parses as JSON and carries all required fields; `seconds`
      equals `round(end - start)` for every line
- [x] No non-stdlib imports in `src/` (same grep as the sibling plan)
- [x] Frozen-clock test: a segment idle at T+61 s writes `end` == the last activity time,
      **not** T+61
- [x] Frozen-clock test: a segment open across local midnight yields two entries, the
      first ending `23:59:59` local with `end_reason: "rollover"`
- [x] Frozen-clock test: a 12 s segment with `minEntrySec: 30` writes **nothing**
- [x] Concurrency test: two processes appending 500 entries each yield 1000 well-formed
      lines, zero partial lines
- [x] E2E: scripted focus A(2 s) → B(2 s) → stop yields entries labelled A then B with
      `end_reason` `switch` then `shutdown` (with `minEntrySec: 0`)
- [x] E2E: revision file bumped every 0.5 s keeps the entry open past `idleTimeoutSec`;
      stopping the bumps closes it within `idleTimeoutSec + pollIntervalSec + 1`

### Host — needs a real Herdr server

Most of these were satisfied **in the devcontainer against a real Herdr server** (0.8.2),
which this repo's earlier plans assumed impossible. Marked `[x]` with a note; the rest
need a real agent or a working day of data.


- [x] `herdr plugin link ./workspace-time-tracker`, daemon starts, hook exits 0 fast —
      done in-container
- [x] **The probe-18 case passed.** Typing into a plain shell — no agent, no Herdr
      events at all — was detected via the screen hash and held the entry open. Not yet
      run for a full 5 minutes, nor with a real agent pane alongside.
- [x] Switch Spaces → entry closed with `end_reason: "switch"`, duration correct —
      verified in-container
- [x] Going quiet closed the entry with `end_reason: "idle"`, ending at the last
      keystroke (12:14:21) rather than when idle fired (12:14:29) — the dead window was
      excluded exactly. Verified in-container with an 8 s timeout.
- [ ] Leave a **real Claude** pane idle with its UI animating for 3 minutes → the entry
      still closes. The equivalent is covered by a test (an agent pane whose screen
      churns every sample is never hashed), but not yet against a real agent.
- [ ] `./bin/track report` after a real day of work looks right against your own memory
- [ ] Measured cost: daemon CPU over an hour is under ~1% of a core
- [x] Runs alongside `agent-caffeinate` with no interference — separate plugin ids,
      state dirs and locks; both suites green together.

## Relevant Files

| File | Change |
| --- | --- |
| `workspace-time-tracker/herdr-plugin.toml` | **New.** |
| `workspace-time-tracker/README.md` | **New.** |
| `workspace-time-tracker/config.example.json` | **New.** |
| `workspace-time-tracker/bin/track` | **New.** sh shim. |
| `workspace-time-tracker/src/main.py` | **New.** |
| `workspace-time-tracker/src/config.py` | **New.** |
| `workspace-time-tracker/src/jsonc.py` | **New.** Copy. |
| `workspace-time-tracker/src/sock.py` | **New.** Copy from `agent-caffeinate`. |
| `workspace-time-tracker/src/daemonize.py` | **New.** Copy from `agent-caffeinate`. |
| `workspace-time-tracker/src/activity.py` | **New.** |
| `workspace-time-tracker/src/segments.py` | **New.** |
| `workspace-time-tracker/src/store.py` | **New.** |
| `workspace-time-tracker/src/report.py` | **New.** |
| `workspace-time-tracker/test/*` | **New.** |
| `workspace-time-tracker/.gitignore` | **New.** |
| `README.md` | Plugin table row. |
| `docs/herdr-daemon-facts.md` | Produced by discovery; cited for the activity-probe choice. |
| `.plans/PLAN.md` | Status + phase row. |

## Discovery corrections

Folded in from [`docs/herdr-daemon-facts.md`](../docs/herdr-daemon-facts.md)
(devcontainer, Herdr **0.8.2 / protocol 20**).

### 1. `panes[].revision` is NOT an activity signal — the screen hash wins

The plan left this as a fork resolved by probe D2. **Resolved: NO.** Under a control that
verified the pane actually responded:

```
baseline                 : revision=1
(a) after command output : revision=1  NO CHANGE
(b) after keystrokes only: revision=1  NO CHANGE
(c) after cd             : revision=2 (cwd=/tmp)  CHANGED
```

`revision` tracks *structural* change, not content. `probe_activity_token` is therefore
the screen hash: `pane.read {pane_id, source:"visible"}`, sha256 of
`result.read.text`, measured at **0.59 ms** per call — 0.0059% of a core at a 10 s poll.
Note `source` is **required**, and the valid values are `visible`, `recent`,
`recent_unwrapped`, `detection` — not `screen`/`scrollback`. The text is at
`result.read.text`, not `result.text`.

### 2. An animating pane defeats the screen hash — agent panes get a carve-out

D3's risk is real. With `top -d 1` standing in for an animated agent UI, **both**
`visible` and `detection` produced 4 distinct hashes in 4 samples over 8 s: the pane
reads as permanently active and the entry would never close.

So the token is chosen **per pane**:

- Pane **has a detected agent** → use `agent_status` from the snapshot. Never hash it.
- Pane is a **plain shell** → hash the screen.

Known false positive, to be documented in the README: a plain pane left running `top`,
`htop`, `watch` or a progress bar reads as permanently active. `pane.process_info`
exists and could refine this later; it is not in scope.

### 3. Poll rather than subscribe

Same reasoning as `agent-caffeinate`: no session-wide agent-status stream (`pane_id` is
required and un-wildcardable), and the server closes the connection after any
non-subscribe request. One `session.snapshot` per poll (**0.35 ms**) yields
`focused_workspace_id`, every pane's `agent_status`, and every pane's `cwd` — everything
the segment state machine needs — plus one `pane.read` for the focused plain pane.
Two socket calls, under 1 ms, per poll. **A failed connect means the server is gone**,
which closes the open segment with `end_reason: "shutdown"`; a daemon provably outlives
its server, so this is required, not optional.

Keep `pollIntervalSec` at 10 for the activity token, but poll the snapshot every **2 s**
so a Space switch is not backdated by up to 10 s. Two intervals, one loop.

### 4. `pane.updated` is subscribe-only and carries `cwd`

Not needed under the poll design, but recorded because it is the event probe 18 wanted
and could not have: it delivers over a subscription (7 frames in the test run), never to
a hook, and carries the full pane record including the new `cwd`.

### 5. Still unrun

- The animation test against a **real Claude pane** (`top` is a proxy).
- **D4**, client attach/detach — so `trackDetached` stays unimplemented and `doctor`
  should say so rather than pretending the key works.
