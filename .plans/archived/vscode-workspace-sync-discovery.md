# vscode-workspace-sync — host discovery

Resolve every unknown that `vscode-workspace-sync.md` currently assumes, by running
Herdr and VS Code on the host. **This is a prerequisite for that plan**, not a parallel
track: three of its decisions (how Space state is read, which events are hooked, whether
mode `active` is viable at all) are guesses until this runs.

## Why this is a separate doc

The implementation plan was written inside a devcontainer where `herdr` does not exist
and cannot be installed. Every Herdr JSON shape in it was inferred from prose in the
0.8.2 docs, and the VS Code behaviour it depends on was inferred from ordinary use.
This doc is the handoff to an agent running where both are real.

## Deliverable

`docs/herdr-vscode-sync-facts.md`, with one section per probe below, each recording
**observed output**, not a restatement of what the docs claim. Paste real JSON and real
log lines; truncate long payloads with `…` rather than paraphrasing them.

Then reconcile: for each probe whose finding contradicts
`.plans/vscode-workspace-sync.md`, edit that plan and add a line to a
`## Discovery corrections` section there naming what changed and why. Do not silently
rewrite an assumption — the point is that the next reader can see which parts were
verified and which were inherited.

## Prerequisites

- Herdr installed and a session running with **at least three Spaces**, at least one of
  which is a Git worktree Space (`herdr worktree create`), and at least one whose label
  differs from its directory basename.
- VS Code with a multi-root `.code-workspace` file open, and Herdr running in that
  window's integrated terminal.
- Record `herdr --version` first. The implementation plan sets `min_herdr_version =
  "0.8.0"`; `herdr api snapshot` is documented at 0.8.2 and may not exist on 0.8.0. If
  the host is older than the feature set below, that is itself a finding.

## Probe plugin

Several probes need to observe what the Herdr **server** passes to a plugin command,
which cannot be inferred from a shell. Build a throwaway plugin first:

```
.plans/scratch/herdr-probe/
  herdr-plugin.toml
  probe.sh          # chmod +x
  bin/hello         # chmod +x, for probe 10
```

`probe.sh` prints to stdout (captured by `herdr plugin log list`) and appends the same
text to `$HERDR_PLUGIN_STATE_DIR/probe.log`:

- `date -Is`, `$HERDR_PLUGIN_EVENT`, `$HERDR_PLUGIN_ACTION_ID`
- `$HERDR_PLUGIN_EVENT_JSON` and `$HERDR_PLUGIN_CONTEXT_JSON` verbatim
- `env | grep '^HERDR_' | sort`
- `pwd` — confirms the cwd really is the plugin root
- `echo "PATH=$PATH"`, then `command -v deno`, `command -v node`, and `command -v git`,
  each falling back to `echo NO-<tool>`

Manifest: one `[[actions]]` entry with `contexts = ["global"]`, a second action whose
`command = ["./bin/hello"]` for probe 10, plus one `[[events]]`
block for each of the seven events the implementation plan hooks
(`workspace.created`, `workspace.closed`, `workspace.renamed`, `workspace.moved`,
`workspace.reordered`, `workspace.updated`, `workspace.focused`), all running
`["./probe.sh"]`.

`herdr plugin link .plans/scratch/herdr-probe`, and `herdr plugin unlink` it when the
discovery is done.

## Checklist

- [x] **1. Herdr version and CLI surface.** `herdr --version`; confirm `herdr api
      snapshot`, `herdr workspace list`, and `herdr plugin log list` all exist. Record
      the version and any command that is missing.

- [x] **2. `api snapshot` shape.** `herdr api snapshot | jq '.'` — record the envelope
      (is it `{"result":{…}}` or bare?), the key holding the focused workspace id, and
      one complete workspace record verbatim. Specifically: does a workspace record
      carry **`cwd`**? What are the field names for id and label? Is there an explicit
      order/position field?

- [x] **3. `workspace list` shape and ordering.** `herdr workspace list | jq '.'`.
      Compare its array order against the Herdr sidebar top-to-bottom, then drag a Space
      to a new sidebar position and compare both `workspace list` and `api snapshot`
      again. Record **which call, if either, is sidebar order** — the implementation
      plan reads order from `api snapshot` and needs to know if that is wrong.

- [x] **4. Space path fallback.** Only if probe 2 found no `cwd` on workspace records:
      `herdr pane list --workspace <id> | jq '.'` and record where a usable directory
      path lives, including for the worktree Space.

- [x] **5. Labels.** For a Space created without `--label`, record whether `label` is
      empty, null, or auto-derived from the directory basename. This decides whether the
      folder `name` field is emitted by default.

- [x] **6. Manifest accepts the event names.** `herdr plugin link` the probe with all
      seven `[[events]]` blocks. If validation fails, bisect by removing one block at a
      time and record **exactly which event names are rejected** and the error text.

- [x] **7. Which events actually fire.** With the probe linked, perform each action below
      and after each one run `herdr plugin log list --plugin <probe-id> --limit 5`.
      Record fired/not-fired per event, and the `HERDR_PLUGIN_EVENT` spelling (the
      0.8.0 notes observed dotted subscription names but an underscored `event` field in
      the payload):
      - `herdr workspace create --cwd ~/some/repo --label demo --no-focus` → expect
        `workspace.created`
      - `herdr workspace rename <id> renamed` → expect `workspace.renamed`
      - drag a Space in the sidebar → expect `workspace.moved` or `workspace.reordered`
      - switch Spaces → expect `workspace.focused`
      - `herdr worktree create --workspace <id> --branch probe/x` → expect
        `workspace.created`
      - `herdr workspace close <id>` → expect `workspace.closed`
      - Record whether **`workspace.updated`** ever fires, and what triggered it. If
        nothing plausibly triggers it, say so — it may be droppable from the manifest.

- [x] **8. Event payload shape.** Paste one full `HERDR_PLUGIN_EVENT_JSON` for a
      workspace event. Note whether it carries a full workspace record (which could let
      the implementation skip the `api snapshot` call) or only ids.

- [x] **9. `HERDR_PLUGIN_CONTEXT_JSON` shape.** Paste it verbatim from both an action
      invocation and an event hook. Specifically: does it name the **focused workspace
      id**? If it does, mode `active` may not need `api snapshot` at all.

- [x] **10. Server environment and relative-path spawn.** The implementation compiles a
      standalone binary with `deno compile` precisely so no interpreter needs to be on
      the server's `PATH`, and invokes it as `["./bin/herdr-vscode-sync", …]`. Two things
      to confirm, plus one to record:
      - **Relative-path spawn works.** Drop any small executable at
        `.plans/scratch/herdr-probe/bin/hello` (`#!/bin/sh` + `echo ok` is enough,
        `chmod +x`), add an action whose `command = ["./bin/hello"]`, invoke it, and
        confirm from the plugin log that it ran. **This is a blocker if it fails** — the
        whole build design assumes cwd is the plugin root.
      - **`PATH` independence is real.** From the probe output, record the `PATH` the
        server passes to plugin commands, and whether `command -v deno`, `command -v
        node`, and `command -v git` resolve under it. The expected finding is that the
        server's `PATH` is minimal and the compiled binary does not care — record it
        either way, because it also tells the implementation whether `$HERDR_BIN_PATH`
        is genuinely the only reliable way to reach Herdr.
      - **Deno on the host, for building.** `deno --version` from a login shell. Record
        the version and its absolute path. `herdr plugin install` runs `[[build]]`, so
        Deno is a prerequisite for installing from GitHub; Deno 2.x is the minimum.

- [x] **11. Events with no client attached.** Detach the client (`ctrl+b q`), run
      `herdr workspace create …` from a plain terminal, reattach, and check the probe
      log. Record whether the hook fired while detached.

- [x] **12. Startup hook timing.** Add a `[[startup]]` block to the probe, restart the
      Herdr server, and record whether it ran and what `HERDR_PLUGIN_EVENT` it received.

- [x] **13. VS Code live-reload — the go/no-go.** With the `.code-workspace` open and
      Herdr running in that window's integrated terminal, hand-edit the `folders` array
      and save, once per case. After each, record whether the explorer updated, whether
      the **window reloaded**, and whether the **integrated terminal survived**:
      - append a folder at the end
      - remove a folder from the middle
      - reorder two folders
      - change `folders[0]`
      - add a folder with a `"name"` field
      Record the VS Code version. **If changing `folders[0]` reloads the window, mode
      `active` is not viable** — say so explicitly in the facts doc.

- [x] **14. VS Code write-back.** After VS Code has applied the edits above, diff the
      file against what was written. Record whether VS Code rewrote it — converting
      absolute paths to relative, reordering keys, or dropping comments. This decides
      whether the plugin fights the editor over path form.

- [x] **15. Extension-host churn.** While making the probe 13 edits, watch the VS Code
      window for extension reactivation (Git reindex, language server restart, terminal
      cwd changes). Record what visibly restarted, qualitatively. This is the cost the
      user pays per Space change and informs the pinned-`folders[0]` recommendation.

- [x] **16. Reconcile.** Update `.plans/vscode-workspace-sync.md` for every contradicted
      assumption and add the `## Discovery corrections` section described above.

- [x] **17. Clean up.** `herdr plugin unlink <probe-id>`, remove the demo and worktree
      Spaces created during probing (`herdr worktree remove --workspace <id>` for the
      worktree — `workspace close` alone leaves the checkout), and delete
      `.plans/scratch/`.

## Validation

- [x] `docs/herdr-vscode-sync-facts.md` exists and has a section per probe 1–15, each
      containing pasted command output rather than prose summary
- [x] Probes 2, 3, and 10 each state a definite answer — these three gate the
      implementation and "unclear" is not an acceptable result for them
- [x] Probe 10 states explicitly whether a plugin-root-relative `command` spawns, and
      records the host's Deno version
- [x] Probe 7 lists all seven events with a fired/not-fired verdict each
- [x] Probe 13 states go or no-go for VS Code live reload, and separately whether mode
      `active` is viable
- [x] `.plans/vscode-workspace-sync.md` has a `## Discovery corrections` section, or an
      explicit line saying every assumption held
- [x] `herdr plugin list` no longer shows the probe plugin, and `.plans/scratch/` is gone
- [x] `herdr workspace list` shows only the Spaces that existed before discovery started

## Relevant Files

Created:

- `docs/herdr-vscode-sync-facts.md` — the deliverable
- `.plans/scratch/herdr-probe/herdr-plugin.toml` — throwaway, deleted by probe 17
- `.plans/scratch/herdr-probe/probe.sh` — throwaway, deleted by probe 17
- `.plans/scratch/herdr-probe/bin/hello` — throwaway, deleted by probe 17

Modified:

- `.plans/vscode-workspace-sync.md` — `## Discovery corrections`, plus edits to
  "Reading Herdr state", "Events to hook", the manifest block, and Risks wherever a
  finding contradicts them
- `.plans/PLAN.md` — move this entry to Completed and mark phase 1 complete
- `docs/herdr-research-notes.md` — fold in anything that generalises beyond this plugin
  (event payload shapes, server PATH behaviour, relative-path spawn, `api snapshot`
  shape); the notes are
  the durable record and the facts doc is plugin-specific

Read but not modified:

- `.plans/vscode-workspace-sync.md` — the assumptions under test
- `.claude/skills/herdr-plugin-authoring/SKILL.md` — probe plugin manifest and CLI
- `docs/example-vscode-workspace.md` — shape of the file being edited in probe 13
