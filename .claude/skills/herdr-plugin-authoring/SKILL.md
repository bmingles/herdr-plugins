---
name: herdr-plugin-authoring
description: "Guides authoring, linking, and debugging Herdr plugins via herdr-plugin.toml, and using the Herdr socket API for agent state and events. Use when the user wants to write or debug a Herdr plugin, add a startup or event hook, register a plugin action or plugin-owned pane, subscribe to events like pane.agent_status_changed, or call socket methods such as pane.report_agent. Keywords: herdr-plugin.toml, herdr plugin link, plugin log list, HERDR_PLUGIN_ROOT, HERDR_SOCKET_PATH, socket API, events.subscribe, report_agent, agent authority."
---

# Authoring Herdr plugins

Herdr's plugin system is real but undocumented in the bundled `herdr` skill. A plugin
is a directory with a `herdr-plugin.toml` manifest and executable commands. Herdr runs
those commands with a rich environment describing the invocation context.

Verified against Herdr 0.8.0 (protocol 19).

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

90 methods; `herdr api schema --json` is the authority (~250KB, so query it rather than
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

## Gotchas

- **`command` is argv, not a shell line.** Use `["sh", "-c", "..."]` if a pipeline is
  needed.
- **Hook stdout goes nowhere visible.** Only `herdr plugin log list` surfaces it.
- **`platforms = []` is an error**, not a wildcard — omit the key instead.
- **Scripts must be executable** and must handle their own errors with informative
  messages; a silent non-zero exit shows up only in the plugin log.
- **Adding a new agent *kind* is not a plugin's job** — it requires a Herdr binary
  update. Detection manifests only patch rules for kinds Herdr already identifies.

## Where the authoritative docs are

```sh
herdr api schema --json      # full socket API JSON Schema
herdr --default-config       # every config.toml key, commented
```

- `https://herdr.dev/llms.txt` — index, links raw MDX pinned per release
- `https://herdr.dev/docs/plugins`, `.../socket-api`, `.../agents` — the useful pages
