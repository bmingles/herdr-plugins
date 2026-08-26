# Plan index

`.plans/PLAN.md` is the source of truth for plan status. Individual plan docs live
beside it; completed plans move to `.plans/archived/`.

## Status

### Pending

- [vscode-workspace-sync-discovery](vscode-workspace-sync-discovery.md) — resolve the
  Herdr and VS Code unknowns on the host. Prerequisite for the plan below.
- [vscode-workspace-sync](vscode-workspace-sync.md) — Herdr plugin that keeps a VS Code
  multi-root `.code-workspace` file in sync with Herdr Spaces.

### Completed

_None yet._

## Development Phases

| Phase | Plan | Description | Status |
| --- | --- | --- | --- |
| 1 | [vscode-workspace-sync-discovery](vscode-workspace-sync-discovery.md) | Probe Herdr JSON shapes, plugin event delivery, server PATH, and VS Code folder live-reload on the host | |
| 2 | [vscode-workspace-sync](vscode-workspace-sync.md) | Mirror Herdr Spaces into a VS Code workspace file's `folders` array | |
