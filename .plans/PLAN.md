# Plan index

`.plans/PLAN.md` is the source of truth for plan status. Individual plan docs live
beside it; completed plans move to `.plans/archived/`.

## Status

### Pending

- [herdr-daemon-discovery](herdr-daemon-discovery.md) — host probes answering the
  unknowns both new plugins depend on: does a daemon spawned from `[[startup]]` survive,
  what does a long-lived `events.subscribe` connection actually look like, and what
  signal proves a human is active in a plain shell. Deliverable:
  `docs/herdr-daemon-facts.md`. **Must run first** — probe A is go/no-go for the design
  in both plans below.

### Completed

- [caffeinate-grace-tuning](archived/caffeinate-grace-tuning.md) — `idleGraceSec` **stays
  60**, now on measured evidence rather than judgement. Run 1 (08-27) was worthless: the
  transition lines were at `debug` while `logLevel` defaulted to `info`, so 5 h of real use
  logged 28 grace releases and zero gap durations, and the planned `[30,60)` grep would have
  printed a false green light. Moving those lines to `info` and adding
  `agent-caffeinate/tools/gap-report.py` made run 2 (08-28) conclusive: over ~3 h active
  (1 h 33 m held, 4 panes, 3 sessions, all Claude Code), the **false-idle ceiling is 22.1 s**
  (6 observations, each sandwiched between real working spans) and human absence starts at
  214.9 s — **nothing in between**. So 30 breaks nothing observed but gives only 1.36x
  margin from six samples, under the plan's 1.5x rule; 60 is kept at 2.7x, and 45 is the
  value the data would support if a shorter grace is ever wanted. Two side findings, both
  recorded: a socket-only reader sees **`done`, essentially never `idle`**
  (`docs/herdr-daemon-facts.md` § C4), so any `grep 'idle -> working'` finds nothing; and
  `workspace-time-tracker`'s `entries.jsonl` is **not** independent corroboration — it shares
  the `working` signal and never hashes agent panes, so a spanning entry proves a human was
  at the keyboard, not that an agent was working. **95 tests pass.**

- [vscode-workspace-adopt](archived/vscode-workspace-adopt.md) — the **inbound**
  direction for `vscode-workspace-sync`: `bin/adopt` reads a `.code-workspace` file's
  `folders` and creates the Herdr Spaces they describe, with `scripts/herdrvs` as the
  shell entry point. Adopt and sync are **mutually exclusive per session** — a session
  either has a configured `workspaceFile` or it does not — which removes the feedback
  loop, the `mode: "active"` conflict and the `pinnedFolders` problem in one rule; adopt
  exits 2 in a session sync manages. Probed on herdr 0.8.2 and recorded as
  `docs/herdr-vscode-sync-facts.md` §19: `workspace create` does **not** dedupe by path,
  and a nonexistent *or* relative `--cwd` **silently** roots the Space at `$HOME`, so
  adopt dedupes against a live snapshot and `isdir`-checks every folder. **199 tests
  pass** (baseline 139), plus end-to-end verification against a **real Herdr server** —
  create, idempotent re-run, skip, `--relabel`, the guard, `herdrvs` and the plugin
  action. See `## Implementation corrections` in the plan.

- [vscode-sync-session-mapping](archived/vscode-sync-session-mapping.md) — each Herdr
  session now drives its own VS Code workspace file via a `sessions` name→config map,
  replacing the blunt "only the default session syncs" guard. Session name is derived
  from `$HERDR_SOCKET_PATH` (no subprocess); a session absent from the map still skips,
  and two sessions claiming one file is a hard config error. **139 tests pass** (baseline
  was 106 — the README's "147" was stale), and the backward-compatibility test passed
  unmodified. Verified end-to-end against a fake snapshot: two sessions wrote two files
  and neither touched the other's. See `## Implementation corrections` in the plan.

- [workspace-time-tracker](workspace-time-tracking.md) — implemented in
  `workspace-time-tracker/`. Entries open on activity in the focused Space, close on a
  switch, and close **backdated to the last activity** after a minute of quiet.
  **88 tests pass**, plus verification against a **real Herdr server**: typing in a
  **plain shell** (no agent, no Herdr events — the probe-18 case the whole design exists
  for) was detected via the screen hash, and an idle close excluded the dead window
  exactly. Discovery replaced the planned subscription with a poll and settled the
  activity token — see `## Discovery corrections` in the plan.

- [agent-caffeinate](agent-caffeinate.md) — implemented in `agent-caffeinate/`.
  Holds `caffeinate -i -s` while any agent reports `working`, releases 60 s after the
  last one stops. **58 tests pass**, and it was additionally verified against a **real
  Herdr server** in the devcontainer with only the inhibitor faked: startup hook returns
  in 42 ms, a real `working` status starts the inhibitor, `idle` releases it after the
  grace, and stopping the server makes the daemon release and exit rather than orphan.
  Discovery replaced the planned subscription with a poll — see `## Discovery
  corrections` in the plan. **Host-verified by the user on macOS**: `caffeinate` starts when a real
  containerised Claude session begins working and is gone ~60 s after it stops.

- [vscode-workspace-sync](archived/vscode-workspace-sync.md) — Herdr plugin that keeps a
  VS Code multi-root `.code-workspace` file in sync with Herdr Spaces. Implemented in
  `vscode-workspace-sync/`: Python 3.9+, stdlib only, no build step. All checklist items
  and all `## Validation → Offline` items pass (147 unittest tests on
  `/usr/bin/python3` 3.9.6). **`## Validation → Host` is still unrun** — it needs a live
  Herdr server and a real VS Code window; the checklist for it is in the archived plan.

- [vscode-workspace-sync-discovery](archived/vscode-workspace-sync-discovery.md) —
  resolved the Herdr and VS Code unknowns on the host. Deliverable:
  `docs/herdr-vscode-sync-facts.md`. VS Code live-reload is a **go**; corrections folded
  into `vscode-workspace-sync.md` under `## Discovery corrections`.

## Development Phases

| Phase | Plan | Description | Status |
| --- | --- | --- | --- |
| 1 | [vscode-workspace-sync-discovery](archived/vscode-workspace-sync-discovery.md) | Probe Herdr JSON shapes, plugin event delivery, server PATH, and VS Code folder live-reload on the host | complete |
| 2 | [vscode-workspace-sync](archived/vscode-workspace-sync.md) | Mirror Herdr Spaces into a VS Code workspace file's `folders` array | complete |
| 3 | [herdr-daemon-discovery](herdr-daemon-discovery.md) | Prove the `[[startup]]` daemon model, `events.subscribe` framing, and a plain-shell activity signal on the host | in progress |
| 4 | [agent-caffeinate](agent-caffeinate.md) | Hold a sleep-inhibiting assertion while agents are working, release it after a minute idle | complete |
| 5 | [workspace-time-tracker](workspace-time-tracking.md) | Track time spent per Space, stopping on switch and on inactivity | complete |
| 6 | [caffeinate-grace-tuning](archived/caffeinate-grace-tuning.md) | Set idleGraceSec from measured false-idle gaps | complete — 60 stands; false-idle ceiling measured at 22.1 s |
| 7 | [vscode-sync-session-mapping](archived/vscode-sync-session-mapping.md) | Map each Herdr session name to its own VS Code workspace file | complete |
| 8 | [vscode-workspace-adopt](archived/vscode-workspace-adopt.md) | Create Herdr Spaces from a `.code-workspace` file's folders, for sessions sync does not manage | complete |
