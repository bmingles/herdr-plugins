# bridge-keepawake-indicator

Give `bridge-keepawake` the tab-bar status indicator `agent-caffeinate` already has,
so a container Herdr session shows that the plugin is pinging.

## Why, and what it is honest about

`agent-caffeinate` renders `☕ caffeinate` from an `indicator` subcommand wired into
`ui.tab_bar_right`. `bridge-keepawake` has no indicator at all — its CLI is
`daemon | stop | status | doctor` — so a container session shows nothing. That is a
missing feature, not missing user config; no `config.toml` entry can surface it today.

**The container cannot cheaply know the host's state, and should not pretend to.**
`agent-caffeinate` renders "holding the inhibitor", which it knows because it owns the
inhibitor. This plugin owns nothing: the host starts and stops `caffeinate` on its own
idle timer, so local ping activity and host inhibitor state diverge by up to
`DEVC_BRIDGE_KEEPAWAKE_IDLE_MS` (5 minutes by default). Asking the host would mean a
subprocess and a round trip on every tab-bar refresh, which violates the constraint
`agent-caffeinate`'s indicator code is explicit about: the command re-runs every few
seconds and "must be cheap and must never block" — it makes no socket call at all.

So this indicator answers **"is the plugin pinging right now"**, not "is the Mac
awake". The README must say so plainly; a reader who assumes otherwise will file a bug
the first time the icon clears while the host is still holding.

## Anti-flicker

The plugin holds no state, so raw `activePanes` emptiness would flicker the icon on
every brief detection gap. A measured example, captured 2026-09-04 against a real
Herdr 0.8.2 session: at the moment a turn ends and the CLI has not yet rendered its
`✻ Waiting for 1 background agent to finish` line, no detection rule matches and the
pane reads `idle` for **one 2-second poll** before returning to `working`. The daemon
correctly recorded `activePanes: []` for that sample.

Flicker is a display concern only, so the fix is display-only: a **hold**. The
indicator renders the holding icon while panes are active *or* while the last active
sample was within `indicatorHoldSec`. This adds no state to the daemon loop and does
not affect pinging — the daemon's behaviour is unchanged, only what the tab bar shows.

`indicatorHoldSec` defaults to **30**. Rationale: 15× the measured 2 s gap, below
`agent-caffeinate`'s 60 s grace so the icon is never the *more* sluggish of the two,
and at the recommended 5 s refresh it costs at most one visible transition per genuine
idle period.

## Contract

### Daemon status file — one new field

`daemon.json` gains `lastActiveAt` (epoch seconds, float), set to the current time on
every poll where `activePanes` is non-empty and otherwise carried forward unchanged.
Absent or non-numeric is treated as "never active". Every existing field keeps its
current meaning.

### CLI

```
bridge-keepawake indicator [--label <text>] [--icon <char>] [--show-idle]
```

| Flag | Default |
| --- | --- |
| `--label` | `keepawake` |
| `--icon` | `☕` (U+2615) |
| `--show-idle` | off |

Reads `daemon.json` and the pid file only. **No socket call, no `devc-bridge`
subprocess, no `Deno.Command` of any kind.** Exit code is `0` in every case below,
including when nothing is rendered.

### What it renders

| Daemon | Renders |
| --- | --- |
| pinging, or last active within `indicatorHoldSec` | `☕ keepawake` |
| running, last ping failed (`lastPingOk: false`) | `⚠ keepawake` |
| running, idle beyond the hold | nothing — `--show-idle` renders `○ keepawake` |
| status file older than the staleness bound (wedged) | `⚠ keepawake` |
| not running, or no status file yet | nothing |

Icons match `agent-caffeinate`: `○` is U+25CB, `⚠` is U+26A0. Empty output is a real
state, not a gap — Herdr clears the entry on empty output, and separators appear only
between visible entries.

`lastPingOk: false` earning `⚠` is the one place this indicator says something
`agent-caffeinate`'s cannot: a reachable daemon whose pings are being refused means
the bridge or the host is down, which is exactly when a user asks why their Mac slept.

Staleness reuses whatever bound `doctor` already applies, so the two never disagree.

### Config

One new optional key in `$HERDR_PLUGIN_CONFIG_DIR/config.json`:

| Key | Default | Meaning |
| --- | --- | --- |
| `indicatorHoldSec` | `30` | Seconds to keep rendering the holding icon after the last active poll. Display smoothing only; does not affect pinging. |

Validated like `pollIntervalSec` — must be a positive number; `0` is rejected.

### Wiring it up

The daemon rewrites a launcher shim at `$stateDir/bridge-keepawake` on every start, so
the path is stable across reinstalls:

```toml
[ui]
tab_bar_right = [
  { type = "command", command = "~/.local/state/herdr/plugins/bridge-keepawake/bridge-keepawake indicator", interval_seconds = 5, timeout_seconds = 2 },
]
tab_bar_right_separator = " · "
```

Then `herdr server reload-config`. Keep each `{ … }` on one line — TOML inline tables
cannot span newlines. No `[[actions]]` entry and no manifest change: a tab-bar entry
is user `config.toml`, not something a plugin declares, the same split
`agent-caffeinate`'s manifest documents.

## Checklist

- [ ] 1. `lastActiveAt` written into `daemon.json` by the daemon loop
- [ ] 2. `indicator` subcommand in `src/main.ts` — pure state-file read, no subprocess
- [ ] 3. `indicatorHoldSec` in `src/config.ts` with validation
- [ ] 4. `indicator` routed in `bin/bridge-keepawake` alongside `stop|status|doctor`
- [ ] 5. `config.example.json`: the new key at its default, with the display-only note
- [ ] 6. README: a "Status indicator" section, the `config.toml` snippet, the render
      table, and an explicit statement that it reflects **local ping state, not host
      inhibitor state**
- [ ] 7. `herdr-plugins/README.md`: the plugin table's "Config needed" cell for
      `bridge-keepawake` notes the optional indicator

## Validation

- [ ] `cd bridge-keepawake && deno task typecheck` — clean
- [ ] `cd bridge-keepawake && deno task test` — all pass, count above the current 21
- [ ] Unit: no status file → `""`, exit 0
- [ ] Unit: pid on record is dead → `""`, exit 0
- [ ] Unit: `activePanes` non-empty → `☕ keepawake`
- [ ] Unit: `activePanes` empty and `lastActiveAt` 5 s ago → `☕ keepawake` (held)
- [ ] Unit: `activePanes` empty and `lastActiveAt` 45 s ago → `""`; with
      `--show-idle` → `○ keepawake`
- [ ] Unit: same case with `indicatorHoldSec: 60` → `☕ keepawake` (hold honoured)
- [ ] Unit: `lastPingOk: false` while active → `⚠ keepawake`
- [ ] Unit: `updatedAt` beyond the staleness bound → `⚠ keepawake`
- [ ] Unit: `--label`/`--icon` override both halves of the output
- [ ] Assert the indicator path spawns **no** subprocess — run it with a `devc-bridge`
      stub on `PATH` that writes a marker file, and assert the marker is absent
- [ ] Manual, in a container Herdr session: add the `tab_bar_right` entry,
      `herdr server reload-config`, confirm `☕ keepawake` appears while an agent works
      and clears ~30 s after it stops
- [ ] Manual: confirm the icon does **not** flicker across a turn→subagent transition,
      the 2 s gap this hold exists for

## Relevant Files

- `bridge-keepawake/src/main.ts` — `indicator` subcommand, render logic, `lastActiveAt`
  in the status payload, usage string
- `bridge-keepawake/src/config.ts` — `indicatorHoldSec`, `KNOWN_KEYS`
- `bridge-keepawake/src/daemonize.ts` — staleness bound shared with `doctor`, if it
  lives here
- `bridge-keepawake/bin/bridge-keepawake` — route `indicator`, update usage
- `bridge-keepawake/config.example.json` — new key
- `bridge-keepawake/README.md` — Status indicator section; the honesty note
- `bridge-keepawake/test/indicator.test.ts` — new
- `bridge-keepawake/test/config.test.ts` — `indicatorHoldSec` validation
- `herdr-plugins/README.md` — plugin table, "Config needed" cell
- `.plans/PLAN.md` — status entry and phase row
