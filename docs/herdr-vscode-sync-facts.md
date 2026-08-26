# Herdr + VS Code sync — observed facts

Discovery run for [`.plans/vscode-workspace-sync.md`](../.plans/vscode-workspace-sync.md),
executed per [`.plans/vscode-workspace-sync-discovery.md`](../.plans/vscode-workspace-sync-discovery.md).

Everything below is **pasted output from the host**, not a restatement of docs. Long
payloads are truncated with `…`; nothing is paraphrased.

- Host: macOS (Darwin 25.5.0), arm64
- Herdr **0.8.0**, protocol 19
- VS Code **1.134.0** (`110a328ea54b42367b803ec53ee0bf52ef26b419`, arm64)
- Date: 2026-08-26
- Probe plugin: `.plans/scratch/herdr-probe` (linked as `herdr-probe`, since unlinked)

## Summary of the three gating answers

| Probe | Question | Answer |
| --- | --- | --- |
| 2 | Where is Space state, and does a workspace record carry `cwd`? | `.result.snapshot.workspaces` — **no `cwd`**. Paths come from `.result.snapshot.panes[].cwd`, or from `HERDR_PLUGIN_CONTEXT_JSON.workspace_cwd` in a hook. |
| 3 | Which call is sidebar order? | **Both.** `api snapshot` and `workspace list` return the same order, and `number` is the 1-based sidebar position. |
| 10 | Does a plugin-root-relative `command` spawn? | **Yes.** cwd is the plugin root. Deno 2.9.5 at `/Users/bingles/.deno/bin/deno`. |

And the go/no-go:

| Probe | Question | Answer |
| --- | --- | --- |
| 13 | VS Code live-reload of `folders`? | **GO.** No window reload in any case; integrated terminals always survived. |
| 13 | Is mode `active` viable? | **Yes, viable** — nothing breaks unpinned. But *any* change to what sits at `folders[0]` restarts the extension host, so a pinned `folders[0]` avoids that cost. |

---

## 1. Herdr version and CLI surface

```
$ herdr --version
herdr 0.8.0
```

All three required commands exist. Note that `herdr plugin` is **not listed** in
`herdr --help` at 0.8.0 — it is present but undocumented at the top level:

```
$ herdr plugin --help
Install and run workflow plugins

Usage: herdr plugin [COMMAND]

Commands:
  install     Install a plugin from GitHub
  uninstall   Uninstall a plugin
  link        Link a local plugin
  unlink      Unlink a local plugin
  enable      Enable a plugin
  disable     Disable a plugin
  list        List installed plugins
  config-dir  Print a plugin config directory
  action      List or invoke plugin actions
  log         Inspect plugin command logs [aliases: logs]
  pane        Manage plugin-owned panes
```

`herdr api snapshot` exists at **0.8.0** — the implementation plan worried it might be
0.8.2-only. It is not:

```
$ herdr api --help
Commands:
  snapshot  Print the live session snapshot
  schema    Print or write the bundled API schema
```

`herdr workspace list` and `herdr plugin log list` both exist. **Nothing is missing**,
and `min_herdr_version = "0.8.0"` is correct.

Also present and useful, not mentioned in the plan:

```
$ herdr api schema --json    # 251 KB JSON Schema of every request/response/event
```

## 2. `api snapshot` shape

The envelope has **three** levels, not two — there is a `snapshot` object the
implementation plan does not account for:

```
$ herdr api snapshot | jq 'keys'
[ "id", "result" ]

$ herdr api snapshot | jq '{id, result_type:.result.type, snapshot_keys:(.result.snapshot|keys)}'
{
  "id": "cli:api:snapshot",
  "result_type": "session_snapshot",
  "snapshot_keys": [
    "agents", "focused_pane_id", "focused_tab_id", "focused_workspace_id",
    "layouts", "panes", "protocol", "tabs", "version", "workspaces"
  ]
}
```

So the real paths are:

- focused workspace id → **`.result.snapshot.focused_workspace_id`** (`"w4"`)
- workspace records → **`.result.snapshot.workspaces`**
- pane records → **`.result.snapshot.panes`**
- `.result.snapshot.version` = `"0.8.0"`, `.result.snapshot.protocol` = `19`

### One complete workspace record, verbatim

Plain (non-worktree) Space:

```json
{
  "active_tab_id": "w1:t1",
  "agent_status": "unknown",
  "focused": false,
  "label": "devc-wksp",
  "number": 1,
  "pane_count": 1,
  "tab_count": 1,
  "workspace_id": "w1"
}
```

Worktree-backed Space — same shape plus a `worktree` object:

```json
{
  "active_tab_id": "w7:t1",
  "agent_status": "unknown",
  "focused": false,
  "label": "probe-x",
  "number": 5,
  "pane_count": 1,
  "tab_count": 1,
  "workspace_id": "w7",
  "worktree": {
    "checkout_path": "/Users/bingles/.herdr/worktrees/herdr-plugins/probe-x",
    "is_linked_worktree": true,
    "repo_key": "/Users/bingles/code/tools/herdr-plugins/.git",
    "repo_name": "herdr-plugins",
    "repo_root": "/Users/bingles/code/tools/herdr-plugins"
  }
}
```

### Answers to the specific questions

- **Does a workspace record carry `cwd`?** **No.** There is no `cwd` field on any
  workspace record, worktree or not. This is the single biggest correction to the
  implementation plan, which reads `path` from "the record's `cwd`".
- **Field names:** id is **`workspace_id`**, label is **`label`**.
- **Explicit order field:** yes — **`number`**, a 1-based sidebar position. It matches
  array index + 1 and **re-sequences** when Spaces are moved or created (see probe 3).

`worktree.checkout_path` is *not* a usable substitute for `cwd`. It attaches **lazily**:
`w6` was created with `--cwd /Users/bingles/code/tools/herdr-plugins`, which is a git
repo root, yet carried no `worktree` object; `w4` only gained one when an unrelated
`herdr worktree create` ran and triggered a repo scan. Do not depend on it.

## 3. `workspace list` shape and ordering

`workspace list` returns the **same** records under a different envelope
(`.result.workspaces`, `.result.type == "workspace_list"`), with no `snapshot` level:

```
$ herdr workspace list
{"id":"cli:workspace:list","result":{"type":"workspace_list","workspaces":[{"active_tab_id":"w1:t1","agent_status":"unknown","focused":false,"label":"devc-wksp","number":1,…},{…"workspace_id":"w4"}]}}
```

### Reorder test

0.8.0 has no `herdr workspace move` CLI, but the socket API has `workspace.move` and
`workspace.move_block` (found in `herdr api schema --json`), which is what a sidebar drag
calls. Driven directly over `$HERDR_SOCKET_PATH`:

```
$ python3 call.py workspace.move '{"workspace_id":"w7","insert_index":0}'
```

Before:

```
1  w1  devc-wksp
2  w4  herdr-plugins
3  w5  devc-wksp
4  w6  renamed
5  w7  probe-x
```

After — **identical in both calls**:

```
=== AFTER: workspace list ===        === AFTER: api snapshot ===
1  w7  probe-x                       1  w7  probe-x
2  w1  devc-wksp                     2  w1  devc-wksp
3  w4  herdr-plugins                 3  w4  herdr-plugins
4  w5  devc-wksp                     4  w5  devc-wksp
5  w6  renamed                       5  w6  renamed
```

**Which call is sidebar order? Both.** `api snapshot` and `workspace list` agree, and
`number` is re-sequenced to match the new array order. The implementation plan's choice to
read order from `api snapshot` is **correct** — array order is authoritative and `number`
is a redundant confirmation of it.

`WorkspaceMoveParams` / `WorkspaceMoveBlockParams`, from the bundled schema:

```json
{ "properties": { "insert_index": {"format":"uint","minimum":0,"type":"integer"},
                  "workspace_id": {"type":"string"} },
  "required": ["workspace_id","insert_index"], "type": "object" }

{ "properties": { "before_workspace_id": {"type":["string","null"]},
                  "workspace_ids": {"items":{"type":"string"},"type":"array"} },
  "required": ["workspace_ids"], "type": "object" }
```

## 4. Space path fallback

Required, because probe 2 found no `cwd`. Paths live on **pane** records, which are
already inside the same `api snapshot` — so **no second call is needed**:

```
$ herdr api snapshot | jq '.result.snapshot.panes'
[
  {
    "agent_status": "unknown",
    "cwd": "/Users/bingles/code/spikes/devc-wksp",
    "foreground_cwd": "/Users/bingles/code/spikes/devc-wksp",
    "focused": false,
    "pane_id": "w1:p1",
    "revision": 1,
    "tab_id": "w1:t1",
    "terminal_id": "term_659e4fac0e01e1",
    "terminal_title": "devc-wksp",
    "terminal_title_stripped": "devc-wksp",
    "workspace_id": "w1",
    "scroll": { … }
  },
  …
]
```

And for the worktree Space, via the fallback the probe called for:

```
$ herdr pane list --workspace w7
{"id":"cli:pane:list","result":{"type":"pane_list","panes":[{"agent_status":"unknown",
"cwd":"/Users/bingles/.herdr/worktrees/herdr-plugins/probe-x",
"foreground_cwd":"/Users/bingles/.herdr/worktrees/herdr-plugins/probe-x",
"focused":false,"pane_id":"w7:p1","revision":1,"tab_id":"w7:t1",
"terminal_id":"term_659f69ea610da15","terminal_title":"probe-x","workspace_id":"w7", …}]}}
```

**Where a usable directory path lives:** `panes[].cwd`, joined to a Space on
`panes[].workspace_id`. Two caveats:

- `cwd` is the pane's shell cwd and **drifts** if the user `cd`s. `foreground_cwd` is the
  foreground process's cwd and drifts more. Neither is a stable "Space root".
- A Space with several panes has several `cwd` values. Pick deterministically — the pane
  matching the Space's `active_tab_id`, or the lowest `pane_id`.

The stable, non-drifting path for a Space is
`HERDR_PLUGIN_CONTEXT_JSON.workspace_cwd`, which the server computes and passes to every
hook (probe 9). It is the better source when the plugin is reacting to one Space.

## 5. Labels

A Space created **without** `--label`:

```
$ herdr workspace create --cwd /Users/bingles/code/spikes/devc-wksp --no-focus
{"id":"cli:workspace:create","result":{…"workspace":{"active_tab_id":"w5:t1",
"agent_status":"unknown","focused":false,"label":"devc-wksp","number":3,
"pane_count":1,"tab_count":1,"workspace_id":"w5"}}}
```

**`label` is auto-derived from the directory basename.** It is never empty and never
null.

Two consequences the implementation plan needs:

- `useSpaceLabels: true` plus the rule "emit `name` only when the label differs from the
  path's basename" means **`name` is almost never emitted by default**. That is a
  reasonable outcome, but it should be stated rather than discovered.
- **Labels are not unique.** `w1` and `w5` were both `"devc-wksp"` at the same time. Any
  keying or `excludeLabels` matching must tolerate duplicates.

## 6. Manifest accepts the event names

All seven `[[events]]` blocks were accepted on the first `plugin link` — **no bisect was
needed, nothing was rejected**:

```
$ herdr plugin link .plans/scratch/herdr-probe
{"id":"cli:plugin","result":{"plugin":{…"events":[
  {"command":["./probe.sh"],"on":"workspace.closed"},
  {"command":["./probe.sh"],"on":"workspace.created"},
  {"command":["./probe.sh"],"on":"workspace.focused"},
  {"command":["./probe.sh"],"on":"workspace.moved"},
  {"command":["./probe.sh"],"on":"workspace.renamed"},
  {"command":["./probe.sh"],"on":"workspace.reordered"},
  {"command":["./probe.sh"],"on":"workspace.updated"}],
  "plugin_id":"herdr-probe","plugin_root":"/Users/bingles/code/tools/herdr-plugins/.plans/scratch/herdr-probe",
  "min_herdr_version":"0.8.0","source":{"kind":"local"},"startup":[{"command":["./probe.sh"]}],
  "warnings":["manifest does not declare platforms; platform support unknown"]},
  "type":"plugin_linked"}}
```

One warning, worth noting because the implementation plan's manifest does declare
`platforms` and so will not see it:

```
warning: manifest does not declare platforms; platform support unknown
```

`plugin link` did **not** run the `[[startup]]` block (see probe 12).

## 7. Which events actually fire

**All seven fire.** `HERDR_PLUGIN_EVENT` is always the **dotted** name; the `event` field
*inside* `HERDR_PLUGIN_EVENT_JSON` is the **underscored** name. The 0.8.0 observation
recorded in the plan is confirmed.

| Event | Verdict | Triggered by |
| --- | --- | --- |
| `workspace.created` | **FIRED** | `herdr workspace create`, and again by `herdr worktree create` |
| `workspace.closed` | **FIRED** | `herdr workspace close` |
| `workspace.renamed` | **FIRED** | `herdr workspace rename` |
| `workspace.moved` | **FIRED** | socket `workspace.move` (a single-Space sidebar drag) |
| `workspace.reordered` | **FIRED** | socket `workspace.move_block` (multi-Space reorder) |
| `workspace.updated` | **FIRED** | worktree metadata being attached to an existing Space |
| `workspace.focused` | **FIRED** | `herdr workspace focus`, and re-emitted after a move |

The log, in order, across the whole probe run:

```
$ herdr plugin log list --plugin herdr-probe
plugin-log-6  action=hello
plugin-log-7  action=probe
plugin-log-8  event=workspace.created      # workspace create --label demo --no-focus
plugin-log-9  event=workspace.renamed      # workspace rename w6 renamed
plugin-log-10 event=workspace.focused      # workspace focus w6
plugin-log-11 event=workspace.focused      # workspace focus w4
plugin-log-12 event=workspace.updated      # <- worktree create, on the *other* Space
plugin-log-13 event=workspace.created      # <- worktree create, the new Space
plugin-log-14 event=workspace.moved        # workspace.move w7 -> index 0
plugin-log-15 event=workspace.focused      # re-emitted after the move
plugin-log-16 event=workspace.reordered    # workspace.move_block
plugin-log-17 event=workspace.focused      # re-emitted after the reorder
plugin-log-18 event=workspace.closed       # workspace close w6
```

Cross-checked against the cumulative sink the probe wrote to
`$HERDR_PLUGIN_STATE_DIR/probe.log`:

```
$ grep 'HERDR_PLUGIN_EVENT  ' probe.log | sort | uniq -c
   1 HERDR_PLUGIN_EVENT      : <unset>          # the action invocation
   1 HERDR_PLUGIN_EVENT      : workspace.closed
   2 HERDR_PLUGIN_EVENT      : workspace.created
   4 HERDR_PLUGIN_EVENT      : workspace.focused
   1 HERDR_PLUGIN_EVENT      : workspace.moved
   1 HERDR_PLUGIN_EVENT      : workspace.renamed
   1 HERDR_PLUGIN_EVENT      : workspace.reordered
   1 HERDR_PLUGIN_EVENT      : workspace.updated
```

### `workspace.updated` — what triggers it

It fires, and it is **not** droppable. `herdr worktree create` emitted
`workspace.updated` for **`w4`**, a Space that was not the subject of the command: Herdr
scanned the repo and attached `worktree` metadata to the already-open Space rooted at the
repo root, then emitted the update.

```json
{"event":"workspace_updated","data":{"type":"workspace_updated","workspace":{
  "workspace_id":"w4","number":2,"label":"herdr-plugins","focused":true,
  "pane_count":2,"tab_count":2,"active_tab_id":"w4:t3","agent_status":"working",
  "worktree":{"repo_key":"/Users/bingles/code/tools/herdr-plugins/.git",
    "repo_name":"herdr-plugins","repo_root":"/Users/bingles/code/tools/herdr-plugins",
    "checkout_path":"/Users/bingles/code/tools/herdr-plugins","is_linked_worktree":false}}}}
```

### Noise to expect

- A **move re-emits `workspace.focused`** afterwards (log-14→15, log-16→17). One user
  gesture, two hook invocations.
- `worktree create` emits **`workspace.updated` + `workspace.created`** — two
  invocations.

The plan's recompute-from-scratch/idempotent design absorbs both correctly; this is
recorded only so the extra runs in the log are not read as bugs.

### An eighth event exists

`herdr api schema --json` lists `workspace.metadata_updated` (payload type
`workspace_metadata_updated`) at 0.8.0. It is display-only badge state —
`workspace.report_metadata` takes `{workspace_id, source, tokens, seq?, ttl_ms?}` where
`tokens` is a ≤16-key string map. **Irrelevant to folder sync; correctly not hooked.**
(The plan's stated reason — "the docs state it does not invoke plugin event hooks" — was
not tested here; the accurate reason is that it carries no path, label, or order data.)

## 8. Event payload shape

Envelope is always `{"event": "<underscored>", "data": {"type": "<underscored>", …}}`.

**`workspace.created`** — full record, but **no `cwd`**:

```json
{"event":"workspace_created","data":{"type":"workspace_created","workspace":{
  "workspace_id":"w6","number":4,"label":"demo","focused":false,
  "pane_count":1,"tab_count":1,"active_tab_id":"w6:t1","agent_status":"unknown"}}}
```

**`workspace.renamed`** — ids only:

```json
{"event":"workspace_renamed","data":{"type":"workspace_renamed","workspace_id":"w6","label":"renamed"}}
```

**`workspace.focused`** — id only:

```json
{"event":"workspace_focused","data":{"type":"workspace_focused","workspace_id":"w4"}}
```

**`workspace.closed`** — id plus the record as it was:

```json
{"event":"workspace_closed","data":{"type":"workspace_closed","workspace_id":"w6",
  "workspace":{"workspace_id":"w6","number":4,"label":"renamed","focused":false,
  "pane_count":1,"tab_count":1,"active_tab_id":"w6:t1","agent_status":"unknown"}}}
```

**`workspace.moved`** — carries **the entire ordered workspaces array**:

```json
{"event":"workspace_moved","data":{"type":"workspace_moved","workspace_id":"w7","insert_index":0,
  "workspaces":[{"workspace_id":"w7","number":1,"label":"probe-x",…},
                {"workspace_id":"w1","number":2,…},{"workspace_id":"w4","number":3,…},
                {"workspace_id":"w5","number":4,…},{"workspace_id":"w6","number":5,…}]}}
```

**`workspace.reordered`** — same, plus the moved set:

```json
{"event":"workspace_reordered","data":{"type":"workspace_reordered","workspace_ids":["w7"],
  "workspaces":[{"workspace_id":"w1","number":1,…},…,{"workspace_id":"w7","number":5,…}]}}
```

**Could the implementation skip the `api snapshot` call?** **No — not in general.**
`moved` and `reordered` do carry the full ordered array, but:

- no payload carries `cwd` for any Space, and
- `renamed` and `focused` carry only ids.

Since the plugin needs a path per Space, `api snapshot` (for `panes[].cwd`) remains
necessary in `mirror` mode. The plan's "never read the event payload, always recompute"
rule is therefore not just a tidiness choice — it is **forced** by the payloads.

## 9. `HERDR_PLUGIN_CONTEXT_JSON` shape

**From an action invocation** (`herdr plugin action invoke probe --plugin herdr-probe`):

```json
{"workspace_id":"w4","workspace_label":"herdr-plugins",
 "workspace_cwd":"/Users/bingles/code/tools/herdr-plugins",
 "tab_id":"w4:t3","tab_label":"2","focused_pane_id":"w4:pC",
 "focused_pane_cwd":"/Users/bingles/code/tools/herdr-plugins",
 "focused_pane_agent":"claude","focused_pane_status":"working",
 "invocation_source":"cli","correlation_id":"cli:plugin"}
```

**From an event hook** (`workspace.created` for `w6`, created with `--no-focus`):

```json
{"workspace_id":"w6","workspace_label":"demo",
 "workspace_cwd":"/Users/bingles/code/tools/herdr-plugins",
 "tab_id":"w6:t1","tab_label":"1","focused_pane_id":"w6:p1",
 "focused_pane_cwd":"/Users/bingles/code/tools/herdr-plugins",
 "focused_pane_status":"unknown",
 "invocation_source":"api","correlation_id":"workspace.created"}
```

### Does it name the focused workspace id?

**No — and this is a correction to the probe's own hypothesis.** `workspace_id` is the
**subject of the event**, not the focused Space. The proof is above: `w6` was created with
`--no-focus`, the payload says `"focused":false`, `w4` was the focused Space throughout —
and context still reported `workspace_id: "w6"`.

So **mode `active` cannot read the focused Space from context in general.** Two usable
routes:

1. On **`workspace.focused` specifically**, subject == focused, and context supplies both
   the id and `workspace_cwd`. That one event can be handled without `api snapshot`.
2. Every other invocation must read `.result.snapshot.focused_workspace_id`.

### Other context facts

- **`workspace_cwd` is present and is the stable Space root** — better than
  `panes[].cwd`, which drifts. A `worktree` object is included when the Space has one.
- `invocation_source` distinguishes `"cli"` (action), `"api"` (event), `"startup"`.
- `correlation_id` is the event name for events, `"cli:plugin"` for CLI actions,
  `"plugin.startup"` for startup.
- Fields are **omitted, not nulled**, when unavailable — `focused_pane_agent` is absent
  when no agent is detected.
- **`workspace.closed` context has no `workspace_cwd`**, because the Space is gone:

  ```json
  {"workspace_id":"w6","workspace_label":"renamed","tab_id":"w6:t1",
   "invocation_source":"api","correlation_id":"workspace.closed"}
  ```

  A plugin that wants the closed Space's path must have cached it. Removing by id is
  simpler and is what the plan already does.

## 10. Server environment and relative-path spawn

### Relative-path spawn — **WORKS**

`[[actions]]` entry with `command = ["./bin/hello"]`:

```
$ herdr plugin action invoke hello --plugin herdr-probe
$ herdr plugin log list --plugin herdr-probe
{"action_id":"hello","command":["./bin/hello"],"exit_code":0,"status":"succeeded",
 "stdout":"ok: relative-path spawn works; cwd=/Users/bingles/code/tools/herdr-plugins/.plans/scratch/herdr-probe\n"}
```

**Not a blocker.** cwd is the plugin root, exactly as the design assumes. Confirmed for
both `./probe.sh` and `./bin/hello`, and for event hooks as well as actions.

### `PATH` independence — **the expected finding did NOT hold**

The plan predicts a minimal server `PATH`. On this host it is the **opposite**: the server
inherited the full interactive user `PATH`, and every tool resolves.

```
PATH=/Users/bingles/Library/Application Support/Code/User/globalStorage/github.copilot-chat/debugCommand:…
     :/Users/bingles/.local/bin:/Users/bingles/bin:…:/opt/homebrew/bin:…
     :/Users/bingles/.deno/bin:/Users/bingles/go/bin:/Users/bingles/go/bin
/Users/bingles/.deno/bin/deno
/Users/bingles/.nvm/versions/node/v24.13.0/bin/node
/opt/homebrew/bin/git
```

`command -v deno`, `command -v node`, and `command -v git` **all resolved**. The `PATH` is
visibly duplicated and contains VS Code `globalStorage` entries, which is the giveaway:
this Herdr server was launched from a VS Code integrated terminal and **inherited that
shell's environment**.

**This does not invalidate the compiled-binary design — it narrows its justification.**
`PATH` here is an accident of how the server happened to be started. A server started from
launchd, from a login item, or from a bare `herdr` in a non-login shell would carry
something quite different. The design should be defended as *"the server's `PATH` is
whatever launched it, therefore unknowable"* rather than *"the server's `PATH` is
minimal"*. The `env -i` PATH-independence test in the plan's offline validation is still
exactly the right test.

`$HERDR_BIN_PATH` **is** provided and is the reliable way to reach Herdr:

```
HERDR_BIN_PATH=/Users/bingles/.local/bin/herdr
```

Full plugin environment, verbatim:

```
HERDR_BIN_PATH=/Users/bingles/.local/bin/herdr
HERDR_ENV=1
HERDR_PANE_ID=w6:p1
HERDR_PLUGIN_ACTION_ID=probe                 # actions only
HERDR_PLUGIN_CONFIG_DIR=/Users/bingles/.config/herdr/plugins/config/herdr-probe
HERDR_PLUGIN_CONTEXT_JSON={…}
HERDR_PLUGIN_EVENT=workspace.created         # events only
HERDR_PLUGIN_EVENT_JSON={…}                  # events only
HERDR_PLUGIN_ID=herdr-probe
HERDR_PLUGIN_ROOT=/Users/bingles/code/tools/herdr-plugins/.plans/scratch/herdr-probe
HERDR_PLUGIN_STATE_DIR=/Users/bingles/.local/state/herdr/plugins/herdr-probe
HERDR_SOCKET_PATH=/Users/bingles/.config/herdr/herdr.sock
HERDR_TAB_ID=w6:t1
HERDR_WORKSPACE_ID=w6
```

Note `HERDR_PLUGIN_CONFIG_DIR` and `HERDR_PLUGIN_STATE_DIR` are **different roots**
(`~/.config/herdr/plugins/config/<id>` vs `~/.local/state/herdr/plugins/<id>`), matching
the plan's usage.

### Deno on the host — for building

```
$ deno --version
deno 2.9.5 (stable, release, aarch64-apple-darwin)
v8 15.0.245.2-rusty
typescript 6.0.3

$ command -v deno
/Users/bingles/.deno/bin/deno
```

**Deno 2.9.5**, absolute path `/Users/bingles/.deno/bin/deno`. Satisfies the Deno 2.x
minimum, so `herdr plugin install` can run `[[build]]`. Same version as the devcontainer's
2.9.5, so offline and host builds agree. `git` is 2.52.0.

## 11. Events with no client attached

Tested without disturbing the live session: a second named session was started, its client
detached, and an event fired against its socket.

```
$ herdr session list
name       status   directory                                     socket
default    running  /Users/bingles/.config/herdr                  /Users/bingles/.config/herdr/herdr.sock
probe      running  /Users/bingles/.config/herdr/sessions/probe   /Users/bingles/.config/herdr/sessions/probe/herdr.sock
```

Detach (`ctrl+b` `q`), confirming the server outlives the client:

```
herdr: detached from server
Run `herdr session attach probe` to reattach

$ herdr session list
probe      running   …          # still running with no client
```

Then, with **no client attached**:

```
$ HERDR_SOCKET_PATH=…/sessions/probe/herdr.sock herdr workspace create \
      --cwd /Users/bingles/code/spikes/devc-wksp --label detached-test --no-focus
{"active_tab_id":"w2:t1",…,"label":"detached-test","number":2,"workspace_id":"w2"}
```

Probe log:

```
when                    : 2026-08-26T12:39:37-0500
HERDR_PLUGIN_EVENT      : workspace.created
HERDR_SOCKET_PATH=/Users/bingles/.config/herdr/sessions/probe/herdr.sock
```

**The hook fired with no client attached.** Hooks are server-side and independent of
client attachment — so the workspace file stays in sync even while the user is detached.

### Unplanned finding: plugins are global across sessions

The probe plugin was linked once, yet it **ran in the second session's server too** — note
the differing `HERDR_SOCKET_PATH` above. Plugin registration lives in
`~/.config/herdr/plugins.json`, which is not session-scoped.

**This is a real risk the implementation plan does not cover.** If the user ever runs two
Herdr sessions, two independent servers will each run `vscode-workspace-sync` against the
*same* configured `workspaceFile`, each computing `folders` from *its own* Space list, and
they will fight — each overwriting the other's folders. The `sync.lock` makes each write
atomic but does nothing about two servers with legitimately different views. See
[Discovery corrections](../.plans/vscode-workspace-sync.md#discovery-corrections).

## 12. Startup hook timing

The `[[startup]]` block did **not** run on `herdr plugin link`. It ran when a **server
started** — captured via the second session's boot rather than by restarting the live
server:

```
when                    : 2026-08-26T12:38:44-0500
HERDR_PLUGIN_EVENT      : startup
HERDR_SOCKET_PATH=/Users/bingles/.config/herdr/sessions/probe/herdr.sock
```

Full record:

```
HERDR_PLUGIN_EVENT      : startup
HERDR_PLUGIN_ACTION_ID  : <unset>

--- HERDR_PLUGIN_EVENT_JSON (verbatim) ---
<unset>

--- HERDR_PLUGIN_CONTEXT_JSON (verbatim) ---
{"workspace_id":"w1","workspace_label":"herdr-plugins",
 "workspace_cwd":"/Users/bingles/code/tools/herdr-plugins",
 "tab_id":"w1:t1","tab_label":"1","focused_pane_id":"w1:p1",
 "focused_pane_cwd":"/Users/bingles/code/tools/herdr-plugins",
 "focused_pane_status":"unknown",
 "invocation_source":"startup","correlation_id":"plugin.startup"}
```

Findings:

- **`HERDR_PLUGIN_EVENT` is the literal string `startup`** — not a dotted
  `plugin.startup`, and not unset. A hook that switches on `HERDR_PLUGIN_EVENT` must
  handle it.
- **`HERDR_PLUGIN_EVENT_JSON` is unset** for startup. Code that unconditionally parses it
  will fail.
- `HERDR_PLUGIN_CONTEXT_JSON` **is** set, with `invocation_source: "startup"` and
  `correlation_id: "plugin.startup"`.
- Startup ran **before** the first `workspace.focused` of that session.
- The session's *initial* workspace emitted **no `workspace.created`** — only `startup`
  then `workspace.focused`. A plugin relying on `created` to learn about Spaces would miss
  the first one; recompute-from-scratch handles it.

## 13. VS Code live-reload — **GO**

### Method

The Herdr server on this host is a **child of a VS Code integrated terminal**:

```
 2019  1130 herdr
 1130 18973 /bin/bash --init-file …/shellIntegration-bash.sh
18973 18963 …/Code Helper.app/…/Code Helper          # pty-host
18963     1 …/Visual Studio Code.app/…/Code          # main
```

Editing *that* window's workspace file could kill this session, so probe 13 ran in a
**separate throwaway VS Code window** on a scratch `.code-workspace` with uniquely-named
git repos (`p13-alpha`…`p13-delta`), leaving the user's windows untouched.

Reload was measured objectively rather than by eye, using `code --status`, which labels
processes per window:

```
    0	450359962737	 29248	window [22] (PROBE13)
    0	135107988821	 29259	extension-host [22]
    0	 45035996274	 29369	     … json-language-features/…/jsonServerMain --clientProcessId=29259
    0	 90071992547	 29295	file-watcher [22]
```

- **renderer pid changes** → the window was replaced
- **extension-host pid changes** → extensions all reactivated
- **pty-host child pids change** → integrated terminals died
- `code --status` "Workspace Stats" lists the folders VS Code has actually adopted, which
  is how each edit was confirmed to have been picked up at all

### Results — one edit per case, 9 s settle each

| Case | Edit | Window reloaded | Ext host | Terminals | VS Code adopted it |
| --- | --- | --- | --- | --- | --- |
| A | append folder at end | **no** | survived | all survived | yes — `delta` appeared |
| B | remove folder from middle | **no** | survived | all survived | yes — `beta` gone |
| C | reorder two folders | **no** | survived | all survived | yes |
| D | **change `folders[0]`** | **no** | **RESTARTED** | all survived | yes |
| D2 | change `folders[0]` again | **no** | **RESTARTED** | all survived | yes |
| MID | change `folders[1]` | **no** | survived | all survived | yes |
| E | add folder with a `name` field | **no** | survived | all survived | yes |

### Second run — isolating *why* `folders[0]` matters

The first run only ever *replaced* the path at index 0, leaving open whether other
gestures that change which folder is first behave the same. A second window
(`REORDERTEST`, window [23], five scratch git repos `r0`–`r4`) tested those, with
negative and positive controls on either side. The extension host was resolved per-window
by parsing `code --status` for `extension-host [23]`.

| Case | Edit | `folders[0]` changed? | Ext host | Expected | Match |
| --- | --- | --- | --- | --- | --- |
| CTRL-APPEND | append `r3` at end | no | survived | no restart | ✓ |
| CTRL-SWAP12 | swap indices 1↔2 | no | survived | no restart | ✓ |
| **GAP-SWAP01** | **swap indices 0↔1** | **yes** | **RESTARTED** | — | — |
| **GAP-REMOVE0** | **remove `folders[0]`**, promoting index 1 | **yes** | **RESTARTED** | — | — |
| **GAP-INSERT0** | **insert a new folder at index 0** | **yes** | **RESTARTED** | — | — |
| CTRL-REPLACE0 | replace index 0 (repeat of case D) | yes | RESTARTED | restart | ✓ |
| CTRL-AFTER | swap indices 1↔2 *after* the churn | no | survived | no restart | ✓ |

```
CTRL-APPEND    folders=[r0 r1 r2 r3  ] exthost 84897->84897  NO-RESTART expected=NO-RESTART
CTRL-SWAP12    folders=[r0 r2 r1 r3  ] exthost 84897->84897  NO-RESTART expected=NO-RESTART
GAP-SWAP01     folders=[r2 r0 r1 r3  ] exthost 84897->89238  RESTARTED
GAP-REMOVE0    folders=[r0 r1 r3     ] exthost 89238->90553  RESTARTED
GAP-INSERT0    folders=[r2 r0 r1 r3  ] exthost 90553->91874  RESTARTED
CTRL-REPLACE0  folders=[r4 r0 r1 r3  ] exthost 91874->93629  RESTARTED  expected=RESTARTED
CTRL-AFTER     folders=[r4 r1 r0 r3  ] exthost 93629->93629  NO-RESTART expected=NO-RESTART
```

The renderer pid was unchanged throughout — **no case reloaded the window**, restarts
included.

**The trigger is the identity of `folders[0]`, nothing else.** `GAP-SWAP01` settles it: the
folder **set was identical** and only the order changed, yet the extension host restarted —
while `CTRL-SWAP12`, equally a pure reorder, did not. So it is not set membership, not the
file having been written, and not reordering in general. `CTRL-AFTER` rules out the
restarts being spurious churn: a non-index-0 edit immediately after four restarts still
cost nothing.

Correlation is perfect across all seven cases: **extension host restarts if and only if the
path at index 0 changed.**

Raw output for the two decisive cases:

```
######## CASE D                          ######## CASE MID
file folders   : p13-beta p13-delta p13-gamma    file folders   : p13-alpha p13-beta p13-gamma
exthosts before: 33562 46669 67287               exthosts before: 39730 46669 67287
exthosts after : 38228 46669 67287               exthosts after : 39730 46669 67287
VERDICT reload : YES - extension host RESTARTED  VERDICT reload : NO  - extension host survived
renderer same  : yes                             renderer same  : yes
ptyshells same : yes - all terminals survived     ptyshells same : yes - all terminals survived
write-back     : file NOT rewritten by VS Code    write-back     : file NOT rewritten by VS Code
```

Terminals and the Herdr server were intact after every case:

```
$ pgrep -P 18973 | sort -n          # baseline: 1130 48310 57444 59478 62499 78334 81447 89401
1130 48310 57444 59478 62499 78334 81447 89401
$ ps -o pid,ppid,comm= -p 2019
 2019  1130 herdr
```

Folder **order** and the `name` field are the one thing `code --status` cannot report
(its Workspace Stats listing is unordered and always shows the directory basename), so
these were confirmed visually. With the file set to `gamma, alpha, {name: ZZ-DISPLAY-NAME → beta}`,
the explorer showed:

```
EXPLORER
  > P13-GAMMA
  > P13-ALPHA
  > ZZ-DISPLAY-NAME
```

**Order propagates live, and `name` overrides the displayed label.**

### Verdict

**GO.** No case caused a window reload. The renderer survived every edit, and **every
integrated terminal survived every edit** — including the one running Herdr. The
load-bearing assumption of the whole plugin holds.

**Mode `active` is viable** — with one important qualification. `folders[0]` is genuinely
special: replacing it restarts the extension host (reproduced twice), while replacing
`folders[1]` does not. A naive `active` mode whose single folder *is* `folders[0]` would
restart the extension host on **every Space switch**. With a pinned folder occupying
`folders[0]`, `active` mode only ever rewrites index 1+ and costs nothing.

This makes the plan's "pin at least one folder" line a **requirement for `active` mode**,
not just a recommendation.

## 14. VS Code write-back

Two distinct cases, and they differ completely.

### When the plugin writes the file — VS Code leaves it alone

Across all seven probe-13 edits, `write-back` was `file NOT rewritten by VS Code` every
time. After seven external edits the file still had its comment and trailing commas:

```jsonc
{
  // probe 14: does VS Code preserve this comment and the trailing commas?
  "folders": [
    { "path": "…/p13-alpha" },
    { "path": "…/p13-beta" },
    { "path": "…/p13-gamma" },
    { "name": "DELTA-RENAMED", "path": "…/p13-delta" },
  ],
  "settings": {
    "window.title": "PROBE13",
  },
}
```

**VS Code does not reformat the file just because it changed on disk.** The plan's
byte-preserving splice is safe.

### When VS Code writes the file — it rewrites everything

**Correction (retested 2026-08-26).** `code --add` is **not** equivalent to the in-editor
"Add Folder to Workspace" command, and the two differ on the single most important point.
Both results are below; the UI result is the one that matters for design decisions, since
that is the path a user actually takes.

#### Via the in-editor command — comments SURVIVE

"Add Folder to Workspace" run from the command palette on a file carrying two comments and
trailing commas:

```json
{
  // COMMENT-MARKER: does this survive a UI folder add?
  "folders": [
    {
      "path": "…/wb/one"
    },
    {
      "path": "…/wb/two"
    },
    {
      "path": "three"
    }
  ],
  "settings": {
    // COMMENT-MARKER-2 inside settings
    "window.title": "WRITEBACK"
  }
}
```

**Both comments survived**, including the one nested inside `settings`. What did change:
trailing commas were stripped, every folder object was expanded to one property per line,
and the new path was written **relative** to the workspace file's directory.

This is consistent with VS Code using surgical `jsonc` edits (the same machinery that keeps
comments in `settings.json`) rather than a re-serialise.

#### Via `code --add` — comments are deleted

The CLI path *does* re-serialise and drops comments (original observation below). Do not
generalise from it to the UI:

```
$ code -r probe13.code-workspace       # target this window deterministically
$ code --add …/scratchpad/p13-epsilon
```

Result:

```json
{
  "folders": [
    {
      "path": "/private/tmp/…/scratchpad/p13-alpha"
    },
    {
      "path": "/private/tmp/…/scratchpad/p13-beta"
    },
    {
      "path": "/private/tmp/…/scratchpad/p13-gamma"
    },
    {
      "name": "DELTA-RENAMED",
      "path": "/private/tmp/…/scratchpad/p13-delta"
    },
    {
      "path": "p13-epsilon"
    }
  ],
  "settings": {
    "window.title": "PROBE13"
  }
}
```

Four things happened **on this CLI path**:

1. **The comment was deleted** — *this does not happen via the UI command.*
2. **Trailing commas were stripped** — normalized to strict JSON. (Also true via the UI.)
3. **Every folder object was expanded to multi-line**, one property per line. (Also true
   via the UI.)
4. **The newly added path was written relative** (`"p13-epsilon"`), resolved against the
   workspace file's own directory. Pre-existing absolute paths were left absolute. (Also
   true via the UI.)

**Net, for design purposes:** VS Code **preserves comments** when the user adds a folder
through the editor, strips trailing commas, expands folder objects, and emits relative
paths. A plugin that destroys comments would therefore be *worse* than the editor, not
merely equivalent to it — which strengthens the case for a comment-preserving splice.

**Does the plugin fight the editor over path form? Yes.** Two required consequences:

- **Reading must accept relative paths** and resolve them against
  `dirname(workspaceFile)`. The plan's `computeFolders` only ever emits absolute paths and
  never reads existing ones, so this matters for the "is it unchanged?" comparison — a
  file VS Code has touched will contain relative paths that are equal-but-not-identical to
  the plugin's absolute ones, and a naive text compare will rewrite the file every run.
- The plan's existing README warning ("manage folders through Herdr, not the VS Code UI")
  is **correct and now evidence-backed** — a single UI folder-add destroys the user's
  comments and formatting throughout the file, not just in `folders`.

## 15. Extension-host churn

Measured rather than eyeballed, by watching the window's extension host and its
language-server children across a `folders[0]` change with a real repo
(`/Users/bingles/code/tools/herdr-plugins`) in the folder set:

```
### BEFORE folders[0] change
  exthost[22] : 39730 46669 67287
  its children: 39757
  filewatchers: 3
### AFTER folders[0] change
  exthost[22] : 45146 46669 67287
  its children: 45160
  filewatchers: 3
```

- The window's **extension host was replaced** (39730 → 45146).
- Its **language-server child was replaced** with it (39757 → 45160). Everything the
  extension host had spawned died and came back — in the user's real windows that same
  set includes `tsserver.js` ×2, `eslintServer.js`, `deno lsp`, `jsonServerMain`,
  `markdown-language-features`, and `pet server`.
- The per-window **`file-watcher` process survived** (count steady at 3, one per window).
- **Integrated terminals survived**, here and in every probe-13 case.

Qualitatively: a `folders[0]` change costs a **full extension reactivation** for that
window — Git re-initialises its repositories, every language server restarts and re-indexes,
and extension state resets. On a workspace containing large repos that is seconds of CPU
and a visibly busy window. It is not a window reload and it does not disturb the terminal,
but it is not free.

**Every other folder mutation — append, remove-middle, reorder, replace at index ≥ 1, add
with `name` — caused no extension-host churn at all.**

This is the measured cost that justifies the pinned-`folders[0]` recommendation, and it is
what makes the difference between `mirror` mode (churn only when the *first* Space changes)
and an unpinned `active` mode (churn on **every** Space switch).

---

## 18. Which events actually reach plugin hooks (follow-up, 2026-08-26)

Prompted by a user report: changing directory in a tab moves the Space, but the VS Code
folder did not follow. Adding a tab did work.

### A Space's cwd is derived, not stored

`cwd` appears in exactly **one** place in the whole request schema — `WorkspaceCreateParams`.
There is no `workspace.set_cwd` method and no CLI equivalent:

```
$ herdr workspace --help
Commands: list, create, get, focus, rename, report-metadata, close
```

So a Space's directory is computed live from its active pane, and `cd` is the only
mechanism that changes it after creation. Measured — `cd docs` in a Space's active pane:

```
   snapshot pane cwd   = /Users/bingles/code/tools/herdr-plugins/docs
   context workspace_cwd = /Users/bingles/code/tools/herdr-plugins/docs
```

**This corrects probe 9**, which called `workspace_cwd` "the stable Space root … better
than `panes[].cwd`, which drifts". Both follow the `cd`; there is no stable root to have.
That claim was inferred, never tested.

### Only 10 of 27 subscription types invoke plugin hooks

A probe declaring **all 27** linked cleanly — `herdr plugin link` accepted every name —
then the session was exercised (cd, split, focus pane, new tab, focus tab, rename, focus
workspace, run a command, close tab, close workspace):

| Fired | Count in ~75 s |
| --- | --- |
| `pane.agent_status_changed` | 64 (**0.85/s**) |
| `pane.created` | 3 |
| `tab.created`, `tab.focused`, `pane.focused`, `workspace.focused` | 2 each |
| `workspace.created`, `workspace.renamed`, `tab.closed`, `workspace.closed` | 1 each |

**Never fired, despite being accepted by the manifest:** `pane.updated`, `pane.closed`,
`pane.moved`, `pane.exited`, `pane.agent_detected`, `pane.output_matched`,
`pane.scroll_changed`, `workspace.updated`, `workspace.metadata_updated`,
`workspace.moved`, `workspace.reordered`, `tab.renamed`, `tab.moved`, `layout.updated`,
`worktree.created`, `worktree.opened`, `worktree.removed`.

**Manifest validation is not a signal.** All 27 validate; 17 are inert. A control run in
the same probe had `workspace.created` fire while `pane.updated` did not, so the probe was
demonstrably working.

`pane.updated` is the painful one: it is delivered over `events.subscribe` **with the new
cwd**, and it is exactly what a cwd-tracking plugin wants — but it never reaches a hook.

```
1.04 cwd=/Users/bingles/code/tools/herdr-plugins        (initial)
3.20 cwd=/Users/bingles/code/tools/herdr-plugins/docs   <- cd docs
8.19 cwd=/private/tmp                                   <- cd /tmp
```

### `pane.agent_status_changed` fires only for agent panes

The obvious poll candidate, at 0.85/s. But a plain-shell Space was created and `cd` plus a
command run in it:

```
  total new events: 4
  attributed to that pane: 0   -- a plain shell fires nothing on cd or command output
```

All 64 came from panes with a detected agent. It does nothing for the plain-shell case,
and a no-op sync measures **~90 ms**, so 0.85/s is roughly **8% of a core per active
agent**. Not hooked.

### Consequence for the plugin

Five hookable events that *do* fire for plain panes were added — `pane.focused`,
`tab.focused`, `pane.created`, `tab.created`, `tab.closed` — so a `cd` is picked up on the
next navigation. No hookable event exists for `cd` followed by staying put; that would
need the rejected long-lived `events.subscribe` daemon.

## Probing limitations worth recording

- **Sidebar drag was driven through the socket API** (`workspace.move` /
  `workspace.move_block`) rather than by an actual mouse drag, since 0.8.0 exposes no
  reorder CLI. These are the methods the sidebar calls, and both emitted the expected
  events, but the gesture itself was not exercised.
- **`workspace.updated` was observed on one trigger only** (worktree metadata attachment).
  Other triggers may exist and were not enumerated.
- **Probe 12 used a second session's server boot**, not a restart of the live server —
  restarting the live server would have killed the session doing the probing. Startup-hook
  behaviour on *live handoff* (`herdr update --handoff`) was therefore not observed.
- **`code --status` Workspace Stats is unordered** and reports directory basenames, so
  folder order and `name` rendering were confirmed visually rather than programmatically.
- Extension-host churn was measured by **process identity**, which proves reactivation
  happened; the user-visible duration of the resulting re-index was not timed.
