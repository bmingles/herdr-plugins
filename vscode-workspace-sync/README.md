# vscode-workspace-sync

A [Herdr](https://herdr.dev) plugin that keeps the `folders` array of a VS Code
multi-root `.code-workspace` file in sync with your Herdr Spaces.

Open the workspace file in VS Code, run `herdr` in its integrated terminal, and navigate
Herdr normally. Creating, closing, renaming or reordering a Space rewrites the workspace
file, and VS Code picks up the new root folders **without a window reload** — every
editor, unsaved buffer and integrated terminal survives, including the one running Herdr.

> Herdr calls them Spaces; VS Code calls its own document a workspace. This README says
> **Space** for the Herdr concept and **workspace file** for the `.code-workspace`
> document.

## Prerequisites

**A Python 3.9 or newer**, and nothing else. No build step, no compiled artifact, no
third-party packages, no lockfile — the plugin is standard-library Python run in place.
macOS ships `/usr/bin/python3` with the Xcode Command Line Tools
(`xcode-select --install`); every mainstream Linux ships one too.

Requires Herdr **0.8.0** or newer. macOS and Linux; Windows is untested and not declared.

## Install

```sh
herdr plugin install <owner>/herdr-plugins/vscode-workspace-sync
```

Or, to develop against a working tree:

```sh
herdr plugin link ./vscode-workspace-sync
herdr plugin list
```

Then write the config file and run the sync once by hand — startup hooks run when a
Herdr **server** starts, not on `plugin link`:

```sh
herdr plugin config-dir vscode-workspace-sync    # prints the config directory
# copy config.example.json there as config.json and edit `workspaceFile`
herdr plugin action invoke sync --plugin vscode-workspace-sync
herdr plugin log list --plugin vscode-workspace-sync --limit 5
```

`herdr plugin log list` is the **only** place hook output is visible. Check it after any
trigger rather than guessing.

## Configuration

`config.json` in the directory printed by
`herdr plugin config-dir vscode-workspace-sync`. Comments and trailing commas are
allowed. `config.example.json` in this directory documents every key with its default.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `workspaceFile` | string | — | **Required.** Absolute path to the `.code-workspace` file. `~` is expanded. |
| `mode` | `"mirror"` \| `"active"` | `"mirror"` | Folder computation mode. |
| `pinnedFolders` | string[] | `[]` | Absolute paths always emitted first. |

Two environment variables are part of the supported contract:

| Variable | Effect |
| --- | --- |
| `HERDR_VSCODE_SYNC_WORKSPACE_FILE` | Overrides `workspaceFile` for one run. |
| `HERDR_VSCODE_SYNC_FAKE_SNAPSHOT` | Read Herdr state from this JSON file instead of running `herdr api snapshot`. Honoured by `--doctor` too. |

An unknown config key is a warning, not an error. A missing config file, a missing
`workspaceFile`, or a `workspaceFile` that does not exist is a hard failure — the plugin
will **not** create a workspace file, because a typo in config must not leave a stray one
behind.

## Modes

- **`mirror`** (default) — `folders` mirrors the full ordered Space list, in Herdr
  sidebar order.
- **`active`** — `folders` holds only the focused Space, so the VS Code explorer follows
  whichever Space you are in.

`folders` is always `pinnedFolders` first, in configured order, then the Spaces.

## Pin at least one folder

**Strongly recommended, especially for `mode: "active"` — but never enforced.** An
unpinned config is fully supported and correct in both modes; the only cost is
performance.

Measured on VS Code 1.134.0: the window's extension host restarts **if and only if the
path at `folders[0]` changes** — by replacement, by a reorder that swaps a different
folder into first place, by removing the first folder, or by inserting one ahead of it. A
pure reorder of indices 1↔2 costs nothing. On a restart every language server and the Git
extension reactivate and re-index: seconds of visible churn on large repos. It is **not**
a window reload — no editors, no unsaved buffers and no terminals are lost.

In `mirror` mode that happens only when the *first* Space changes. In `active` mode the
single folder *is* `folders[0]`, so it would happen on **every Space switch**. Give
`pinnedFolders` one stable entry and `active` mode only ever rewrites index 1 and above,
which is free. It also keeps `${workspaceFolder}` stable.

`--doctor` warns when `mode` is `"active"` and `pinnedFolders` is empty, then proceeds.

## Manage folders in Herdr, not the VS Code UI

When the **plugin** writes the file, VS Code adopts the change and leaves the file alone —
comments, trailing commas and formatting all survive (measured across seven folder
mutations).

When **VS Code** writes the file — "Add Folder to Workspace", or `code --add` — it
rewrites the whole document: **comments are deleted**, trailing commas are stripped, every
folder object is expanded to one property per line, and the newly added path is written
*relative* to the workspace file's directory. That damage is not confined to `folders`.

The plugin tolerates the aftermath (it resolves relative paths before deciding whether
anything changed, so it will not fight the editor over path form), but your comments are
already gone by then. Add and remove roots through Herdr.

## How it works

One entrypoint, `src/main.py`, reached through the `bin/sync` POSIX-sh shim, invoked three
ways by `herdr-plugin.toml`:

- `[[startup]]` — once when a Herdr server starts or takes over a live handoff, so the
  workspace file matches the restored session.
- `[[events]]` — one hook each for `workspace.created`, `closed`, `renamed`, `moved`,
  `reordered`, `updated` and `focused`.
- `[[actions]]` — `sync`, for a manual resync. (Diagnostics are the `--doctor`
  *flag*, run directly — see Diagnostics.)

Every invocation does the same three things: read the authoritative Herdr state, compute
the desired `folders`, and rewrite the file only if the result differs. **The event
payload is never read to decide what changed** — state is recomputed from scratch. That
makes runs idempotent, makes the redundant invocations Herdr emits free (a reorder
re-emits `workspace.focused`; `herdr worktree create` emits `workspace.updated` +
`workspace.created`), and means a missed or unknown event cannot leave stale state.

`folders` is computed as: resolve every path (no symlink resolution — `realpath` would
rewrite `/tmp` to `/private/tmp` on macOS) → drop paths that are not existing directories
deduplicate by resolved path, first occurrence wins.

If the computed list comes out **empty**, the plugin logs a warning and writes nothing:
`"folders": []` would blank the VS Code explorer, and an empty result far more likely
means Herdr returned something unexpected.

### Writing

The workspace file is JSONC and it is yours. Rather than parsing and re-serialising it, a
small hand-written tokenizer (`src/jsonc.py`) locates the top-level `"folders"` member's
value span and only that span is replaced. Everything else — comments, trailing commas,
key order, indentation — survives byte for byte. The `folders` array itself is rendered
canonically, one entry per line at the `"folders"` line's indentation plus two spaces, and
with **no** trailing comma:

```jsonc
"folders": [
  { "path": "/abs/one" },
  { "path": "/abs/two", "name": "api" }
]
```

A `name` appears only when the Space's label differs from its
directory basename. Herdr auto-derives labels from the basename, so in practice `name`
shows up only for Spaces you labelled yourself and for worktree Spaces named after a
branch. Pinned folders never get a `name`.

The write itself: `realpath` the target (so a symlinked workspace file is replaced
*through* the link), then `fsync`, and `os.replace` over the target — atomic, so VS Code's watcher sees one event.

No lock file. A burst of events can race, but each run recomputes from scratch and
replaces the file with a single atomic `os.replace`, so the loser of a race is
corrected by the next event — and the `folders` array is regenerable from Herdr by
definition.

### Why the shim, and why absolute interpreter paths

Plugin commands are spawned by the Herdr **server**, whose `PATH` is whatever launched it
and therefore unknowable — a server started from a VS Code integrated terminal inherits a
full interactive `PATH`; one started from launchd carries almost nothing. So
`command = ["python3", …]` is unsafe, and so is a `#!/usr/bin/env python3` shebang, since
`env` resolves through `PATH` too.

`bin/sync` tries `/usr/bin/python3`, `/opt/homebrew/bin/python3` and
`/usr/local/bin/python3` in order, falls back to `command -v python3`, and otherwise exits
127 with install instructions. `bin/sync` must stay **executable**; the `.py` files must
**not** be, and carry no shebang — they are passed to the interpreter as arguments.

Herdr itself is reached through `$HERDR_BIN_PATH` (falling back to `herdr` on `PATH`)
rather than the raw socket, per the plugin docs' portability guidance.

## Logging and diagnostics

Every run prints one line to stdout:

```
vscode-workspace-sync: reason=event mode=mirror target=/path/x.code-workspace folders=3 result=wrote
```

`result` is one of `wrote`, `unchanged`, `skipped-empty`, `skipped-session`,
or `doctor`. Failures print to stderr and exit non-zero.

### Diagnostics

`--doctor` prints the resolved config, the resolved target path and whether it exists, the
Herdr socket and session-guard decision, the snapshot summary with every Space and its
path, the computed folder list, and whether a real run would write — then exits **without
writing**. It is the way to answer "the plugin did nothing and I do not know why", since
every quiet outcome (`skipped-empty`, `skipped-session`, a config file read from somewhere
you did not expect) looks identical from outside.

Run the script **directly**, so the output lands in your terminal:

```sh
cd /path/to/vscode-workspace-sync && ./bin/sync --doctor
```

`herdr plugin list` prints each plugin's `plugin_root` if you do not know the path. To skip
the lookup (uses `python3`, which this plugin already requires — not `jq`, which macOS does
not ship):

```sh
cd "$(herdr plugin list --json | python3 -c 'import json,sys; print(next(p["plugin_root"] for p in json.load(sys.stdin)["result"]["plugins"] if p["plugin_id"]=="vscode-workspace-sync"))')" && ./bin/sync --doctor
```

There is deliberately **no `doctor` plugin action**. A plugin action's stdout goes only to
`herdr plugin log list`, JSON-escaped with `\n` sequences — the least legible channel
available, and a poor route for a diagnostic. The `sync` action stays, because you invoke
that one for its effect and the one-line summary in the log is enough to confirm it worked.

## One Herdr session at a time

Plugin registration lives in `~/.config/herdr/plugins.json`, which is **not**
session-scoped: a single linked plugin runs in every session's server. Two Herdr sessions
would each compute `folders` from their own Space list and overwrite the other's.

The guard: by default the plugin only syncs for the **default** session (a
`$HERDR_SOCKET_PATH` outside `.../sessions/<name>/`); other sessions log
`skipped-session` and exit 0.
socket instead. `--doctor` reports the socket it saw.

## Herdr JSON shapes

Recorded so the plugin documents its own contract. Observed on **Herdr 0.8.0 / protocol
19**; full evidence in [`docs/herdr-vscode-sync-facts.md`](../docs/herdr-vscode-sync-facts.md).

### `herdr api snapshot`

Note the **three** envelope levels — `.result.snapshot.workspaces`, not
`.result.workspaces`:

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

Array order **is** sidebar order (verified against a reorder); `number` is a redundant
1-based sidebar position.

**Workspace records carry no `cwd`.** Each Space is reduced to `{ id, label, path }`,
where `path` comes from `panes[]` joined on `workspace_id` — the pane whose `tab_id`
matches the Space's `active_tab_id`, else the lowest `pane_id`. `foreground_cwd` is
ignored. `worktree.checkout_path` appears on some records but attaches **lazily** and is
never used.

A worktree-backed Space adds:

```json
"worktree": {
  "checkout_path": "/Users/bingles/.herdr/worktrees/herdr-plugins/probe-x",
  "is_linked_worktree": true,
  "repo_key": "/Users/bingles/code/tools/herdr-plugins/.git",
  "repo_name": "herdr-plugins",
  "repo_root": "/Users/bingles/code/tools/herdr-plugins"
}
```

### `HERDR_PLUGIN_CONTEXT_JSON`

```json
{"workspace_id":"w6","workspace_label":"demo",
 "workspace_cwd":"/Users/bingles/code/tools/herdr-plugins",
 "tab_id":"w6:t1","tab_label":"1","focused_pane_id":"w6:p1",
 "focused_pane_cwd":"/Users/bingles/code/tools/herdr-plugins",
 "focused_pane_status":"unknown",
 "invocation_source":"api","correlation_id":"workspace.created"}
```

`workspace_id` is the **subject of the event**, not the focused Space — only on
`workspace.focused` are the two the same. `workspace_cwd` is the *stable* Space root (pane
`cwd` drifts when you `cd`), so the plugin prefers it for the Space named in a hook and
falls back to the pane join for all the others. It is **absent on `workspace.closed`**,
because the Space is gone. Fields are omitted, not nulled, when unavailable.

### `HERDR_PLUGIN_EVENT_JSON`

Always `{"event": "<underscored>", "data": {"type": "<underscored>", …}}`. The plugin
never reads it: no payload carries `cwd`, and `renamed`/`focused` carry only ids, so
`api snapshot` is required anyway. On a **startup** invocation it is **unset entirely**
while `HERDR_PLUGIN_EVENT` is the literal string `startup` — code that parses it
unconditionally crashes the hook that matters most.

## Development

There is nothing to build. Edit and re-invoke; `herdr plugin link` picks up source
changes on the next hook invocation, and an installed plugin can be patched in place to
test a fix.

```sh
cd vscode-workspace-sync
/usr/bin/python3 -m unittest discover -s test -v     # 147 tests, stdlib unittest only
/usr/bin/python3 -m py_compile src/*.py              # the syntax gate
```

Run the tests with **`/usr/bin/python3`** specifically. The floor is **Python 3.9** —
stock macOS ships 3.9.6 — and a newer interpreter silently accepts `match` statements,
PEP 604 `X | Y` runtime annotations, and `tomllib`, none of which are allowed. There is no
type checker or linter in the loop to catch them.

No module in `src/` may share a name with a stdlib module: `src` is `sys.path[0]` when
`bin/sync` hands `src/main.py` to the interpreter, so `src/types.py` would shadow the
stdlib `types` module and the very first stdlib import would die. That is why the JSON
shapes are documented at the top of `src/herdr.py`.

`test/fixtures/snapshot.json` is a faithful transcription of a real `api snapshot` and its
paths are the ones discovery observed; `test/fixtures/snapshot-portable.json` has the same
shape with paths that exist on any POSIX machine, and is what the automated tests use.
