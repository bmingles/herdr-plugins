# vscode-workspace-sync

A Herdr plugin that keeps the `folders` array of a VS Code multi-root
`.code-workspace` file in sync with Herdr Spaces (workspaces).

> **Prerequisite:** [`vscode-workspace-sync-discovery.md`](vscode-workspace-sync-discovery.md)
> runs first, on a host with Herdr and VS Code. Every Herdr JSON shape below is inferred
> from the 0.8.2 docs rather than observed, and three decisions here — how Space state is
> read, which events are hooked, and whether mode `active` is viable — are guesses until
> that plan reports back. Read its `docs/herdr-vscode-sync-facts.md` deliverable and this
> plan's `## Discovery corrections` section before writing code.

Workflow it serves: open the `.code-workspace` file in VS Code, run `herdr` in the
integrated terminal, and navigate Herdr normally. Creating, closing, renaming, or
reordering a Space rewrites the workspace file, and VS Code picks up the new root
folders without a window reload.

Two modes, both implemented; `mirror` is the default:

- **`mirror`** (preferred) — `folders` mirrors the full ordered Space list.
- **`active`** — `folders` contains only the focused Space.

## Context and constraints

- Verified against the Herdr **0.8.2** docs (`https://herdr.dev/llms.txt`, pinned to
  `v0.8.2`). The prior investigation in `docs/herdr-research-notes.md` was against
  0.8.0 / protocol 19; the plugin manifest surface is unchanged between them.
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
the Herdr **server**, which under launchd or systemd typically carries a minimal `PATH`
that excludes a Homebrew, nvm, mise, or asdf toolchain. `["node", …]` — or `["deno",
"run", …]`, whose default install location `~/.deno/bin` is even less likely to be
present — would fail to spawn, visible only in `herdr plugin log list`. A compiled
binary needs nothing on `PATH` but itself.

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
   the workspace file matches the restored session.
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

`workspace.metadata_updated` is deliberately **not** hooked — the socket API docs state
it does not invoke plugin event hooks.

`workspace.focused` fires on every Space switch and spawns the binary each time
(~40 ms, exits without writing). That is the accepted cost of a static manifest.

### Reading Herdr state

`herdr api snapshot` is the single source of truth: one call returns the focused
workspace id and the workspace records together. Parse from its JSON, in order:
`.result.focused_workspace_id` and `.result.workspaces`, falling back to
`.focused_workspace_id` / `.workspaces` if the CLI prints the result unwrapped.

Each Space record is reduced to `{ id, label, path }`, where `path` is the record's
`cwd`. The field names, the `cwd` fallback, and whether the snapshot array is in sidebar
order come from discovery probes 2–5 — do not write this module before reading them.
Copy the confirmed shapes into `vscode-workspace-sync/README.md` under "Herdr JSON
shapes" so the plugin documents its own contract.

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

`HERDR_VSCODE_SYNC_WORKSPACE_FILE` overrides `workspaceFile`. Any unknown key is a
warning, not an error. A missing or unreadable config file, or a missing
`workspaceFile`, exits non-zero with a message naming
`$HERDR_PLUGIN_CONFIG_DIR/config.json` and the `herdr plugin config-dir
vscode-workspace-sync` command that prints it.

Pinning at least one folder is recommended in the README: it keeps `folders[0]` stable,
so `${workspaceFolder}` and the extension host are undisturbed as Spaces come and go.

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
- **The server's `PATH` is not your shell's `PATH`.** That is the whole reason the
  manifest invokes a compiled binary by plugin-root-relative path. Do not add a hook
  that shells out to a tool assuming it is on `PATH`; reach Herdr through
  `$HERDR_BIN_PATH` and nothing else.
- **`herdr plugin link` does not run `[[build]]`.** Run `deno task build` after every
  source change during local development, or the hooks spawn a binary that is stale or
  absent.
- **The binary is gitignored and platform-specific.** Never commit `bin/`; never copy one
  between machines or architectures.
- **Hook stdout goes nowhere visible.** `herdr plugin log list --plugin
  vscode-workspace-sync` after every trigger is the only way to see it.
- **`platforms = []` is an error**, not a wildcard.
- **Startup hooks are one-shot, not daemons.** They run after session restore and again
  on live handoff, but not on client attach, config reload, or `plugin link`. After
  linking, invoke the `sync` action once by hand.
- **Plugin commands do not inherit the integrated terminal's environment.** Do not try
  to read `TERM_PROGRAM`, `VSCODE_*`, or the pane's env from a hook.
- **The Herdr server and the workspace file must be on the same filesystem.** If VS Code
  is attached to a devcontainer or an SSH remote while Herdr runs on the host, the paths
  in `folders` are host paths — which is what VS Code wants anyway. A Herdr running
  inside the container and a VS Code window on the host is out of scope.
- **VS Code may rewrite the workspace file itself** when the user adds or removes a
  folder through the UI, including converting absolute paths to relative ones. The next
  sync will overwrite `folders` with absolute paths. Say so in the README: manage
  folders through Herdr, not the VS Code UI.

## Risks

- **VS Code live-reload of `folders` is the load-bearing assumption.** Adding and
  removing root folders is applied without a window reload in normal use, but this must
  be confirmed on the user's actual VS Code version before the plugin is worth
  finishing — a window reload would kill the integrated terminal running Herdr. The
  first host validation step tests exactly this, and it is the go/no-go gate. If a
  reload does occur, `mirror` mode with pinned folders is still tolerable while `active`
  mode is not, since `active` rewrites on every Space switch.
- **Extension-host restarts on folder changes** are milder but real: some extensions
  re-index when the folder set changes. Pinning `folders[0]` limits the blast radius.
- **Herdr JSON shapes are inferred from prose, not observed.** Discovery probes 2–5 must
  run before the parsing code is trusted.
- **Plugin event hooks may not fire for every workspace event.** The docs only state
  that `workspace.metadata_updated` does not. Discovery probes 6 and 7 check each hooked
  event individually — at manifest-validation time and at runtime — and any that never
  fires gets removed from the manifest and noted in the README.
- **Installing from GitHub requires Deno on the target machine.** `herdr plugin install`
  runs `[[build]]`, so a user without Deno gets a build failure at install time rather
  than a broken plugin at run time. Documented as a prerequisite in the README; the
  plugin docs anticipate exactly this pattern ("document required system tools such as
  `cargo`, `npm`, `bun`, or `lua`").
- **A plugin-root-relative `command` is assumed to resolve.** The docs and the 0.8.0
  probe both say cwd is the plugin root, but the compiled-binary design leans on it
  entirely. Discovery probe 10 confirms it directly.

## Rejected alternatives

- **Detecting the active VS Code window's workspace file** (parsing
  `windowsState.lastActiveWindow.workspaceIdentifier.configURIPath` from VS Code's
  `globalStorage/storage.json`, or `code --status`). Undocumented, version-fragile, and
  platform-specific, and plugin hooks have no window context to begin with. Explicit
  configuration is the contract; revisit only if the user asks.
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
- [ ] `vscode-workspace-sync/src/types.ts` — Herdr JSON shapes as interfaces, populated
      from discovery probes 2–5, with a comment naming the Herdr version they were
      observed against
- [ ] `vscode-workspace-sync/src/jsonc.ts` — `findTopLevelMember`, `stripComments`
- [ ] `vscode-workspace-sync/src/config.ts` — load, defaults, `~` expansion, env
      override, unknown-key warnings, validation errors
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
      members; a file with a non-default indent; `snapshot.json` matching the real
      `api snapshot` shape from discovery
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

- [ ] **Go/no-go** — already answered by discovery probe 13; confirm
      `docs/herdr-vscode-sync-facts.md` records a "go", and that mode `active` was not
      ruled out, before running anything below
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
