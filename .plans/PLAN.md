# Plan index

`.plans/PLAN.md` is the source of truth for plan status. Individual plan docs live
beside it; completed plans move to `.plans/archived/`.

## Status

### Pending

_None._

### Completed

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
