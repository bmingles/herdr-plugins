# vscode-workspace-sync

A Herdr plugin that keeps the `folders` array of a VS Code multi-root
`.code-workspace` file in sync with Herdr Spaces (workspaces).

> **Prerequisite: DONE.** [`vscode-workspace-sync-discovery.md`](vscode-workspace-sync-discovery.md)
> ran on 2026-08-26 against Herdr 0.8.0 / protocol 19 and VS Code 1.134.0. All three
> previously-guessed decisions are now settled: Space state is read as corrected under
> "Reading Herdr state", all seven hooked events fire, and mode `active` **is** viable
> given a pinned `folders[0]`. Read
> [`docs/herdr-vscode-sync-facts.md`](../docs/herdr-vscode-sync-facts.md) and the
> `## Discovery corrections` section below before writing code — the JSON shapes in this
> plan have been updated in place, so anything *not* listed as corrected held as written.

## Discovery corrections

Discovery ran on 2026-08-26 against **Herdr 0.8.0 / protocol 19** and **VS Code 1.134.0**
on macOS arm64. Full evidence in
[`docs/herdr-vscode-sync-facts.md`](../docs/herdr-vscode-sync-facts.md). Assumptions that
changed:

1. **`api snapshot` has an extra `snapshot` level.** The plan read
   `.result.focused_workspace_id` / `.result.workspaces`. The real paths are
   **`.result.snapshot.focused_workspace_id`** and **`.result.snapshot.workspaces`**
   (`.result.type == "session_snapshot"`). "Reading Herdr state" is corrected.

2. **Workspace records do not carry `cwd`.** The plan set each Space's `path` from "the
   record's `cwd`"; no such field exists on any workspace record. Paths come from
   **`.result.snapshot.panes[]`** (joined on `workspace_id`) — already in the same
   snapshot, so no extra call — or, for a single Space in a hook, from
   **`HERDR_PLUGIN_CONTEXT_JSON.workspace_cwd`**, which is more stable because pane `cwd`
   drifts when the user `cd`s. `worktree.checkout_path` exists on some records but
   attaches **lazily** and must not be relied on.

3. **`api snapshot` order is sidebar order — confirmed.** `workspace list` agrees with it,
   and `number` is a 1-based sidebar position that re-sequences on reorder. The plan's
   choice was right; it is now verified rather than assumed.

4. **All seven hooked events fire, and `workspace.updated` is not droppable.** Manifest
   validation accepted all seven names with no error. `workspace.updated` fires when
   Herdr attaches worktree metadata to an already-open Space. Two sources of extra
   invocations were observed and are harmless given the recompute-from-scratch design: a
   move re-emits `workspace.focused`, and `worktree create` emits `workspace.updated` +
   `workspace.created`.

5. **Event payloads cannot replace the `api snapshot` call.** `moved` and `reordered` do
   carry the full ordered workspace array, but **no payload carries `cwd`**, and
   `renamed` / `focused` carry only ids. The "never read the event payload, always
   recompute" rule is therefore *forced*, not merely tidy.

6. **`HERDR_PLUGIN_CONTEXT_JSON` names the event's Space, not the focused one.** The
   discovery doc hypothesised that context might let mode `active` skip `api snapshot`.
   It does not: a Space created with `--no-focus` still appeared as context
   `workspace_id`. Only on **`workspace.focused`** is the subject also the focused Space.
   Everything else must read `.result.snapshot.focused_workspace_id`.

7. **Labels are auto-derived from the directory basename and are never empty.** They are
   also **not unique** (two Spaces held `"devc-wksp"` simultaneously). Consequence:
   `useSpaceLabels` + "emit `name` only when the label differs from the basename" means
   **`name` is almost never emitted by default**, and `excludeLabels` must tolerate
   duplicate matches.

8. **The server's `PATH` was the opposite of predicted.** The plan asserts a minimal
   server `PATH`; the observed server had inherited a full interactive `PATH` (from the
   VS Code integrated terminal that launched it) and `deno`, `node`, and `git` all
   resolved. The compiled-binary design is still correct, but its justification changes
   from *"the server's `PATH` is minimal"* to **"the server's `PATH` is whatever launched
   it, therefore unknowable"**. The `env -i` offline test remains exactly right.

9. **Relative-path spawn confirmed.** `command = ["./bin/hello"]` ran with cwd set to the
   plugin root, for both actions and event hooks. `$HERDR_BIN_PATH` is provided
   (`/Users/bingles/.local/bin/herdr`). Host Deno is **2.9.5** at
   `/Users/bingles/.deno/bin/deno` — same version as the devcontainer.

10. **Startup hooks: `HERDR_PLUGIN_EVENT` is the literal `startup`, and
    `HERDR_PLUGIN_EVENT_JSON` is unset.** Code that unconditionally parses the event JSON
    will crash on startup. Context *is* supplied, with
    `invocation_source: "startup"`. Also: a session's *initial* Space emits no
    `workspace.created`.

11. **Event hooks fire with no client attached** — confirmed, so the file stays in sync
    while the user is detached.

12. **NEW RISK — plugins are global across sessions.** Registration lives in
    `~/.config/herdr/plugins.json`, which is not session-scoped: one linked plugin ran in
    a second session's server too. Two Herdr sessions would mean two servers writing the
    same `workspaceFile` from different Space lists. See Risks.

13. **VS Code live-reload is a GO, and mode `active` is viable.** No folder mutation
    caused a window reload, and **every integrated terminal survived every edit** —
    including the one running Herdr. But `folders[0]` is genuinely special: replacing it
    restarts the extension host, while any change at index ≥ 1 does not. A second run
    isolated the trigger precisely: **the extension host restarts if and only if the path
    at `folders[0]` changes** — by replacement, by a reorder that swaps a different folder
    into first place, by removing the first folder, or by inserting one ahead of it. A pure
    reorder of indices 1↔2 costs nothing, so it is not "the set changed" or "the file was
    written" but specifically the identity of index 0. Unpinned remains **supported and
    correct** — nothing breaks, no terminal dies — so pinning is a strong recommendation
    for `active` mode, not an enforced constraint.

14. **VS Code rewrites the whole file when *it* writes.** Confirmed via `code --add`: the
    comment was deleted, trailing commas stripped, every folder object expanded to
    one-property-per-line, and the newly added path written **relative** to the workspace
    file's directory. VS Code does *not* reformat in response to the plugin's own writes.
    Consequence: **reading must resolve relative paths** against `dirname(workspaceFile)`,
    or the unchanged-content check will rewrite the file on every run after any UI edit.

Assumptions that **held** as written: plugin-root cwd; `min_herdr_version = "0.8.0"`
(`api snapshot` exists at 0.8.0); `workspace.metadata_updated` correctly not hooked
(though for a different reason — it carries only display-only badge tokens); startup hooks
not running on `plugin link`; and VS Code preserving the file byte-for-byte when the
plugin writes it.

Workflow it serves: open the `.code-workspace` file in VS Code, run `herdr` in the
integrated terminal, and navigate Herdr normally. Creating, closing, renaming, or
reordering a Space rewrites the workspace file, and VS Code picks up the new root
folders without a window reload.

Two modes, both implemented; `mirror` is the default:

- **`mirror`** (preferred) — `folders` mirrors the full ordered Space list.
- **`active`** — `folders` contains only the focused Space.

## Context and constraints

- Written against the Herdr **0.8.2** docs (`https://herdr.dev/llms.txt`, pinned to
  `v0.8.2`), then **verified by observation on Herdr 0.8.0 / protocol 19** — the host's
  actual version. `herdr api snapshot` exists at 0.8.0, so `min_herdr_version = "0.8.0"`
  is correct. The plugin manifest surface is unchanged between the two.
- "Space" is the Herdr UI name for a workspace. This doc says **Space** for the Herdr
  concept and **workspace file** for the VS Code `.code-workspace` document, because
  both would otherwise be "workspace".
- Plugin commands are spawned by the **Herdr server**, with the plugin root as cwd.
  They do not inherit the environment of the integrated terminal, so nothing about the
  VS Code window is discoverable from the process environment. The target workspace
  file is therefore **configuration**, not detection (see Rejected alternatives).
- The implementing agent will most likely be inside this devcontainer, where `herdr`
  does not exist and cannot be installed, but Deno **2.9.5** is on `PATH` at
  `/usr/local/bin/deno`. Everything in **Validation → Offline** must pass there,
  compilation included. Everything in **Validation → Host** requires a macOS/Linux host
  with Herdr and VS Code and is run by the user.
- The compiled binary is built for the machine that builds it. One compiled in this
  devcontainer (linux/arm64) runs the offline validation here and is **not** the artifact
  the macOS host will use — the host rebuilds via `[[build]]` on install, or
  `deno task build` when developing locally.

## Design

### Runtime and build

TypeScript on Deno, compiled to a standalone executable with `deno compile`. The
manifest invokes `./bin/herdr-vscode-sync`, a plugin-root-relative path, which resolves
because Herdr runs plugin commands with the plugin root as their cwd.

This is what removes the interpreter-on-`PATH` problem. Plugin commands are spawned by
the Herdr **server**, and **the server's `PATH` is whatever launched it — therefore
unknowable.** Discovery measured a server started from a VS Code integrated terminal: it
had inherited a full interactive `PATH` and `deno`, `node`, and `git` all resolved. A
server started from launchd, systemd, a login item, or a bare `herdr` in a non-login shell
carries something else entirely, quite possibly excluding a Homebrew, nvm, mise, or asdf
toolchain. `["node", …]` — or `["deno", "run", …]`, whose default install location
`~/.deno/bin` is even less likely to be present — would fail to spawn on those hosts,
visible only in `herdr plugin log list`. A compiled binary needs nothing on `PATH` but
itself, so it is correct on all of them.

Deno is therefore a **build-time** requirement only:

```toml
[[build]]
command = ["deno", "task", "build"]
```

with `deno.json` defining:

```jsonc
"tasks": {
  "build": "deno compile --allow-read --allow-write --allow-env --allow-run --output bin/herdr-vscode-sync src/main.ts",
  "test":  "deno test --allow-read --allow-write --allow-env --allow-run",
  "check": "deno check src/ test/ && deno lint && deno fmt --check"
}
```

Permissions are baked in at compile time and cannot be scoped tighter than this: the
paths the plugin reads and writes, and the Herdr binary it executes, all arrive from
config and environment at runtime, so there is no compile-time path list to pin them to.
The README states this plainly rather than leaving a broad grant unexplained.

`bin/` is gitignored — the artifact is ~80 MB and platform-specific. `herdr plugin
install` runs `[[build]]` on the target machine, so an installed plugin compiles
natively and no cross-compilation or committed binary is involved. **`herdr plugin link`
does not run build commands**, so local development runs `deno task build` by hand after
every source change; a forgotten build shows up as a spawn error in the plugin log.

Minimum Deno 2.x. macOS binaries are ad-hoc signed by `deno compile` and are not
quarantined when built locally.

### Architecture

One entrypoint, `src/main.ts`, invoked three ways by the manifest:

1. `[[startup]]` — once when the Herdr server starts or takes over a live handoff, so
   the workspace file matches the restored session. On this invocation
   `HERDR_PLUGIN_EVENT` is the literal string `startup` and **`HERDR_PLUGIN_EVENT_JSON`
   is unset** — never parse it unconditionally. `HERDR_PLUGIN_CONTEXT_JSON` *is* set,
   with `invocation_source: "startup"`.
2. `[[events]]` — one hook per Space lifecycle event.
3. `[[actions]]` — `sync` for a manual resync, `doctor` for diagnostics.

Every invocation does the same thing: read the authoritative Herdr state, compute the
desired `folders` array, and rewrite the file only if the result differs. The script
**never reads the event payload to decide what changed** — it recomputes from scratch.
That makes it idempotent, makes redundant invocations free, and means a missed or
unknown event cannot leave stale state.

Herdr is reached through `$HERDR_BIN_PATH` (falling back to `herdr` on `PATH`), not the
raw socket, per the plugin docs' portability guidance.

### Events to hook

One `[[events]]` block per event (`on` takes a single event name):

| Event | Why |
| --- | --- |
| `workspace.created` | new Space → add folder |
| `workspace.closed` | closed Space → remove folder |
| `workspace.renamed` | label feeds the folder `name` |
| `workspace.moved` | single-Space reorder |
| `workspace.reordered` | atomic multi-Space reorder |
| `workspace.updated` | cwd / worktree provenance change |
| `workspace.focused` | required by `active` mode; a no-op in `mirror` mode |

All seven were confirmed to fire on 0.8.0, and `herdr plugin link` accepted all seven
names with no validation error.

`workspace.metadata_updated` is deliberately **not** hooked — it carries only display-only
badge tokens (`workspace.report_metadata` takes a ≤16-key string map), so it has no path,
label, or order data to sync.

`workspace.focused` fires on every Space switch and spawns the binary each time
(~40 ms, exits without writing). That is the accepted cost of a static manifest.

Two observed sources of **extra** invocations, both harmless because every run recomputes
from scratch — recorded so the plugin log is not misread as buggy:

- a reorder emits `workspace.moved` (or `workspace.reordered`) and then **re-emits
  `workspace.focused`**;
- `herdr worktree create` emits **`workspace.updated` + `workspace.created`**, the
  `updated` often for a *different* Space than the one being created.

### Reading Herdr state

`herdr api snapshot` is the single source of truth: one call returns the focused
workspace id, the workspace records, **and the pane records** together. **Observed shape
(Herdr 0.8.0, protocol 19)** — note the `snapshot` level:

```jsonc
{ "id": "cli:api:snapshot",
  "result": {
    "type": "session_snapshot",
    "snapshot": {
      "version": "0.8.0", "protocol": 19,
      "focused_workspace_id": "w4",
      "workspaces": [
        { "workspace_id": "w4", "number": 2, "label": "herdr-plugins",
          "focused": true, "active_tab_id": "w4:t3", "pane_count": 2,
          "tab_count": 2, "agent_status": "working" }        // <- NO cwd
      ],
      "panes": [
        { "pane_id": "w4:p1", "workspace_id": "w4", "tab_id": "w4:t1",
          "cwd": "/Users/bingles/code/tools/herdr-plugins",  // <- the path lives here
          "foreground_cwd": "/Users/bingles/code/tools/herdr-plugins", "focused": false }
      ]
    } } }
```

Parse `.result.snapshot.focused_workspace_id` and `.result.snapshot.workspaces`. Array
order **is** sidebar order (verified against a reorder); `number` is a redundant 1-based
sidebar position.

**Workspace records carry no `cwd`.** Each Space is reduced to `{ id, label, path }`
where `path` comes from `.result.snapshot.panes[]` joined on `workspace_id`. A Space may
have several panes, so pick deterministically — the pane whose `tab_id` matches the
Space's `active_tab_id`, else the lowest `pane_id`. Ignore `foreground_cwd`.

Both `cwd` and `foreground_cwd` **drift** when the user `cd`s inside a pane, so neither is
a stable Space root. The stable value is `HERDR_PLUGIN_CONTEXT_JSON.workspace_cwd`, which
the server computes per invocation; prefer it for the Space named in a hook and fall back
to the pane join for all the others. `worktree.checkout_path` appears on some records but
attaches lazily and must not be used.

Copy these shapes into `vscode-workspace-sync/README.md` under "Herdr JSON shapes" so the
plugin documents its own contract.

For offline development and tests, `HERDR_VSCODE_SYNC_FAKE_SNAPSHOT=<path>` makes the
script read that file instead of executing Herdr. This is a supported contract, not a
test-only hack — `doctor` honours it too.

### Computing `folders`

```
candidates = configured pinnedFolders, in order
           + mirror: every Space in sidebar order
             active: the focused Space only (empty if none)
```

Applied in this order:

1. `path.resolve` each path; strip a trailing separator. No symlink resolution —
   `realpath` would rewrite `/tmp` to `/private/tmp` on macOS and surprise the user.
2. Drop entries whose path is not an existing directory.
3. Drop Spaces excluded by `excludePaths` (path equal to, or nested inside, a listed
   path — matched on a separator boundary, not a bare string prefix) or by
   `excludeLabels` (exact label match). Pinned folders are never excluded.
4. Deduplicate by resolved path, first occurrence wins.

Each surviving entry renders as `{ "path": "<abs>" }`, or
`{ "path": "<abs>", "name": "<label>" }` when `useSpaceLabels` is true, the Space label
is non-empty, and the label differs from the path's basename. Pinned folders never get
a `name`.

Herdr **auto-derives a Space's label from its directory basename** when none is given, and
labels are never empty or null. So with the default config the "label differs from
basename" test fails for most Spaces and **`name` is rarely emitted** — only for Spaces the
user explicitly labelled, or worktree Spaces whose label is the branch. That is the
intended behaviour; it is called out so an implementer does not read the near-absence of
`name` as a bug. Labels are also **not unique**, so `excludeLabels` may match several
Spaces at once.

**Safety valve:** if the computed list is empty, log a warning and write nothing.
Emitting `"folders": []` would blank the VS Code explorer, and an empty result is far
more likely to mean "Herdr returned something unexpected" than "the user wants no
folders".

### Rewriting the file

The workspace file is JSONC and is the user's — comments, trailing commas, key order,
and every member other than `folders` must survive byte for byte. So: no parse and
re-serialize. Instead, a small tokenizer locates the top-level `"folders"` member's
value span and the text is spliced.

`src/jsonc.ts` exports:

- `findTopLevelMember(text, key)` → `{ keyStart, valueStart, valueEnd }` or `null`.
  Walks the text tracking string state (with `\` escapes), `//` line comments,
  `/* */` block comments, and brace/bracket depth, so a `]` inside a string or comment
  cannot terminate the array. Only depth-1 members of the root object match.
- `stripComments(text)` → the same tokenizer, replacing comment spans with spaces, used
  to parse the plugin's own config file so it may contain comments.

Rendered array, exactly:

```jsonc
"folders": [
  { "path": "/abs/one" },
  { "path": "/abs/two", "name": "api" }
]
```

- Base indent is copied from the line on which `"folders"` starts; entry indent is base
  plus two spaces; the closing `]` sits at base indent.
- **No trailing comma** after the last entry, even if the file's original array had one.
  The plugin owns this array outright and strict JSON is safer for other tooling. The
  rest of the file keeps whatever style it had.
- One entry per line, `{ "path": ... }` inline as shown.
- Paths and names are emitted with `JSON.stringify` so escaping is correct.
- If the file has no top-level `folders` member, insert `"folders": [...],` as the
  **first** member of the root object, at the root's inner indentation.
- If the target file does not exist, log the resolved path and exit non-zero. Do not
  create it — a typo in config must not silently produce a stray workspace file.

Writing:

1. `fs.realpathSync` the target first, so a symlinked workspace file is replaced through
   the link rather than having the link clobbered.
2. Before the first write of a session, copy the original to
   `$HERDR_PLUGIN_STATE_DIR/backup/<basename>.<iso-timestamp>`; keep the newest 10.
3. Write to a temp file in the same directory, `fchmod` it to the original's mode, then
   `fs.renameSync` over the target — atomic, and VS Code's watcher sees one event.
4. Skip the write entirely when the rendered text equals the current file text.

**The unchanged-check must compare resolved paths, not raw text.** VS Code writes newly
added folders as paths **relative** to the workspace file's directory (confirmed via
`code --add`), so after any UI folder-add the file holds relative paths that are
equal-but-not-identical to the plugin's absolute ones. A pure text compare would then
rewrite the file on *every* run. Parse the existing `folders` array, resolve each `path`
against `dirname(workspaceFile)`, and compare the resolved lists; fall back to a text
compare only if the existing array cannot be parsed.

### Concurrency

A burst of events means several processes racing on one file. Serialize with a lock
file at `$HERDR_PLUGIN_STATE_DIR/sync.lock`, created `wx` with `{pid, startedAt}`:

- Poll for the lock for up to 5000 ms (50 ms interval).
- Break a lock whose `startedAt` is older than 30 000 ms, logging that it did so.
- On timeout: log and exit **0**. The last event of a burst will still get the lock, and
  every holder re-reads Herdr state after acquiring it, so a late run is always fresh.
- Release in a `finally`, and on `SIGTERM`/`SIGINT`.

`debounceMs` (default `0`) sleeps after acquiring the lock and before reading state, for
users who want to coalesce heavy bursts.

### Configuration

`$HERDR_PLUGIN_CONFIG_DIR/config.json`, parsed after `stripComments`, so comments are
allowed. `config.example.json` in the plugin root documents it.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `workspaceFile` | string | — | **Required.** Absolute path to the `.code-workspace` file. `~` is expanded. |
| `mode` | `"mirror"` \| `"active"` | `"mirror"` | Folder computation mode. |
| `pinnedFolders` | string[] | `[]` | Absolute paths always emitted first. |
| `excludePaths` | string[] | `[]` | Spaces at or under these paths are skipped. |
| `excludeLabels` | string[] | `[]` | Spaces with these exact labels are skipped. |
| `useSpaceLabels` | boolean | `true` | Emit `name` when the label differs from the basename. |
| `debounceMs` | number | `0` | Sleep after acquiring the lock, before reading state. |
| `sessionSocket` | string \| null | `null` | Only sync when `$HERDR_SOCKET_PATH` equals this. `null` means the default session's socket only. Guards against two Herdr sessions fighting over one workspace file — see Risks. |

`HERDR_VSCODE_SYNC_WORKSPACE_FILE` overrides `workspaceFile`. Any unknown key is a
warning, not an error. A missing or unreadable config file, or a missing
`workspaceFile`, exits non-zero with a message naming
`$HERDR_PLUGIN_CONFIG_DIR/config.json` and the `herdr plugin config-dir
vscode-workspace-sync` command that prints it.

**Pinning at least one folder is strongly recommended, especially for `mode: "active"` —
but it is never enforced.** An unpinned config is fully supported and correct in both
modes; the only cost is performance. Do **not** turn this into a validation error.
Discovery measured `folders[0]` to be genuinely special: replacing it
restarts the window's extension host (every language server and the Git extension
reactivate), while replacing any later index costs nothing. An unpinned `active` mode —
whose single folder *is* `folders[0]` — would restart the extension host on **every Space
switch**. With a pinned folder holding index 0, `active` only ever rewrites index 1+ and is
free.

Nothing functional depends on it: no window reload, no lost editors, no lost terminals —
only the window's extension host and its language servers reactivating. In `mirror` mode
that happens just when the *first* Space changes, which many users will barely notice. In
`active` mode it happens on **every Space switch**, which is why the recommendation is
much stronger there.

Therefore: if `mode` is `"active"` and `pinnedFolders` is empty, `--doctor` and every run
print a prominent warning naming this cost — **and then proceed normally**. Keeping
`folders[0]` stable also keeps `${workspaceFolder}` stable.

### Logging

Hook stdout is invisible except through `herdr plugin log list`, so every run prints one
summary line to stdout — `reason`, mode, target file, folder count, and one of
`wrote` / `unchanged` / `skipped-empty` / `lock-timeout`. Failures print to stderr and
exit non-zero.

`--doctor` prints resolved config, resolved target path and whether it exists, the raw
Herdr snapshot summary, the computed folder list, and a unified-style preview of the
`folders` splice — then exits without writing.

### Manifest

```toml
id = "vscode-workspace-sync"
name = "VS Code Workspace Sync"
version = "0.1.0"
min_herdr_version = "0.8.0"
description = "Keep a VS Code multi-root workspace file in sync with Herdr Spaces"
platforms = ["macos", "linux"]

[[build]]
command = ["deno", "task", "build"]

[[startup]]
command = ["./bin/herdr-vscode-sync", "--reason", "startup"]

[[events]]
on = "workspace.created"
command = ["./bin/herdr-vscode-sync", "--reason", "event"]
# ... one block per event in the table above

[[actions]]
id = "sync"
title = "Sync VS Code workspace"
contexts = ["global", "workspace"]
command = ["./bin/herdr-vscode-sync", "--reason", "action"]

[[actions]]
id = "doctor"
title = "VS Code sync diagnostics"
contexts = ["global"]
command = ["./bin/herdr-vscode-sync", "--doctor"]
```

**No runtime dependencies.** The JSONC tokenizer is hand-written (see Rejected
alternatives) and everything else is Deno built-ins, so the compiled binary is
self-contained and `deno compile` works with no network. `@std/assert` is a test-only
dependency, pinned in a committed `deno.lock`.

Windows is excluded from `platforms`: path rendering and the Herdr named-pipe transport
are untested there.

## Gotchas

- **`command` is argv, not a shell line.** No expansion, no `~`, no `$VAR`.
- **The server's `PATH` is whatever launched the server.** It may be a full interactive
  `PATH` (measured: a server started from a VS Code integrated terminal had `deno`,
  `node`, and `git` all resolvable) or nearly empty under launchd. Because it is
  unknowable, the manifest invokes a compiled binary by plugin-root-relative path. Do not
  add a hook that shells out to a tool assuming it is on `PATH` — and do not be reassured
  by it working on your own machine; reach Herdr through `$HERDR_BIN_PATH` and nothing
  else.
- **`herdr plugin link` does not run `[[build]]`.** Run `deno task build` after every
  source change during local development, or the hooks spawn a binary that is stale or
  absent.
- **The binary is gitignored and platform-specific.** Never commit `bin/`; never copy one
  between machines or architectures.
- **Hook stdout goes nowhere visible.** `herdr plugin log list --plugin
  vscode-workspace-sync` after every trigger is the only way to see it.
- **`platforms = []` is an error**, not a wildcard.
- **Startup hooks are one-shot, not daemons.** They run after session restore and again
  on live handoff, but not on client attach, config reload, or `plugin link` (confirmed:
  `plugin link` did not run the block). After linking, invoke the `sync` action once by
  hand.
- **On a startup invocation `HERDR_PLUGIN_EVENT_JSON` is unset** and `HERDR_PLUGIN_EVENT`
  is the bare string `startup`. Parsing the event JSON unconditionally crashes the hook
  that matters most.
- **A session's first Space emits no `workspace.created`.** Only `startup` then
  `workspace.focused` were observed for it. Nothing may be inferred about the Space set
  from `created` events alone.
- **Plugins are registered globally, not per session.** `~/.config/herdr/plugins.json` is
  not session-scoped — a single linked plugin was observed running in a second session's
  server. See Risks.
- **Plugin commands do not inherit the integrated terminal's environment.** Do not try
  to read `TERM_PROGRAM`, `VSCODE_*`, or the pane's env from a hook.
- **The Herdr server and the workspace file must be on the same filesystem.** If VS Code
  is attached to a devcontainer or an SSH remote while Herdr runs on the host, the paths
  in `folders` are host paths — which is what VS Code wants anyway. A Herdr running
  inside the container and a VS Code window on the host is out of scope.
- **VS Code rewrites the *whole* workspace file when the user changes folders through the
  UI.** Measured via `code --add`: the file's comments were **deleted**, trailing commas
  stripped, every folder object re-indented to one property per line, and the added path
  written **relative** to the workspace file's directory. Pre-existing absolute paths were
  left absolute. So the damage is not confined to `folders` — say so in the README:
  manage folders through Herdr, not the VS Code UI. Conversely, VS Code does **not**
  reformat the file in response to the plugin's own writes, so the byte-preserving splice
  is safe.
- **Existing `folders` entries may hold relative paths.** Resolve them against
  `dirname(workspaceFile)` before comparing, or the unchanged-check misfires forever after
  one UI edit.

## Risks

- ~~**VS Code live-reload of `folders` is the load-bearing assumption.**~~ **RESOLVED —
  GO.** On VS Code 1.134.0, appending, removing from the middle, reordering, replacing at
  index ≥ 1, and adding a `name` field all applied with **no window reload**, and **every
  integrated terminal survived every edit**, including the one running Herdr. Order and
  `name` propagate live. Mode `active` is **viable**.
- **Extension-host restarts are real, and keyed exactly to `folders[0]`.** Measured across
  seven cases with controls in both directions: the window's extension host restarts **iff
  the path at index 0 changes** — whether by direct replacement, a reorder that swaps a
  different folder into first place, removal of the first folder, or insertion ahead of it.
  Every mutation at index ≥ 1 (append, remove-middle, replace, reorder 1↔2, add with
  `name`) costs nothing. Notably a reorder of 0↔1 restarts even though the folder *set* is
  unchanged, so the trigger is index-0 identity, not set membership.
  On a restart every language server and the Git extension reactivate and re-index —
  seconds of visible churn on large repos — but it is **not** a window reload, no editors
  or unsaved buffers are lost, and no terminal dies. An unpinned config is therefore
  **supported and correct**; pinning is a strong recommendation for `active` mode (where
  index 0 changes on every switch) and a mild one for `mirror` (where it changes only when
  the first Space does).
- ~~**Herdr JSON shapes are inferred from prose, not observed.**~~ **RESOLVED** — observed
  against 0.8.0 / protocol 19; see Discovery corrections 1–2 and
  `docs/herdr-vscode-sync-facts.md`. The two shapes that actually differed from the plan
  were the `snapshot` envelope level and the absence of `cwd` on workspace records.
- ~~**Plugin event hooks may not fire for every workspace event.**~~ **RESOLVED** — all
  seven hooked events fired, and the manifest validated all seven names. No event needs
  removing.
- **Two Herdr sessions would fight over the workspace file.** Plugin registration is
  global (`~/.config/herdr/plugins.json`), not session-scoped: one linked plugin was
  observed running in a second session's server. Each server would compute `folders` from
  its *own* Space list and overwrite the other's. `sync.lock` serialises the writes but
  cannot reconcile two legitimately different views. Mitigation, in order of preference:
  scope the config to one session by having the plugin exit 0 unless
  `$HERDR_SOCKET_PATH` matches a configured `sessionSocket` (unset = the default session
  only); at minimum, document that the plugin supports one session at a time and have
  `--doctor` report the socket it saw. Single-session users are unaffected.
- **Installing from GitHub requires Deno on the target machine.** `herdr plugin install`
  runs `[[build]]`, so a user without Deno gets a build failure at install time rather
  than a broken plugin at run time. Documented as a prerequisite in the README; the
  plugin docs anticipate exactly this pattern ("document required system tools such as
  `cargo`, `npm`, `bun`, or `lua`").
- ~~**A plugin-root-relative `command` is assumed to resolve.**~~ **RESOLVED** — confirmed
  directly on 0.8.0 for both an action and event hooks; cwd is the plugin root.

## Rejected alternatives

- **Detecting the active VS Code window's workspace file** (parsing
  `windowsState.lastActiveWindow.workspaceIdentifier.configURIPath` from VS Code's
  `globalStorage/storage.json`, or `code --status`). Undocumented, version-fragile, and
  platform-specific, and plugin hooks have no window context to begin with. Explicit
  configuration is the contract; revisit only if the user asks. (Discovery *did* use
  `code --status` as a measurement tool — its "Workspace Stats" section reports which
  folders VS Code has adopted — but it lists them unordered, aggregated across all
  windows, and by directory basename, which is exactly why it is unfit as a runtime
  detection mechanism.)
- **A long-lived `events.subscribe` daemon started from `[[startup]]`.** The plugin docs
  are explicit that startup hooks are one-shot initialization, not supervised daemons —
  nothing restarts a dead subscriber. Per-event hooks are the sanctioned design and
  Space events are human-paced.
- **`jsonc-parser` (what VS Code itself uses) for the edit.** Deno bundles dependencies
  into the compiled binary, so the old objection — an `npm ci` that `plugin link` never
  runs — no longer applies. It is rejected on rendering instead: its
  `modify`/`applyEdits` formats inserted objects one key per line, so entries would come
  out as four lines each rather than the compact `{ "path": … }` form in
  `docs/example-vscode-workspace.md`. This is a file the user reads; exact rendering
  control is worth ~100 lines of tokenizer.
- **`deno run` straight from the manifest.** Needs `deno` on the server's `PATH` — the
  exact problem the compiled binary exists to avoid.
- **A POSIX-sh entrypoint that locates an interpreter before exec'ing.** Solves the
  `PATH` problem only by re-introducing it one level down, and adds a shell layer to
  debug through when a hook silently fails.
- **Parsing and re-serializing the whole workspace file.** Destroys the user's comments
  and formatting.
- **A `pathMap` prefix-rewrite config for remote setups.** Not needed for
  Herdr-in-the-integrated-terminal; noted under Future work.

## Future work

Out of scope, listed so it is not re-litigated: syncing the focused Space's colour into
`settings.workbench.colorCustomizations`; a `pathMap` for remote/container path
translation; Windows support; syncing Herdr tabs to VS Code editor groups.

## Checklist

- [ ] `vscode-workspace-sync/herdr-plugin.toml` — manifest exactly as specified above,
      with the `[[build]]` block and all seven `[[events]]` blocks
- [ ] `vscode-workspace-sync/deno.json` — `build` / `test` / `check` tasks exactly as
      specified, plus `fmt` and `lint` config; no runtime imports
- [ ] `vscode-workspace-sync/deno.lock` — committed, pinning the `@std/assert` test
      dependency
- [ ] `vscode-workspace-sync/.gitignore` — `bin/`
- [ ] `vscode-workspace-sync/src/types.ts` — Herdr JSON shapes as interfaces, transcribed
      from `docs/herdr-vscode-sync-facts.md` probes 2–5 (note the `.result.snapshot`
      level, the absence of `cwd` on workspace records, and the pane join), with a comment
      naming Herdr 0.8.0 / protocol 19 as the version observed
- [ ] `vscode-workspace-sync/src/jsonc.ts` — `findTopLevelMember`, `stripComments`
- [ ] `vscode-workspace-sync/src/config.ts` — load, defaults, `~` expansion, env
      override, unknown-key warnings, validation errors, and the `sessionSocket` guard
      (exit 0 without writing when `$HERDR_SOCKET_PATH` does not match)
- [ ] `vscode-workspace-sync/src/herdr.ts` — run `herdr api snapshot` via
      `new Deno.Command($HERDR_BIN_PATH)` (or read the fake snapshot file) and reduce it
      to ordered `{id,label,path}` records plus `focusedWorkspaceId`
- [ ] `vscode-workspace-sync/src/folders.ts` — pure `computeFolders(spaces, focusedId,
      config)` implementing resolve → exists → exclude → dedupe → `name`
- [ ] `vscode-workspace-sync/src/rewrite.ts` — pure `renderFolders(entries, baseIndent)`
      and `spliceFolders(text, entries)`, including the insert-when-absent path
- [ ] `vscode-workspace-sync/src/write.ts` — realpath, backup rotation (keep 10), temp
      file + mode preservation + atomic rename, unchanged-content skip
- [ ] `vscode-workspace-sync/src/lock.ts` — exclusive create, 5 s wait, 30 s stale break,
      release on exit and on `SIGTERM`/`SIGINT`
- [ ] `vscode-workspace-sync/src/main.ts` — argv (`--reason`, `--doctor`), orchestration,
      one-line stdout summary, exit codes
- [ ] `vscode-workspace-sync/config.example.json` — every key with its default, commented
- [ ] `vscode-workspace-sync/README.md` — Deno build prerequisite, install, `deno task
      build` before `plugin link`, `herdr plugin config-dir`, config table, both modes,
      why the compile-time permissions are broad, the pin-`folders[0]` recommendation,
      the "manage folders in Herdr, not the VS Code UI" warning, and the recorded Herdr
      JSON shapes
- [ ] `vscode-workspace-sync/test/fixtures/` — at minimum: the file from
      `docs/example-vscode-workspace.md`; a file with no `folders` member; a file with a
      `]` inside a string value and inside a comment; a file with block comments between
      members; a file with a non-default indent; **a file with relative `folders` paths and
      one-property-per-line objects, as VS Code itself writes them** (see facts doc probe
      14); `snapshot.json` matching the real `api snapshot` shape from discovery, including
      the `.result.snapshot` level, a `panes` array, and a worktree-backed Space
- [ ] `vscode-workspace-sync/test/jsonc.test.ts`
- [ ] `vscode-workspace-sync/test/folders.test.ts`
- [ ] `vscode-workspace-sync/test/rewrite.test.ts` — asserts non-`folders` bytes are
      unchanged
- [ ] Root `README.md` — add a plugin index entry pointing at the new directory
- [ ] `docs/herdr-research-notes.md` — record the observed `api snapshot` /
      `workspace list` shapes and which event hooks actually fired

## Validation

### Offline — runnable in this devcontainer

- [ ] `cd vscode-workspace-sync && deno task check` passes — type check, lint, and
      `fmt --check` all clean
- [ ] `deno task test` passes
- [ ] `deno task build` produces `bin/herdr-vscode-sync` and `./bin/herdr-vscode-sync
      --doctor` runs
- [ ] **PATH independence:** `env -i HOME=$HOME HERDR_PLUGIN_CONFIG_DIR=…
      HERDR_VSCODE_SYNC_FAKE_SNAPSHOT=… ./bin/herdr-vscode-sync --doctor` succeeds with
      no `PATH` at all — this is the property the whole build design exists to provide,
      so test it rather than assume it
- [ ] Round-trip: for each fixture, splicing back its own existing folders produces a
      byte-identical file
- [ ] A fixture whose `folders` hold **relative** paths (as VS Code emits them) resolves to
      the same set as the absolute equivalent, and reports `unchanged` rather than
      rewriting
- [ ] Comment and trailing-comma preservation: splicing new folders into the
      `docs/example-vscode-workspace.md` fixture leaves the `settings` block, its
      comments, and its trailing commas untouched
- [ ] `]` inside a string value and inside a `//` comment do not terminate the array
- [ ] Insert path: a fixture with no `folders` member gains one as the first root member
      and remains parseable by `JSON.parse(stripComments(text))`
- [ ] Empty computed list writes nothing and exits 0 with `skipped-empty`
- [ ] Missing target file exits non-zero and names the resolved path
- [ ] Missing `workspaceFile` config exits non-zero and names the config path
- [ ] `HERDR_VSCODE_SYNC_FAKE_SNAPSHOT=test/fixtures/snapshot.json
      HERDR_PLUGIN_CONFIG_DIR=… ./bin/herdr-vscode-sync` rewrites a scratch copy of a fixture
      to the expected `folders`, and a second run reports `unchanged`
- [ ] Same command with `--doctor` writes nothing and prints the computed folder list
- [ ] `mode: "active"` yields exactly the focused Space plus any pinned folders
- [ ] Two concurrent `./bin/herdr-vscode-sync` runs against the same scratch file both exit 0 and
      leave valid JSONC (a `deno eval` parse of the result succeeds)

### Host — requires Herdr + VS Code, run by the user

- [x] **Go/no-go** — answered by discovery probe 13: **GO**, no window reload and
      terminals survive; mode `active` is viable with a pinned `folders[0]`. Recorded in
      `docs/herdr-vscode-sync-facts.md`
- [ ] `herdr plugin link ./vscode-workspace-sync` succeeds and
      `herdr plugin list` shows it enabled
- [ ] `herdr plugin action invoke vscode-workspace-sync.doctor` then
      `herdr plugin log list --plugin vscode-workspace-sync --limit 5` shows the resolved
      config and computed folders, exit 0
- [ ] `herdr plugin action invoke vscode-workspace-sync.sync` rewrites the file and the
      VS Code explorer shows one root per Space, in sidebar order
- [ ] `herdr workspace create --cwd ~/some/repo --label demo --no-focus` adds that root
      to the explorer within a second, with no window reload
- [ ] `herdr workspace rename <id> renamed` updates the folder's displayed name
- [ ] `herdr workspace close <id>` removes the root
- [ ] Reordering Spaces in the Herdr sidebar reorders the explorer roots
- [ ] `herdr plugin log list --plugin vscode-workspace-sync --limit 40` shows one run per
      event above — note any hooked event that never appears
- [ ] Restart the Herdr server; the startup hook resyncs the file to the restored session
- [ ] Set `mode: "active"`, switch Spaces, and confirm the explorer follows the focused
      Space
- [ ] A comment and a manual `settings` edit made in the workspace file between two syncs
      both survive
- [ ] After adding a folder through the **VS Code UI** (which rewrites the file and emits a
      relative path), the next sync reports `unchanged` rather than rewriting — proves the
      resolved-path comparison works
- [ ] With `mode: "active"` and a pinned `folders[0]`, switching Spaces does **not**
      restart the extension host; with `pinnedFolders` empty, `--doctor` warns about it
- [ ] Start a second Herdr session (`herdr --session other`) and confirm the plugin syncs
      for only one of them — the other run exits 0 without writing

## Relevant Files

Created:

- `.plans/PLAN.md` — plan index (this plan registered under Pending / phase 1)
- `.plans/vscode-workspace-sync.md` — this document
- `vscode-workspace-sync/herdr-plugin.toml`
- `vscode-workspace-sync/deno.json`
- `vscode-workspace-sync/deno.lock`
- `vscode-workspace-sync/.gitignore`
- `vscode-workspace-sync/config.example.json`
- `vscode-workspace-sync/README.md`
- `vscode-workspace-sync/src/main.ts`
- `vscode-workspace-sync/src/types.ts`
- `vscode-workspace-sync/src/jsonc.ts`
- `vscode-workspace-sync/src/config.ts`
- `vscode-workspace-sync/src/herdr.ts`
- `vscode-workspace-sync/src/folders.ts`
- `vscode-workspace-sync/src/rewrite.ts`
- `vscode-workspace-sync/src/write.ts`
- `vscode-workspace-sync/src/lock.ts`
- `vscode-workspace-sync/test/jsonc.test.ts`
- `vscode-workspace-sync/test/folders.test.ts`
- `vscode-workspace-sync/test/rewrite.test.ts`
- `vscode-workspace-sync/test/fixtures/*.code-workspace`
- `vscode-workspace-sync/test/fixtures/snapshot.json`
- `vscode-workspace-sync/bin/herdr-vscode-sync` (build output, gitignored)
- `README.md` (repo root — does not exist yet; create with a plugin index)

Modified:

- `docs/herdr-research-notes.md` — append observed `api snapshot` / `workspace list`
  JSON shapes and which plugin event hooks fired

Read but not modified:

- `docs/example-vscode-workspace.md` — source of the primary test fixture
- `.claude/skills/herdr-plugin-authoring/SKILL.md` — manifest, env, and CLI reference
