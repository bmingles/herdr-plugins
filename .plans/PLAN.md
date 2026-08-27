# Plan index

`.plans/PLAN.md` is the source of truth for plan status. Individual plan docs live
beside it; completed plans move to `.plans/archived/`.

## Status

### Pending

- [caffeinate-grace-tuning](caffeinate-grace-tuning.md) — decide `agent-caffeinate`'s
  default `idleGraceSec` (currently 60; is 30 safe?) from a day of debug logs rather than
  judgement. **Written to be picked up cold** — it carries the log format, the analysis
  commands, the classification that avoids relying on anyone's memory, and the decision
  rule. Blocked only on the user running the plugin for a day at `"logLevel": "debug"`.

- [herdr-daemon-discovery](herdr-daemon-discovery.md) — host probes answering the
  unknowns both new plugins depend on: does a daemon spawned from `[[startup]]` survive,
  what does a long-lived `events.subscribe` connection actually look like, and what
  signal proves a human is active in a plain shell. Deliverable:
  `docs/herdr-daemon-facts.md`. **Must run first** — probe A is go/no-go for the design
  in both plans below.

### Completed

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
| 6 | [caffeinate-grace-tuning](caffeinate-grace-tuning.md) | Set idleGraceSec from measured false-idle gaps |  |
