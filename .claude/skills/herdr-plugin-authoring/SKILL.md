---
name: herdr-plugin-authoring
description: "Guides authoring, linking, and debugging Herdr plugins via herdr-plugin.toml, and using the Herdr socket API for agent state and events. Use when the user wants to write or debug a Herdr plugin, add a startup or event hook, register a plugin action or plugin-owned pane, subscribe to events like pane.agent_status_changed, or call socket methods such as pane.report_agent. Also covers the surfaces a plugin can render custom text and status icons in: metadata tokens, sidebar rows, the tab bar status area, toasts. Keywords: herdr-plugin.toml, report_metadata, state_labels, tab_bar_right, notification.show, sidebar rows, status indicator, custom token, herdr plugin link, plugin log list, HERDR_PLUGIN_ROOT, HERDR_SOCKET_PATH, socket API, events.subscribe, report_agent, agent authority."
---

# Authoring Herdr plugins

Herdr's plugin system is real but undocumented in the bundled `herdr` skill. A plugin
is a directory with a `herdr-plugin.toml` manifest and executable commands. Herdr runs
those commands with a rich environment describing the invocation context.

Verified against Herdr 0.8.2 (protocol 20). Re-check with `herdr --version`; 0.8.2 added `pane.graphics.*`, `client.window_title.*` and `agent.view.*` over 0.8.0.

## Manifest

`herdr-plugin.toml` at the plugin root. `id`, `name`, and `version` are required;
everything else is optional. Validation errors are precise and name the missing field,
so iterating with `herdr plugin link` is an effective way to learn the schema.

```toml
id = "my-plugin"          # required
name = "My Plugin"        # required
version = "0.1.0"         # required
min_herdr_version = "0.8.0"
description = "..."
platforms = ["macos", "linux"]   # omit for "unknown"; an empty array is an error

[[startup]]     # runs at session/server start
command = ["./daemon.sh"]

[[events]]      # runs on a subscription event
on = "pane.agent_status_changed"
command = ["./on-status.sh"]

[[actions]]     # user/CLI invocable
id = "dump"
title = "Dump context"
contexts = ["global", "pane"]    # global|workspace|tab|pane|selection
command = ["./dump.sh"]

[[panes]]       # plugin-owned UI pane
id = "monitor"
title = "Monitor"
placement = "split"              # overlay|popup|split|tab|zoomed
command = ["./monitor.sh"]

[[link_handlers]]
id = "jira"
title = "Open ticket"
pattern = "..."
action = "..."
```

`command` is an argv array, not a shell string. The cwd is the plugin root, so
`./script.sh` resolves.

## Development loop

```sh
herdr plugin link <path> [--disabled]        # develop against a working tree
herdr plugin list [--json]
herdr plugin action invoke <action_id> [--plugin ID]
herdr plugin log list [--plugin ID] [--limit N]
```

`plugin log list` captures each command's stdout, stderr, exit code, and timing. Hook
output is otherwise invisible, so this is the only way to debug `[[startup]]` and
`[[events]]` commands — check it after every invocation rather than guessing.

Remaining CLI surface:

```sh
herdr plugin install <owner>/<repo>[/subdir] [--ref REF] [--yes]
herdr plugin unlink|enable|disable <plugin_id>
herdr plugin config-dir <plugin_id>
herdr plugin action list
herdr plugin pane open|focus|close
```

## Runtime environment

Verified by dumping `env` from a linked probe plugin. Every command gets:

```
HERDR_PLUGIN_ID, HERDR_PLUGIN_ROOT
HERDR_PLUGIN_CONFIG_DIR   # ~/.config/herdr/plugins/config/<id>
HERDR_PLUGIN_STATE_DIR    # ~/.local/state/herdr/plugins/<id>
HERDR_SOCKET_PATH, HERDR_BIN_PATH, HERDR_ENV=1
HERDR_WORKSPACE_ID, HERDR_TAB_ID, HERDR_PANE_ID
HERDR_PLUGIN_CONTEXT_JSON   # focused pane id/cwd/agent/status, workspace, tab,
                            # invocation_source, correlation_id
```

Plus, conditionally:

```
HERDR_PLUGIN_EVENT, HERDR_PLUGIN_EVENT_JSON   # [[events]] only
HERDR_PLUGIN_ACTION_ID                        # [[actions]] only
```

Example event payload in `HERDR_PLUGIN_EVENT_JSON`:

```json
{"event":"pane_agent_status_changed",
 "data":{"pane_id":"w4:p2","workspace_id":"w4","agent_status":"working","agent":"claude"}}
```

Write persistent files to `$HERDR_PLUGIN_STATE_DIR` and user-editable config to
`$HERDR_PLUGIN_CONFIG_DIR`; do not write into `$HERDR_PLUGIN_ROOT`.

**Prefer `$HERDR_BIN_PATH` over raw socket calls.** The CLI is portable — Windows uses
a different local socket form. Reach for the socket only when a plugin needs a
long-lived subscription or a method the CLI does not expose.

## Socket API

Newline-delimited JSON over `$HERDR_SOCKET_PATH`, one request object per line:

```json
{"id": "req_1", "method": "pane.report_agent", "params": {}}
```

117 methods; `herdr api schema --json` is the authority (~250KB, so query it rather than
reading it whole). `HERDR_SOCKET_PATH` selects **which server** the CLI talks to, which
is how to target a non-default session.

27 event subscription types via `events.subscribe`. `events.wait` blocks on a single
match and supports 19 match forms. Agent-relevant events:
`pane.agent_status_changed`, `pane.agent_detected`, `pane.exited`,
`pane.output_matched`.

Agent status enum: `idle | working | blocked | done | unknown`. `done` is `idle` whose
tab has not been seen in the focused UI; CLI reads do not mark a tab seen.

### Agent state reporting — read this before calling it

| Method | Purpose |
|---|---|
| `pane.report_agent` | identity **and** state; `agent` may be an arbitrary label |
| `pane.report_agent_session` | native session refs only — **cannot establish identity** |
| `pane.report_metadata` | display-only (title, `display_agent`, `state_labels`, tokens) |
| `pane.clear_agent_authority` | release a reporting source |
| `pane.release_agent` | drop the agent from the pane |

**Reporting state makes that source a full lifecycle authority, which disables screen
detection rules for the pane** (surfaced as `screen_detection_skip_reason`). Observed
consequence: after reporting, `herdr agent explain` computed `idle` from the screen
while `agent_status` stayed `working`, and `clear_agent_authority` did **not** restore
screen-driven state in testing. Whether the authority ever decays is unknown — it was
only observed over a few minutes.

Practical rule: report state only if the plugin owns the agent's full lifecycle and
will keep reporting. To make Herdr recognize an agent hidden behind a wrapper
(container, sandbox) while leaving Herdr's own rules in charge, use the `HERDR_AGENT`
environment variable instead — see `herdr-devcontainer-agent-running`.

Agents that report through lifecycle hooks (Pi, OMP, OpenCode, Kilo, Kimi, MastraCode)
work this way by design. Claude Code, Codex, Cursor and most others are
screen-manifest agents whose integrations supply only session identity.

## Showing custom text in the UI

Five surfaces, from most to least useful for "a plugin-owned status indicator". None of
them are writable from the manifest — a plugin cannot edit `config.toml`, so anything
needing a config stanza is a README instruction to the user.

| Surface | Scope | Needs user config? |
|---|---|---|
| `$name` metadata tokens in sidebar rows | per pane / per Space | yes — the row layout |
| `title`, `display_agent`, `state_labels` | per pane | no |
| `ui.tab_bar_right` `command` entry | global (one per server) | yes |
| `notification.show` | transient toast | yes — delivery defaults to `off` |
| `[[panes]]`, `client.window_title.set`, `pane.graphics.*` | pane / window | no |

### Metadata tokens

`pane.report_metadata` / `workspace.report_metadata` push arbitrary named strings that
render as `$name` in sidebar rows. Display-only: unlike `pane.report_agent` this does
**not** take lifecycle authority, so Herdr's own detection stays in charge.

```sh
"$HERDR_BIN_PATH" pane report-metadata w1:p1 --source "plugin:my-plugin" \
  --token build="OK green" --token model=opus --ttl-ms 60000
"$HERDR_BIN_PATH" workspace report-metadata w1 --source "plugin:my-plugin" \
  --token jj_status="2 changes"
```

The user must then place the token, because tokens are values and styling is config:

```toml
[ui.sidebar.agents]
rows = [["state_icon", "workspace", "tab", { token = "$build", fg = "#a6e3a1" }], ["agent"]]

[ui.sidebar.spaces]
rows = [["state_icon", "workspace"], ["branch", "$jj_status"]]
```

Limits: names `^[A-Za-z0-9_-]{1,32}$`, values normalized (whitespace trimmed, control
characters stripped) and capped at **80 characters**, ≤16 keys per report, ≤32 retained
per resource, ≤32 distinct `source` slots per resource for its lifetime. A string sets a
key, JSON `null` clears it, omitted keys are untouched. `ttl_ms` is 1–86400000 and
applies per key updated by that call; `seq` makes out-of-order reports safe. Tokens are
**not restored after a server restart**, and `workspace.metadata_updated` reaches API
subscribers but **does not invoke plugin event hooks**.

Row rendering is worth knowing: missing values and their separators disappear, and a row
with no values at all disappears. So for **state-dependent colour** — which a reporter
otherwise cannot control, since `fg` is static per occurrence — report a different token
name per state and let the user style each:

```toml
[{ token = "$build_ok", fg = "#a6e3a1" }, { token = "$build_fail", fg = "#f38ba8" }]
```

Set one, `--clear-token` the other; exactly one ever renders.

`--title`, `--display-agent` and `--state-label <status>=<text>` need no row config —
they override built-in rendering directly. `state_labels` keys must be `idle`,
`working`, `blocked`, `done`, or `unknown`, and only relabel: semantic state still drives
the dot colour, waits, notifications and rollups. `--agent` and `--applies-to-source`
guard presentation fields against the wrong occupant; they do not guard token patches.

### You cannot reuse `state_icon`

`state_icon` renders the **semantic agent state of a real agent record**. There is no way
to ask Herdr to draw one for a non-agent thing. The only route is reporting a synthetic
agent with `pane.report_agent`, which makes the plugin a full lifecycle authority and
disables screen detection for that pane — a bad trade for a glyph, and actively
self-defeating for any plugin that also *reads* `agent_status`. Pick your own glyph
instead; emoji carry their own colour, which matters where styling is unavailable.

`ui.status_indicators = "symbols"` swaps the dots for distinct static glyphs, but that
glyph set is not documented — do not try to match it from memory.

### The tab bar status area — the only global surface

Pane and workspace tokens are per-resource. For one fact per Herdr server, use a
`command` entry, which Herdr re-runs on the server on an interval and renders:

```toml
[ui]
tab_bar_right = [
  { type = "zoom" },
  { type = "datetime", format = "%H:%M" },
  { type = "command", command = "~/bin/my-status", interval_seconds = 5, timeout_seconds = 2 },
]
tab_bar_right_separator = " . "
```

Herdr renders the **last line of successful output** and clears the entry on failure,
empty output, or timeout — so "print nothing" is a first-class way to say "nothing to
show", and a broken script degrades to silence rather than garbage. `interval_seconds`
is 1–31536000, `timeout_seconds` 1–3600, runs never overlap and never block rendering.
Commands get the same context as custom command keybindings — **including
`HERDR_SOCKET_PATH`**, which is how a script finds the right session's state — but not
the `HERDR_PLUGIN_*` variables, so resolve plugin state dirs by their default paths.
Executed with `/bin/sh -lc` on Linux/macOS and `cmd.exe /d /c` on Windows. ANSI colour is
undocumented; assume plain text. Every run is a process spawn, so keep the script to file
reads — a Python one costs ~30 ms, which is ~0.6% of a core at a 5 s interval.

### Toasts, panes, window title, graphics

- `notification.show` — `{title, body?, position?, sound?}`, sound is
  `none|done|request`. CLI: `herdr notification show <TITLE> --body ... --sound none`.
  **`ui.toast.delivery` defaults to `off`**, so this is invisible until the user opts in.
- `[[panes]]` in the manifest — a plugin-owned pane, `placement` one of
  `overlay|popup|split|tab|zoomed`, driven by `herdr plugin pane open|focus|close`. Your
  command owns a real terminal, so draw anything.
- `client.window_title.set` / `.clear` — overrides `ui.window_title` for the foreground
  client until cleared.
- `pane.graphics.set` / `.stream` / `.clear` — real PNG/RGB/RGBA images composited into a
  pane, up to 16 layers with `layer_id` and `z_index`. Overkill for a status glyph.
- `pane.rename` / `tab.rename` / `workspace.rename` — real persistent labels, not
  display-only. Blunt, but visible everywhere with no config.

## Gotchas

- **`command` is argv, not a shell line.** Use `["sh", "-c", "..."]` if a pipeline is
  needed.
- **Hook stdout goes nowhere visible.** Only `herdr plugin log list` surfaces it.
- **`platforms = []` is an error**, not a wildcard — omit the key instead.
- **Scripts must be executable** and must handle their own errors with informative
  messages; a silent non-zero exit shows up only in the plugin log.
- **A `$name` token is invisible until the user adds it to a row.** Reporting one
  is half the job; the other half is a README stanza for `ui.sidebar.*.rows`.
- **A plugin cannot write `config.toml`.** Every config-dependent UI surface is an
  instruction to the user, never something `herdr plugin install` sets up.
- **Adding a new agent *kind* is not a plugin's job** — it requires a Herdr binary
  update. Detection manifests only patch rules for kinds Herdr already identifies.

## Where the authoritative docs are

```sh
herdr api schema --json      # full socket API JSON Schema
herdr --default-config       # every config.toml key, commented
```

- `https://herdr.dev/llms.txt` — index, links raw MDX pinned per release
- `https://herdr.dev/docs/plugins`, `.../socket-api`, `.../agents` — the useful pages
