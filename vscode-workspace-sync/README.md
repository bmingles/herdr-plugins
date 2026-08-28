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
herdr plugin install bmingles/herdr-plugins/vscode-workspace-sync
```

Or, to develop against a working tree:

```sh
herdr plugin link ./vscode-workspace-sync
herdr plugin list
```

### Write the config

**The plugin does nothing until `config.json` exists** — it has no default target, by
design, so it can never guess at a file and rewrite the wrong one. `config-dir` prints a
bare path and works before the plugin is installed:

```sh
CFG="$(herdr plugin config-dir vscode-workspace-sync)"
mkdir -p "$CFG"
cat > "$CFG/config.json" <<'JSON'
{
  "workspaceFile": "~/path/to/your.code-workspace"
}
JSON
```

Point `workspaceFile` at a `.code-workspace` file **that already exists** — the plugin
will not create one. `config.example.json` in this directory documents the other two keys.

### First run

Check what it would do before it writes anything:

```sh
./bin/sync --doctor          # from this directory; prints Spaces, paths, computed folders
```

Then sync once by hand — startup hooks run when a Herdr **server** starts, not on
`plugin link`:

```sh
herdr plugin action invoke sync --plugin vscode-workspace-sync
herdr plugin log list --plugin vscode-workspace-sync --limit 5
```

`herdr plugin log list` is the **only** place hook output is visible. Check it after any
trigger rather than guessing — and see [Diagnostics](#diagnostics) when nothing seems to
happen.

## Configuration

`config.json` in the directory printed by
`herdr plugin config-dir vscode-workspace-sync`. Comments and trailing commas are
allowed. `config.example.json` in this directory documents every key with its default.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `workspaceFile` | string | — | Absolute path to the `.code-workspace` file. `~` is expanded. **Required unless `sessions` is set.** |
| `mode` | `"mirror"` \| `"active"` | `"mirror"` | Folder computation mode. |
| `pinnedFolders` | string[] | `[]` | Absolute paths always emitted first. |
| `sessions` | object | `{}` | One workspace file per Herdr session, keyed by session name — see [One workspace file per Herdr session](#one-workspace-file-per-herdr-session). |

Two environment variables are part of the supported contract:

| Variable | Effect |
| --- | --- |
| `HERDR_VSCODE_SYNC_WORKSPACE_FILE` | Overrides `workspaceFile` for one run. |
| `HERDR_VSCODE_SYNC_FAKE_SNAPSHOT` | Read Herdr state from this JSON file instead of running `herdr api snapshot`. Honoured by `--doctor` too. |

An unknown config key is a warning, not an error. A missing config file, a missing
`workspaceFile`, or a `workspaceFile` that does not exist is a hard failure — the plugin
will **not** create a workspace file, because a typo in config must not leave a stray one
behind. So is two sessions claiming the same `workspaceFile`; see below.

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

## Changing directory

A Space's directory is **derived from its active pane**, not stored — `cwd` appears only
in `workspace.create` across the entire socket API, and there is no `workspace.set_cwd`.
So `cd` is the only way a Space moves after it is created, and the folder in VS Code
follows it.

The catch: **`cd` fires no hookable event.** `pane.updated` is the event that carries the
new cwd, and it is delivered over the socket but never to plugin hooks — the manifest
accepts it and it silently never fires. Of Herdr 0.8.0's 27 subscription types, only 10
reach plugin hooks at all.

So the plugin hooks the five that do fire for ordinary shell panes — `pane.focused`,
`tab.focused`, `pane.created`, `tab.created`, `tab.closed`. After a `cd`, the folder
updates on your **next navigation**: switching pane or tab, opening a tab, splitting.
In normal use that is immediate enough that you rarely notice.

What is still not covered: `cd` and then staying in that pane, touching nothing else.
VS Code stays on the old directory until something happens. Force it with:

```sh
herdr plugin action invoke sync --plugin vscode-workspace-sync
```

`pane.agent_status_changed` fires ~0.85/s and would act as a near-realtime poll, but it
is deliberately **not** hooked: it only fires for panes with a detected agent — nothing at
all for a plain shell, which is the case this is meant to fix — and at ~90 ms per no-op
run it would cost roughly 8% of a core per active agent.

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

Two entrypoints, each behind its own POSIX-sh shim. `src/main.py` (via `bin/sync`) is
the outbound direction described here; `src/adopt.py` (via `bin/adopt`) is the inbound
one — see [The other direction](#the-other-direction-adopting-a-workspace-file).

`src/main.py` is invoked three ways by `herdr-plugin.toml`:

- `[[startup]]` — once when a Herdr server starts or takes over a live handoff, so the
  workspace file matches the restored session.
- `[[events]]` — twelve hooks: the seven `workspace.*` events (`created`, `closed`,
  `renamed`, `moved`, `reordered`, `updated`, `focused`) plus `pane.focused`,
  `tab.focused`, `pane.created`, `tab.created` and `tab.closed`. The latter five exist so
  a `cd` gets picked up — see [Changing directory](#changing-directory).
- `[[actions]]` — `sync`, for a manual resync. (Diagnostics are the `--doctor`
  *flag*, run directly — see Diagnostics.)

`src/adopt.py` is registered only as the `adopt` action, never on an event: `folders` is
regenerable and a Space is not, so importing can only be one-shot and additive.

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

## The other direction: adopting a workspace file

`bin/sync` mirrors Herdr **into** a workspace file. `bin/adopt` goes the other way: it
reads a `.code-workspace` file's `folders` and creates the Herdr Spaces they describe.

Pick one per Herdr session. They are **mutually exclusive**, and adopt enforces it.

| You want | Tool | Session config |
| --- | --- | --- |
| *"I already have a workspace file. Set Herdr up to match it."* | **adopt** | **no** `workspaceFile` for this session |
| *"This is a dynamic workspace. Let VS Code follow whatever I do in Herdr."* | **sync** | a `workspaceFile` for this session |

### Why not both

The two directions look symmetric and are not. `folders` is **regenerable** — it can
always be recomputed from Herdr and overwritten, which is why sync is safe on twelve
event hooks. A Space is **not**: it owns tabs, panes, running agents and scrollback, so
it can only ever be *added*. Adopt is therefore one-shot, additive and explicitly
invoked, and is deliberately not registered on any event.

Running both against one session would also break in three concrete ways, which the
mutual-exclusivity rule removes at a stroke:

- **A feedback loop.** Each create fires `workspace.created`, which runs the sync hook,
  which rewrites the file.
- **`mode: "active"`.** The file holds a single folder, so adopting from it and then
  syncing truncates the Spaces straight back out.
- **`pinnedFolders`.** Pins live in the file but must never become Spaces.

### The guard

Adopt refuses, exiting **2**, whenever this session resolves to a `workspaceFile`. The
check calls `config.load()` — the same four resolution rules sync uses — so the two can
never disagree about which session owns which file:

| Situation | Adopt |
| --- | --- |
| no `config.json` at all | runs |
| session has no entry, and is not the default reaching a top-level `workspaceFile` (rule 4) | runs |
| `sessions[<this session>]` is set (rule 2) | **refuses** |
| default session with a top-level `workspaceFile` (rule 3) | **refuses** |
| `HERDR_VSCODE_SYNC_WORKSPACE_FILE` is set | **refuses** |
| `config.json` exists but does not parse | **fails**, exit 1 — a typo'd sync config must not read as "no config" |

### Usage

```sh
./bin/adopt --dry-run          # print the plan, create nothing
./bin/adopt                    # create the missing Spaces
./bin/adopt --file ~/x.code-workspace
./bin/adopt --relabel          # also fix labels on Spaces that already exist
```

With no `--file`, adopt uses the single `*.code-workspace` in the current directory.
Zero or more than one is an error naming them — it never guesses.

`scripts/herdrvs` in this repo wraps that as a shell function. Source it from your shell
config and `herdrvs` works from any project directory:

```sh
source /path/to/herdr-plugins/scripts/herdrvs
cd ~/code/my-project && herdrvs --dry-run
```

It is a locator and nothing more — it finds the plugin root (via
`$HERDR_VSCODE_SYNC_ROOT`, else `herdr plugin list --json`) and passes every argument
through, so all the flags above work.

There is also an `adopt` plugin action, for the one-click case:

```sh
herdr plugin action invoke adopt --plugin vscode-workspace-sync
```

It searches the **focused pane's** directory (a plugin command's own cwd is the plugin
root, so `HERDR_PLUGIN_CONTEXT_JSON` is what supplies the real one). Direct invocation
stays the primary interface, because actions accept no arguments — there is no
`--dry-run` and no `--file` through that route, and stdout reaches only
`herdr plugin log list`, JSON-escaped. Same reasoning that keeps `--doctor` a flag.

### What adopt does, per folder

Paths are resolved the way VS Code resolves them — a relative `path` is relative to the
**workspace file's own directory**, not `$PWD` — then normalised by the same
`resolve_path` the sync direction uses, so a path matches a Space by exactly the rule
that emitted one.

| Condition | Action |
| --- | --- |
| not an existing directory | **skip**, with a warning |
| already the cwd of some Space | **exists**, nothing created; with `--relabel` and a differing `name`, renamed |
| otherwise | **create** — `--cwd <absolute> [--label <name>] --no-focus` |

`--label` is passed only when the file supplies a `name`; Herdr derives the label from
the basename otherwise. Creates run in file order, so the sidebar matches the file when
starting from empty. `--no-focus` throughout, so adopting never moves you.

Entries that are unusable — not an object, no string `path`, or a `${...}` VS Code
variable that cannot be expanded outside the editor — are warned about and dropped
rather than being fatal.

Spaces present in Herdr but **absent from the file** are listed and left alone. There is
no `--close-extra`: closing a Space kills its tabs, panes and any running agent. A fresh
session's initial Space is the usual entry in that list.

A failed create is reported and the run continues; the process exits 1 if any failed.

### Measured Herdr behaviour this defends against

Probed on **herdr 0.8.2**; evidence in
[`docs/herdr-vscode-sync-facts.md` §19](../docs/herdr-vscode-sync-facts.md). All three
are **silent** — none of them produces an error:

| Behaviour | Consequence |
| --- | --- |
| `workspace create` does **not** dedupe by path, and does not update the existing label | without adopt's own dedupe, every re-run would double the sidebar |
| a `--cwd` naming a **nonexistent** directory succeeds, rooting the Space at `$HOME` | hence the `isdir` check on every folder |
| a **relative** `--cwd` also lands at `$HOME` | hence resolving to absolute before the call |

## Logging and diagnostics

Every run prints one line to stdout:

```
vscode-workspace-sync: reason=event session=default mode=mirror target=/path/x.code-workspace folders=3 result=wrote
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

## One workspace file per Herdr session

Plugin registration lives in `~/.config/herdr/plugins.json`, which is **not**
session-scoped: a single linked plugin runs in **every** session's server. Two Herdr
sessions pointed at one `workspaceFile` would each compute `folders` from their own Space
list and overwrite the other's.

With no `sessions` key, that is settled bluntly: only the **default** session syncs.
Named sessions log `skipped-session` and exit 0 — whether or not the default session is
also running.

`sessions` gives each one its own file instead, keyed by the name from
`herdr session list`:

```jsonc
{
  "sessions": {
    "default": { "workspaceFile": "~/code/main.code-workspace" },
    "work":    { "workspaceFile": "~/code/work.code-workspace",
                 "pinnedFolders": ["~/code/work"] },
    "oss":     { "workspaceFile": "~/code/oss.code-workspace", "mode": "active" }
  }
}
```

The session name comes from `$HERDR_SOCKET_PATH` — no subprocess, since the hook
environment already carries it:

| Socket | Session |
| --- | --- |
| unset | `default` |
| `~/.config/herdr/herdr.sock` | `default` |
| `~/.config/herdr/sessions/probe/herdr.sock` | `probe` |

Resolution is four rules, in order:

1. Resolve the session name.
2. **`sessions[name]`**, if present. Its `workspaceFile` is required and is never
   inherited from the top level; `mode` and `pinnedFolders` fall back to the top-level
   values unless the entry sets them.
3. Otherwise the **top-level** config — but only for the `default` session.
4. Otherwise nothing applies: log `skipped-session` and exit 0.

`"default"` is a legal key and wins over the top-level config, so a multi-session setup
can live entirely inside `sessions` and omit the top-level `workspaceFile`. Conversely,
with no `sessions` key rules 3 and 4 are exactly the single-session behaviour above, so
an existing config keeps working untouched.

Two entries resolving to the same `workspaceFile` — or one entry colliding with a
top-level file still reachable by rule 3 — is a **hard error** naming both sessions. That
collision is the precise failure this map exists to prevent, so it fails loudly at load
rather than flapping at runtime.

`HERDR_VSCODE_SYNC_WORKSPACE_FILE` overrides all of it, including a rule-4 skip, which is
what makes it usable to drive a named session by hand. `--doctor` reports the socket, the
resolved session, which rule matched, and the whole map.

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
`workspace.focused` are the two the same. `workspace_cwd` is the Space's *current* root,
which the plugin uses for the Space named in a hook, falling back to the pane join for all
the others. It is **not** a stable root: after a `cd` it reports the new subdirectory, the
same as `panes[].cwd`. Herdr stores no Space root at all — `cwd` appears only in
`workspace.create` across the whole socket API, so a Space's directory is derived live
from its active pane. It is **absent on `workspace.closed`**,
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
/usr/bin/python3 -m unittest discover -s test -v     # 199 tests, stdlib unittest only
/usr/bin/python3 -m py_compile src/*.py              # the syntax gate
```

Run the tests with **`/usr/bin/python3`** specifically. The floor is **Python 3.9** —
stock macOS ships 3.9.6 — and a newer interpreter silently accepts `match` statements,
PEP 604 `X | Y` runtime annotations, and `tomllib`, none of which are allowed. There is no
type checker or linter in the loop to catch them.

No module in `src/` may share a name with a stdlib module: `src` is `sys.path[0]` when
a shim hands `src/main.py` or `src/adopt.py` to the interpreter, so `src/types.py` would
shadow the stdlib `types` module and the very first stdlib import would die.
`test_adopt.ShimTest` asserts this against `sys.stdlib_module_names` on 3.10+. That is why the JSON
shapes are documented at the top of `src/herdr.py`.

`test/fixtures/snapshot.json` is a faithful transcription of a real `api snapshot` and its
paths are the ones discovery observed; `test/fixtures/snapshot-portable.json` has the same
shape with paths that exist on any POSIX machine, and is what the automated tests use.

The adopt tests reach Herdr through `test/fake-herdr`, pointed at by `HERDR_BIN_PATH`,
which logs every argv it is handed to `$FAKE_HERDR_LOG`. That is how "adopt passed an
absolute, existing `--cwd`" is asserted — a property with no downstream check, since
Herdr accepts a bad one silently.
