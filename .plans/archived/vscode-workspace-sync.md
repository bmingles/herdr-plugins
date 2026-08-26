# vscode-workspace-sync

A Herdr plugin that keeps the `folders` array of a VS Code multi-root
`.code-workspace` file in sync with Herdr Spaces (workspaces).

**Python 3.9+, standard library only, no build step and no dependencies** — so
`herdr plugin install <owner>/<repo>` is the whole install story for a shared plugin. See
[Runtime](#runtime-python-39-stdlib-only-no-build).

> **Prerequisite: DONE.** [`vscode-workspace-sync-discovery.md`](vscode-workspace-sync-discovery.md)
> ran on 2026-08-26 against Herdr 0.8.0 / protocol 19 and VS Code 1.134.0. All three
> previously-guessed decisions are now settled: Space state is read as corrected under
> "Reading Herdr state", all seven hooked events fire, and mode `active` **is** viable
> given a pinned `folders[0]`. Read
> [`docs/herdr-vscode-sync-facts.md`](../../docs/herdr-vscode-sync-facts.md) and the
> `## Discovery corrections` section below before writing code — the JSON shapes in this
> plan have been updated in place, so anything *not* listed as corrected held as written.

## Post-implementation simplification

After the plugin was implemented and validated, it was deliberately cut back. The plan
below still describes the *original* design; these five things were removed as unearned
surface, and the sections describing them are stale:

1. **`lock.py` and the whole concurrency section.** A burst can race, but every run
   recomputes from scratch and finishes with one atomic `os.replace`, so the loser of a
   race is corrected by the next event — and `folders` is regenerable from Herdr by
   definition. The `lock-timeout` summary result is gone with it.
2. **Backup rotation.** The plugin owns exactly one explicitly configured file, and its
   `folders` array is rebuildable from the Space list, so the only genuinely
   non-recoverable content is the user's own `settings`/`launch`/`tasks` — which the
   splice never touches and which `test_rewrite.py` asserts byte-identical.
3. **Four config keys:** `excludePaths`, `excludeLabels`, `debounceMs`, and the
   `sessionSocket` *key*. Config is now `workspaceFile`, `mode`, `pinnedFolders`.
4. **`useSpaceLabels`.** The behaviour it toggled is now always on: emit `name` when a
   Space's label differs from the path basename.
5. **`src/types.py` / `herdr_types.py`.** The eight `TypedDict`s were documentation for a
   type checker this project does not run; the observed JSON shapes are now a comment
   block at the top of `src/herdr.py`, and the two runtime classes (`Space`,
   `FolderEntry`) live there too.

6. **The `doctor` plugin action** (the `--doctor` *flag* stays). A plugin action's stdout
   goes only to `herdr plugin log list`, JSON-escaped — the least legible channel
   available, and a poor route for a diagnostic. The README documents running
   `./bin/sync --doctor` directly instead. The `sync` action stays: it is invoked for its
   effect, and the one-line summary in the log confirms it.
7. **The unified diff preview in `--doctor`** (and the `difflib` import). Replaced by a
   one-line `would write: yes|no`. A dry-run diff earns its place in a destructive tool;
   `folders` is regenerable and the write is atomic, so running `sync` and reading the
   file is the simpler answer.

**Kept deliberately:** the named-session guard, now hardcoded rather than configurable —
plugin registration is global, so without it a second Herdr session would rewrite the
same file from its own Space list and the folders would flap. It is ~10 lines in
`config.named_session`.

8. **Redundant tests and one fixture.** Cut end-to-end CLI cases that only re-covered
   unit-level config errors and folder computation, three of four `--doctor` output
   cases, fixture-specific splice cases already covered by the generic all-fixtures
   property loops, and the `tab-indent` fixture (`four-space-indent` already proves the
   base indent is copied from the `"folders"` line).

   **Kept deliberately:** the eight property loops that iterate every workspace fixture —
   ~95 lines of fixtures buying the broadest coverage in the suite — and in particular
   `test_non_folders_bytes_are_unchanged_for_every_fixture`. The `folders` array is
   regenerable from Herdr, but the user's `settings`/`launch`/`tasks` and their comments
   are **not**; that test is what guards them against a mis-located splice span. Also kept:
   `TestShim` (the design's load-bearing `PATH`-independence properties), the
   resolved-path regression tests, and `TestReduceSnapshot`, which encodes the
   discovery findings (`.result.snapshot` level, the pane join, `active_tab_id` winning
   over lowest `pane_id`, `worktree.checkout_path` never used, duplicate labels).

Result: 1,650 → 1,178 source lines, 1,508 → 1,130 test lines, 119 → 106 tests, all
passing on `/usr/bin/python3` 3.9.6. Behaviour is otherwise unchanged.

## Discovery corrections

Discovery ran on 2026-08-26 against **Herdr 0.8.0 / protocol 19** and **VS Code 1.134.0**
on macOS arm64. Full evidence in
[`docs/herdr-vscode-sync-facts.md`](../../docs/herdr-vscode-sync-facts.md). Assumptions that
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
   resolved. `PATH`-independence is still required, but its justification changes from
   *"the server's `PATH` is minimal"* to **"the server's `PATH` is whatever launched it,
   therefore unknowable"** — and that is satisfied by naming the interpreter by absolute
   path, which is what allowed the design to drop the compile step entirely (see
   Runtime). The `env -i` offline test remains exactly right.

9. **Relative-path spawn confirmed.** `command = ["./bin/hello"]` ran with cwd set to the
   plugin root, for both actions and event hooks — which is what lets the manifest invoke
   `./bin/sync`. `$HERDR_BIN_PATH` is provided (`/Users/bingles/.local/bin/herdr`).
   (Discovery also recorded Deno 2.9.5 on the host, relevant only to the since-rejected
   `deno compile` design; the plugin no longer needs any toolchain.)

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

14. **VS Code reformats the file when *it* writes — but keeps comments.** Confirmed via
    the in-editor "Add Folder to Workspace": comments survive (including nested inside
    `settings`), trailing commas are stripped, every folder object is expanded to
    one-property-per-line, and the newly added path is written **relative** to the
    workspace file's directory. (`code --add` from the CLI is a *different* path that does
    re-serialise and delete comments; an earlier draft of this plan generalised from it
    incorrectly.) VS Code does *not* reformat in response to the plugin's own writes.
    Two consequences: **reading must resolve relative paths** against
    `dirname(workspaceFile)`, or the unchanged-content check rewrites the file on every run
    after any UI edit; and since the editor preserves comments, the plugin's
    comment-preserving splice is a requirement to avoid being *worse* than the editor.

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
  does not exist and cannot be installed, but `/usr/bin/python3` is **3.12.3**.
  Everything in **Validation → Offline** must pass there. Everything in
  **Validation → Host** requires a macOS/Linux host with Herdr and VS Code and is run by
  the user.
- **There is no build step and no compiled artifact.** The plugin is Python source,
  stdlib only, run in place. What the devcontainer validates is byte-for-byte what the
  host runs.
- **Target Python 3.9.** That is what stock macOS ships (`/usr/bin/python3` measured at
  **3.9.6** on the host); the devcontainer's 3.12.3 is more permissive and must not be
  allowed to mask a 3.10+ feature. Concretely: no `match` statements, no PEP 604
  `X | Y` annotations evaluated at runtime, and **no `tomllib`** (3.11+) — which is one
  reason config stays JSON.

## Design

### Runtime: Python 3.9+, stdlib only, no build

**Python 3, standard library only, run in place. No build step, no compiled artifact, no
third-party packages, no lockfile.** This is a deliberate choice for a *shared* plugin:
`herdr plugin install <owner>/<repo>` fetches and links, and the plugin works. There is no
`[[build]]` block, no release attachments, no per-platform artifacts, and nothing for an
installing user to have on their machine beyond a Python 3 that macOS and every mainstream
Linux already ship.

Everything the plugin needs is stdlib: `json`, `re`, `subprocess`, `os`, `tempfile`,
`shutil`, `fcntl`, `errno`, `argparse`. Verified importable on stock macOS
`/usr/bin/python3` (3.9.6).

#### The `PATH` problem, and why an absolute interpreter path solves it

Plugin commands are spawned by the Herdr **server**, and **the server's `PATH` is whatever
launched it — therefore unknowable.** Discovery measured a server started from a VS Code
integrated terminal: it had inherited a full interactive `PATH` with `deno`, `node`, and
`git` all resolvable. A server started from launchd, systemd, or a login item carries
something else entirely. So `command = ["python3", …]` is not safe, and neither is a
`#!/usr/bin/env python3` shebang — `env` resolves through `PATH` too.

The fix is to name the interpreter by **absolute path**, which macOS guarantees at
`/usr/bin/python3`. A tiny POSIX-sh shim does the selection:

```sh
#!/bin/sh
# bin/sync — locate a Python 3 without trusting PATH, then exec the real entrypoint.
for py in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    [ -x "$py" ] && exec "$py" "$(dirname "$0")/../src/main.py" "$@"
done
py=$(command -v python3 2>/dev/null) && exec "$py" "$(dirname "$0")/../src/main.py" "$@"
echo "herdr-vscode-sync: no python3 found. Install Xcode Command Line Tools" >&2
echo "  (xcode-select --install) or Homebrew Python (brew install python)." >&2
exit 127
```

The manifest then invokes `["./bin/sync", …]`. Discovery confirmed plugin-root-relative
commands spawn with the plugin root as cwd, for both actions and event hooks, so this
resolves. `$(dirname "$0")` is used rather than relying on cwd, so the shim also works when
invoked by absolute path.

This is **not** the "POSIX-sh entrypoint that locates an interpreter" listed under Rejected
alternatives in earlier drafts. That objection was about shelling out to find a toolchain
that may not exist anywhere on the machine. Here the candidate list is three fixed paths
and the interpreter is preinstalled — the shim removes a `PATH` dependency rather than
adding one. Both measured environments are covered: the host's `/usr/bin/python3` (3.9.6)
and the devcontainer's `/usr/bin/python3` (3.12.3). Note the devcontainer's *`PATH`*
python3 is `/usr/local/python/current/bin/python3`, which the list does not name and does
not need to.

#### Development loop

Edit and re-invoke — there is nothing to rebuild, which also means a user can patch an
installed plugin in place to test a fix. `herdr plugin link ./vscode-workspace-sync` picks
up source changes on the next hook invocation.

**`bin/sync` must be committed executable** (`chmod +x`; `git update-index --chmod=+x` if
the mode is wrong), or every hook fails with a spawn error visible only in `herdr plugin
log list`. The `.py` files do **not** need the executable bit — they are passed to the
interpreter as arguments, never exec'd directly.

### Architecture

One entrypoint, `src/main.py` (reached via the `bin/sync` shim), invoked three ways
by the manifest:

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

`workspace.focused` fires on every Space switch and spawns the interpreter each time,
which exits without writing. Python start-up is slower than a compiled binary (tens of ms
rather than a few), and this is the one place that cost is paid repeatedly — keep
`src/main.py` free of expensive imports so the no-op path stays cheap. That is the
accepted cost of a static manifest.

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

`src/jsonc.py` exports:

- `findTopLevelMember(text, key)` → `{ keyStart, valueStart, valueEnd }` or `null`.
  Walks the text tracking string state (with `\` escapes), `//` line comments,
  `/* */` block comments, and brace/bracket depth, so a `]` inside a string or comment
  cannot terminate the array. Only depth-1 members of the root object match.
- `strip_comments(text)` → the same tokenizer, replacing comment spans with spaces, used
  to parse the plugin's own config file so it may contain comments.

Both are pure functions over `str`, with no dependency on the rest of the plugin, which is
what makes them cheap to unit-test. `json.loads` cannot substitute for either: it rejects
comments and trailing commas outright, and re-serialising with `json.dumps` would destroy
the user's formatting — the same objection that ruled out parse-and-reserialise generally.

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
- Paths and names are emitted with `json.dumps` so escaping is correct.
- If the file has no top-level `folders` member, insert `"folders": [...],` as the
  **first** member of the root object, at the root's inner indentation.
- If the target file does not exist, log the resolved path and exit non-zero. Do not
  create it — a typo in config must not silently produce a stray workspace file.

Writing:

1. `os.path.realpath` the target first, so a symlinked workspace file is replaced through
   the link rather than having the link clobbered.
2. Before the first write of a session, copy the original to
   `$HERDR_PLUGIN_STATE_DIR/backup/<basename>.<iso-timestamp>`; keep the newest 10.
3. Write via `tempfile.mkstemp(dir=<same directory>)`, `os.chmod` it to the original's
   `stat().st_mode`, `os.fsync` before closing, then `os.replace` over the target —
   atomic, and VS Code's watcher sees one event. `os.replace` (not `shutil.move`) is what
   guarantees the rename is atomic on the same filesystem.
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
file at `$HERDR_PLUGIN_STATE_DIR/sync.lock`, created via
`os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)` holding `{pid, startedAt}`:

- Poll for the lock for up to 5000 ms (50 ms interval).
- Break a lock whose `startedAt` is older than 30 000 ms, logging that it did so.
- On timeout: log and exit **0**. The last event of a burst will still get the lock, and
  every holder re-reads Herdr state after acquiring it, so a late run is always fresh.
- Release in a `finally`, and install `signal.signal` handlers for `SIGTERM`/`SIGINT`
  that release and re-raise.

An exclusive-create lockfile is preferred over `fcntl.flock` here: it survives being
inspected by a human, carries the holding pid for debugging, and its staleness rule is
explicit rather than depending on process-exit semantics.

`debounceMs` (default `0`) sleeps after acquiring the lock and before reading state, for
users who want to coalesce heavy bursts.

### Configuration

`$HERDR_PLUGIN_CONFIG_DIR/config.json`, parsed with `json.loads(strip_comments(text))`, so
comments are allowed. `config.example.json` in the plugin root documents it.

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

# No [[build]] block. The plugin is Python source run in place; `herdr plugin install`
# fetches and links with nothing to compile.

[[startup]]
command = ["./bin/sync", "--reason", "startup"]

[[events]]
on = "workspace.created"
command = ["./bin/sync", "--reason", "event"]
# ... one block per event in the table above

[[actions]]
id = "sync"
title = "Sync VS Code workspace"
contexts = ["global", "workspace"]
command = ["./bin/sync", "--reason", "action"]

[[actions]]
id = "doctor"
title = "VS Code sync diagnostics"
contexts = ["global"]
command = ["./bin/sync", "--doctor"]
```

**No dependencies at all**, runtime or test. The JSONC tokenizer is hand-written (see
Rejected alternatives) and everything else is Python stdlib; tests use `unittest`, also
stdlib. Nothing to pin, nothing to lock, no network needed at install time.

Discovery confirmed 0.8.0 both **accepts** a `[[build]]` block and treats it as
**optional** — a manifest without one links and runs normally. Declaring `platforms`
also suppresses the `manifest does not declare platforms` warning that discovery hit.

Windows is excluded from `platforms`: path rendering and the Herdr named-pipe transport
are untested there.

## Gotchas

- **`command` is argv, not a shell line.** No expansion, no `~`, no `$VAR`.
- **The server's `PATH` is whatever launched the server.** It may be a full interactive
  `PATH` (measured: a server started from a VS Code integrated terminal had `deno`,
  `node`, and `git` all resolvable) or nearly empty under launchd. Because it is
  unknowable, the interpreter is selected by **absolute path** in `bin/sync` and Herdr is
  reached through `$HERDR_BIN_PATH`. Do not add a hook that shells out to a tool assuming
  it is on `PATH`, and do not be reassured by it working on your own machine.
- **`bin/sync` and `src/main.py` must be committed executable.** A missing `+x` bit is the
  most likely first failure and shows up only as a spawn error in `herdr plugin log list`.
- **Do not add a `#!/usr/bin/env python3` shebang and invoke the `.py` directly.** `env`
  resolves through `PATH`, which is exactly the thing that cannot be trusted. The shim
  exists for this reason.
- **Target Python 3.9, not the version you happen to be running.** The devcontainer's
  3.12 will silently accept `match`, PEP 604 unions, and `tomllib`; stock macOS 3.9.6
  will not. There is no type checker or linter in the loop to catch this — only the 3.9
  floor stated in the README.
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
- **VS Code reformats the workspace file when the user changes folders through the UI, but
  it does NOT delete comments.** Measured via the in-editor "Add Folder to Workspace":
  comments survive (including inside `settings`), trailing commas are stripped, folder
  objects are expanded to one property per line, and the added path is written **relative**
  to the workspace file's directory. The `code --add` CLI takes a different code path that
  *does* re-serialise and drop comments — do not generalise from it. Since the editor
  itself preserves comments, a plugin that destroyed them would be worse than the editor;
  the byte-preserving splice is the right call. VS Code does **not** reformat the file in
  response to the plugin's own writes.
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
- **Installing from GitHub requires only a Python 3.** There is no `[[build]]` step, so
  nothing can fail at install time for want of a toolchain. The residual risk is narrow:
  on macOS `/usr/bin/python3` is a Command Line Tools shim, so a machine without CLT
  *and* without Homebrew Python gets exit 127 and the shim's install instructions. In
  practice a Herdr user has one or the other. Stated as a one-line prerequisite in the
  README rather than a build dependency.
- **`herdr plugin install` from GitHub is untested.** Discovery verified that `[[build]]`
  is optional and that a build-less manifest links and runs via `plugin link`, but never
  exercised `plugin install` against a published repo. First host validation step should
  be an install from the real repo, not just a link.
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
- **Any third-party JSONC library for the edit.** Adding a dependency would reintroduce
  an install step — the entire thing this design avoids. Independently, VS Code's own
  `jsonc-parser` formats inserted objects one key per line, so entries would come out as
  four lines each rather than the compact `{ "path": … }` form in
  `docs/example-vscode-workspace.md`. This is a file the user reads; exact rendering
  control is worth ~100 lines of tokenizer.
- **`json.loads` / `json.dumps` for the whole file.** Rejects comments and trailing commas,
  and re-serialising destroys the user's formatting.
- **TypeScript on Deno, compiled with `deno compile`.** This was the previous design. It
  is correct and `PATH`-independent, but it costs a `[[build]]` step, which means every
  installing user needs Deno 2.x, or the project needs per-platform binaries published as
  release attachments with checksums. For a shared plugin that overhead is the dominant
  cost, and an absolute interpreter path achieves the same `PATH` independence for free.
  Reconsider only if the plugin ever needs something outside the Python stdlib.
- **`deno run` or `node` straight from the manifest.** Both need a toolchain the user must
  install, and neither has a reliable absolute path — Node especially, given
  nvm/fnm/volta/Homebrew all install elsewhere. macOS ships neither.
- **Pure POSIX sh with `jq`.** `jq` is not preinstalled on macOS, so it is a real
  dependency; and byte-preserving JSONC splicing in sh is far more fragile than the
  tokenizer it would replace.
- **Parsing and re-serializing the whole workspace file.** Destroys the user's comments
  and formatting.
- **A `pathMap` prefix-rewrite config for remote setups.** Not needed for
  Herdr-in-the-integrated-terminal; noted under Future work.

## Future work

Out of scope, listed so it is not re-litigated: syncing the focused Space's colour into
`settings.workbench.colorCustomizations`; a `pathMap` for remote/container path
translation; Windows support; syncing Herdr tabs to VS Code editor groups.

## Checklist

- [x] `vscode-workspace-sync/herdr-plugin.toml` — manifest exactly as specified above:
      no `[[build]]` block, `platforms` declared, all seven `[[events]]` blocks, and every
      `command` invoking `./bin/sync`
- [x] `vscode-workspace-sync/bin/sync` — POSIX-sh interpreter shim exactly as specified
      (absolute-path candidates, `PATH` fallback, exit 127 with install instructions),
      committed **executable** (the `.py` files must not be, and need no shebang)
- [x] `vscode-workspace-sync/.gitignore` — `__pycache__/`, `*.pyc`
- [x] `vscode-workspace-sync/src/types.py` — Herdr JSON shapes as dataclasses or
      TypedDicts, transcribed from `docs/herdr-vscode-sync-facts.md` probes 2–5 (note the
      `.result.snapshot` level, the absence of `cwd` on workspace records, and the pane
      join), with a comment naming Herdr 0.8.0 / protocol 19 as the version observed.
      **Delivered as `src/herdr_types.py`, not `src/types.py`.** `src` is `sys.path[0]`
      when `bin/sync` hands `src/main.py` to the interpreter, so a `types.py` there
      shadows the stdlib `types` module and the first stdlib import
      (`json` → `re` → `enum` → `from types import MappingProxyType`) dies with a
      partially-initialised-module `ImportError`. Reproduced on `/usr/bin/python3` 3.9.6;
      the filename was the only thing that could be changed to fix it.
- [x] `vscode-workspace-sync/src/jsonc.py` — `find_top_level_member`, `strip_comments`
- [x] `vscode-workspace-sync/src/config.py` — load, defaults, `~` expansion, env
      override, unknown-key warnings, validation errors, and the `sessionSocket` guard
      (exit 0 without writing when `$HERDR_SOCKET_PATH` does not match)
- [x] `vscode-workspace-sync/src/herdr.py` — run `herdr api snapshot` via
      `subprocess.run([os.environ["HERDR_BIN_PATH"], "api", "snapshot"])` (or read the
      fake snapshot file) and reduce it to ordered `{id,label,path}` records plus
      `focused_workspace_id`
- [x] `vscode-workspace-sync/src/folders.py` — pure `compute_folders(spaces, focused_id,
      config)` implementing resolve → exists → exclude → dedupe → `name`
- [x] `vscode-workspace-sync/src/rewrite.py` — pure `render_folders(entries, base_indent)`
      and `splice_folders(text, entries)`, including the insert-when-absent path
- [x] `vscode-workspace-sync/src/write.py` — realpath, backup rotation (keep 10), mkstemp
      + mode preservation + `os.fsync` + `os.replace`, unchanged-content skip comparing
      **resolved** paths
- [x] `vscode-workspace-sync/src/lock.py` — `O_CREAT|O_EXCL` create, 5 s wait, 30 s stale
      break, release in `finally` and on `SIGTERM`/`SIGINT`
- [x] `vscode-workspace-sync/src/main.py` — argv via `argparse` (`--reason`, `--doctor`),
      orchestration, one-line stdout summary, exit codes
- [x] `vscode-workspace-sync/config.example.json` — every key with its default, commented
- [x] `vscode-workspace-sync/README.md` — Python 3.9+ as the only prerequisite, install via
      `herdr plugin install`, `herdr plugin config-dir`, config table, both modes, the
      pin-`folders[0]` recommendation with its measured cost, the "manage folders in Herdr,
      not the VS Code UI" warning, and the recorded Herdr JSON shapes
- [x] `vscode-workspace-sync/test/fixtures/` — at minimum: the file from
      `docs/example-vscode-workspace.md`; a file with no `folders` member; a file with a
      `]` inside a string value and inside a comment; a file with block comments between
      members; a file with a non-default indent; **a file with relative `folders` paths and
      one-property-per-line objects, as VS Code itself writes them** (see facts doc probe
      14); `snapshot.json` matching the real `api snapshot` shape from discovery, including
      the `.result.snapshot` level, a `panes` array, and a worktree-backed Space
- [x] `vscode-workspace-sync/test/test_jsonc.py`
- [x] `vscode-workspace-sync/test/test_folders.py`
- [x] `vscode-workspace-sync/test/test_rewrite.py` — asserts non-`folders` bytes are
      unchanged
- [x] Root `README.md` — add a plugin index entry pointing at the new directory
- [x] `docs/herdr-research-notes.md` — already updated by discovery; extend only if
      implementation turns up something new. **Left unchanged:** implementation ran
      entirely offline against recorded fixtures and turned up nothing new about Herdr.
      The one new finding (the `types.py` stdlib shadow) is a Python packaging fact, not
      a Herdr one, and is recorded in the plugin README and in `src/herdr_types.py`.

## Validation

### Offline — runnable in this devcontainer

- [x] `cd vscode-workspace-sync && /usr/bin/python3 -m unittest discover -s test -v` — **147 tests, OK**
- [x] `/usr/bin/python3 -m py_compile src/*.py` is clean — the cheapest available syntax gate,
      since there is no type checker in the loop
- [x] **Python 3.9 floor:** grepped — no `match`/`case`, no PEP 604 unions, no
      `tomllib` import (the only hit is a comment in `config.py` explaining its absence).
      Original item: the sources contain no `match` statement, no PEP 604 `X | Y`
      annotation evaluated at runtime, and no `tomllib` import. Grep for them; the
      devcontainer's 3.12 will not catch these
- [x] `./bin/sync --doctor` runs (exit 0), and `bin/sync` is mode `755`
- [x] **PATH independence:** `env -i HOME=… HERDR_PLUGIN_CONFIG_DIR=… 
      HERDR_VSCODE_SYNC_FAKE_SNAPSHOT=… ./bin/sync --doctor` exits 0 with no `PATH`.
      Original item: `env -i HOME=$HOME HERDR_PLUGIN_CONFIG_DIR=…
      HERDR_VSCODE_SYNC_FAKE_SNAPSHOT=… ./bin/sync --doctor` succeeds with **no `PATH` at
      all** — this is the property the shim exists to provide, so test it rather than
      assume it
- [x] **Shim fallback:** a copy of the shim with the candidate list pointed at
      nonexistent paths, run under `env -i HOME=… PATH=`, exits **127** with the
      install message. (`PATH=` must be set-but-empty: an *unset* `PATH` makes
      `command -v` fall back to the shell's built-in default, which finds
      `/usr/bin/python3`.) Original item: with `/usr/bin/python3` temporarily masked (run the shim under a
      `PATH`-less env and a candidate list pointed at a nonexistent path, or test the
      selection logic directly), the shim exits 127 with the install message rather than
      failing obscurely
- [x] Round-trip: for each fixture, splicing back its own existing folders produces a
      byte-identical file — **holds for every fixture whose `folders` array is already in
      the plugin's canonical rendering** (`canonical`, `brackets`, `block-comments`).
      For `example` (trailing comma), `four-space-indent` / `tab-indent` (non-`base+2`
      entry indent) and `vscode-written` (one-property-per-line) it cannot hold: the plan
      mandates canonical re-rendering of the array in those exact cases. What was verified
      instead, for every fixture: **every byte outside the `folders` value is identical**,
      the splice is **idempotent**, and the parsed `folders` value is unchanged. In
      practice none of those files is ever rewritten, because the resolved-path
      unchanged-check reports `unchanged` first
- [x] A fixture whose `folders` hold **relative** paths (as VS Code emits them) resolves to
      the same set as the absolute equivalent, and reports `unchanged` rather than
      rewriting
- [x] Comment and trailing-comma preservation: splicing new folders into the
      `docs/example-vscode-workspace.md` fixture leaves the `settings` block, its
      comments, and its trailing commas untouched
- [x] `]` inside a string value, a `//` comment and a `/* */` comment do not terminate
      the array
- [x] Insert path: a fixture with no `folders` member gains one as the first root member
      and remains parseable by `JSON.parse(stripComments(text))`
- [x] Empty computed list writes nothing and exits 0 with `skipped-empty`
- [x] Missing target file exits non-zero and names the resolved path
- [x] Missing `workspaceFile` config exits non-zero and names the config path (as does a
      missing config file)
- [x] `HERDR_VSCODE_SYNC_FAKE_SNAPSHOT=test/fixtures/snapshot.json
      HERDR_PLUGIN_CONFIG_DIR=… ./bin/sync` rewrites a scratch copy of a fixture
      to the expected `folders`, and a second run reports `unchanged`
- [x] Same command with `--doctor` writes nothing and prints the computed folder list
- [x] `mode: "active"` yields exactly the focused Space plus any pinned folders
- [x] Two concurrent `./bin/sync` runs against the same scratch file both exit 0 and
      leave valid JSONC (a `python3 -c 'json.loads(strip_comments(...))'` parse succeeds)

### Host — requires Herdr + VS Code, run by the user

- [x] **Go/no-go** — answered by discovery probe 13: **GO**, no window reload and
      terminals survive; mode `active` is viable with a pinned `folders[0]`. Recorded in
      `docs/herdr-vscode-sync-facts.md`
- [ ] `herdr plugin link ./vscode-workspace-sync` succeeds and
      `herdr plugin list` shows it enabled, with no `platforms` warning
- [ ] **Install path:** after publishing, `herdr plugin install <owner>/<repo>` succeeds on
      a machine that has never had Deno or Node — proving the no-build design end to end.
      This was never exercised during discovery
- [ ] `cd <plugin_root> && ./bin/sync --doctor` prints the resolved config, the Space
      list with paths, and the computed folders, exit 0. (There is deliberately no
      `doctor` plugin action — action stdout only reaches `herdr plugin log list`,
      JSON-escaped.)
- [ ] `herdr plugin action invoke sync --plugin vscode-workspace-sync` rewrites the file and the
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
- `vscode-workspace-sync/.gitignore`
- `vscode-workspace-sync/config.example.json`
- `vscode-workspace-sync/README.md`
- `vscode-workspace-sync/bin/sync` (POSIX-sh interpreter shim, committed executable)
- `vscode-workspace-sync/src/main.py`
- `vscode-workspace-sync/src/types.py`
- `vscode-workspace-sync/src/jsonc.py`
- `vscode-workspace-sync/src/config.py`
- `vscode-workspace-sync/src/herdr.py`
- `vscode-workspace-sync/src/folders.py`
- `vscode-workspace-sync/src/rewrite.py`
- `vscode-workspace-sync/src/write.py`
- `vscode-workspace-sync/src/lock.py`
- `vscode-workspace-sync/test/test_jsonc.py`
- `vscode-workspace-sync/test/test_folders.py`
- `vscode-workspace-sync/test/test_rewrite.py`
- `vscode-workspace-sync/test/fixtures/*.code-workspace`
- `vscode-workspace-sync/test/fixtures/snapshot.json`
- `README.md` (repo root — does not exist yet; create with a plugin index)

No `deno.json`, `deno.lock`, or `bin/` build output: there is no build step and no
dependency manifest of any kind.

Modified:

- `docs/herdr-research-notes.md` — append observed `api snapshot` / `workspace list`
  JSON shapes and which plugin event hooks fired

Read but not modified:

- `docs/example-vscode-workspace.md` — source of the primary test fixture
- `.claude/skills/herdr-plugin-authoring/SKILL.md` — manifest, env, and CLI reference
