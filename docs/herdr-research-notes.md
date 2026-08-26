# Herdr research notes

Findings from investigating how to surface devcontainer agent status in the host
Herdr instance. Much of this is not in the bundled `herdr` skill file, and some was
established by experiment rather than documentation.

**Practical outcome:** the original goal needs no plugin — see
[devcontainer-agent-status.md](./devcontainer-agent-status.md). These notes exist so
the reasoning and the dead ends don't have to be rediscovered.

The plugin system and socket API sections are distilled for agent use as the
`herdr-plugin-authoring` skill in `.claude/skills/`; the detection and dead-end
sections as `herdr-devcontainer-agent-running`. Keep them in sync.

Verified against Herdr **0.8.0 (protocol 19)** on macOS with Docker Desktop, unless
noted. Latest at time of writing was 0.8.2 (protocol 20).

---

## Where the real documentation is

The bundled skill (`herdr --skill`) covers pane/agent control but **not** the plugin
system or detection internals. The authoritative docs are:

```
https://herdr.dev/llms.txt        # index; links raw MDX pinned to a release tag
https://herdr.dev/agent-guide.md  # setup/troubleshooting oriented
herdr api schema --json           # full socket API JSON Schema (~250KB)
herdr --default-config            # every config.toml key with comments
```

Individual pages are at `https://herdr.dev/docs/<name>` (HTML) or as raw MDX via the
index. The most useful are `agents`, `socket-api`, and `plugins`.

Reaching for the docs earlier would have saved most of this investigation — the
`HERDR_AGENT` answer is in the `agents` page.

---

## Agent detection internals

### Identity

Herdr watches each pane's **foreground process group** and matches an executable
name against known agent kinds. Purely name-based: a shell script named `claude`
whose processes are `sh`/`sleep` is identified as Claude.

Identity lives exactly as long as that process. When it exits and the shell returns,
identity clears (`agent=None`).

`HERDR_AGENT=<kind>` on a foreground process overrides this, for cases where a
wrapper hides the real process (containers, sandboxes like `fence`/`nono`). It is an
**assertion**, not a hint — nothing behind the wrapper is inspected. Herdr ignores
a few trivial utilities (`sleep`, `cat`) but honours substantial ones (`docker`,
`python3`).

Adding a genuinely new agent *kind* requires a Herdr binary update; manifests only
patch rules for agents it already identifies.

### State

Once identity exists, state comes from TOML manifests evaluated against the pane's
live bottom-buffer snapshot and OSC title:

```
~/.local/state/herdr/agent-detection/remote/<agent>.toml   # shipped/updated by Herdr
~/.config/herdr/agent-detection/<agent>.toml               # local override
```

`claude.toml` had 16 rules. Regions seen: `osc_title`,
`bottom_non_empty_lines(N)`, `last_non_empty_above_prompt_box`,
`after_last_horizontal_rule`, `whole_recent`, `prompt_box_body`. Matchers:
`regex`, `line_regex`, `contains`, `all`/`any`/`not`, plus `priority` and
`visible_working`.

Because these read *terminal output*, they work through `docker exec` unchanged.
This is the key insight: container agent status was never a transport problem.

Some agents (Pi, OMP, OpenCode, Kilo, Kimi, MastraCode) report state via lifecycle
hooks instead, which become authoritative and **disable** screen rules. Claude Code,
Codex, Cursor and most others are screen-manifest agents; their integrations supply
only session identity.

### Debugging detection

```sh
herdr agent explain <pane>                              # live verdict from the server
herdr agent explain <pane> --verbose                    # per-rule matchers + evidence
herdr agent explain --file snap.txt --agent claude      # offline, real rule engine
herdr pane read <pane> --source detection               # the snapshot rules see
```

`--verbose` prints every evaluated rule with `✓`/`✗`, its matchers, and a preview of
the region it examined. This is by far the fastest way to diagnose "why is it idle".

Notable states:
- `default_known_agent_idle_fallback` — identity known, **no rule matched**. Idle is
  a fallback, not a positive signal.
- `skipped_update_reason: matched_rule:<id>` — e.g. Claude's `/model` picker reports
  `unknown` deliberately; menus are not treated as blocking prompts.
- `screen_detection_skip_reason` — a full lifecycle hook authority is in charge.

---

## Plugin system

Real and undocumented in the skill file. Manifest: **`herdr-plugin.toml`**.

```toml
id = "my-plugin"          # required
name = "My Plugin"        # required
version = "0.1.0"         # required
min_herdr_version = "0.8.0"
description = "..."
platforms = ["macos", "linux"]   # omit for "unknown"; empty array is an error

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

`command` is an argv array; cwd is the plugin root, so `./script.sh` works.
Validation errors are precise and name the missing field — iterating with
`herdr plugin link` is an effective way to learn the schema.

### CLI

```sh
herdr plugin install <owner>/<repo>[/subdir] [--ref REF] [--yes]
herdr plugin link <path> [--disabled]
herdr plugin list [--json]
herdr plugin unlink|enable|disable <plugin_id>
herdr plugin config-dir <plugin_id>
herdr plugin action list|invoke <action_id> [--plugin ID]
herdr plugin log list [--plugin ID] [--limit N]     # stdout/stderr/exit codes
herdr plugin pane open|focus|close
```

`plugin log list` captures each command's stdout, stderr, exit code and timing —
the way to debug hooks, since their output is otherwise invisible.

### Runtime environment

Verified by dumping `env` from a linked probe plugin:

```
HERDR_PLUGIN_ID, HERDR_PLUGIN_ROOT
HERDR_PLUGIN_CONFIG_DIR   # ~/.config/herdr/plugins/config/<id>
HERDR_PLUGIN_STATE_DIR    # ~/.local/state/herdr/plugins/<id>
HERDR_SOCKET_PATH, HERDR_BIN_PATH, HERDR_ENV=1
HERDR_WORKSPACE_ID, HERDR_TAB_ID, HERDR_PANE_ID
HERDR_PLUGIN_CONTEXT_JSON          # focused pane id/cwd/agent/status, workspace, tab,
                                   # invocation_source, correlation_id
HERDR_PLUGIN_EVENT                 # events only
HERDR_PLUGIN_EVENT_JSON            # events only
HERDR_PLUGIN_ACTION_ID             # actions only
```

Example event payload:

```json
{"event":"pane_agent_status_changed",
 "data":{"pane_id":"w4:p2","workspace_id":"w4","agent_status":"working","agent":"claude"}}
```

Prefer invoking `$HERDR_BIN_PATH` (the CLI) over raw sockets for portability;
Windows uses a different local socket form.

---

## Socket API

Newline-delimited JSON over `$HERDR_SOCKET_PATH`:

```json
{"id": "req_1", "method": "pane.report_agent", "params": {...}}
```

90 methods. `herdr api schema --json` is the authority. **`HERDR_SOCKET_PATH`
selects which server the CLI talks to** — useful for targeting a non-default session.

27 event subscription types via `events.subscribe`; `events.wait` blocks on a single
match (19 match forms). Agent-relevant: `pane.agent_status_changed`,
`pane.agent_detected`, `pane.exited`, `pane.output_matched`.

Agent status enum: `idle | working | blocked | done | unknown`. `done` is idle whose
tab has not been seen in the focused UI; CLI reads do not mark a tab seen.

### State reporting — and its authority semantics

| Method | Purpose |
|---|---|
| `pane.report_agent` | identity **and** state; `agent` may be an arbitrary label |
| `pane.report_agent_session` | native session refs only — **cannot establish identity** |
| `pane.report_metadata` | display-only (title, `display_agent`, `state_labels`, tokens) |
| `pane.clear_agent_authority` | release a reporting source |
| `pane.release_agent` | drop the agent from the pane |

Reporting state makes that source a **full lifecycle authority**, which disables
screen rules for the pane (surfaced as `screen_detection_skip_reason`). Observed
consequence: after reporting, `agent explain` computed `idle` from the screen while
`agent_status` stayed `working`, and `clear_agent_authority` did **not** restore
screen-driven state in testing.

This is why `HERDR_AGENT` is the right tool for containers and `pane.report_agent`
is not: the former keeps Herdr's own rules in charge, the latter takes them out.

---

## Dead ends

Recorded with evidence so they aren't retried.

**Bind-mounting `herdr.sock` into a container — fails on Docker Desktop/macOS.**
The socket node passes through virtiofs but the listener does not:

```
exists: True  is_sock: True
CONNECT FAILED: ConnectionRefusedError [Errno 111] Connection refused
```

Would likely work on native Linux Docker. A bind-mounted **directory** works fine
(including UID mapping), so a spool-file transport is viable — but unnecessary,
given detection needs no transport.

**Herdr server inside the container — works, but wrong shape.** A Linux binary runs
fine in a devcontainer and the full agent stack works in-container. The host can
even reach it by proxying a Unix socket over `docker exec -i` (verified: host CLI
listed a container agent). But that server owns its own agents, so you get a
*second* Herdr UI rather than container agents in your host session.

**`herdr --remote` — SSH only.** It is an `ssh -T` stdio bridge running a hidden
`herdr remote-client-bridge` subcommand on the far side (client-initiated; silent
until spoken to). It probes with `test -x <bin> && <bin> --version && <bin> status
client --json`, auto-installs a matching binary from herdr.dev, and honours
`HERDR_REMOTE_BINARY` and `[remote] manage_ssh_config`. The transport is hardcoded
to `ssh`, so `docker exec` cannot substitute — a devcontainer would need sshd.
Client and server **protocol versions must match exactly** (a 0.8.0 client against a
0.8.2 server returns `protocol_mismatch`).

**Polling `docker ps` from the host** — considered, rejected: can only infer
working/idle from process state and could never detect `blocked`.

**Declaring identity via `pane.report_agent` and re-reporting state** — works, but
takes screen rules out of play and requires a poll-and-re-report loop. Superseded by
`HERDR_AGENT`.

---

## Open questions

- Does reported lifecycle authority ever **decay**? Only observed over a few
  minutes. If it expires, some hybrid designs become viable again.
- Claude's approval dialog matched the low-priority fallback rule
  `legacy_no_prompt_blocker` (priority 300) rather than a primary blocked rule. It
  worked, but it's a weak signal that could regress.
- **Native session identity for container agents is unsolved.** Integration hooks
  need the host socket, which containers can't reach on Docker Desktop, so
  `[session] resume_agents_on_restore` won't cover them.
- `shift+tab` did not survive `docker exec -it` key encoding. Unknown how many other
  chords are affected.
- Whether `--remote` merges a remote session into the host view or replaces it —
  architecture strongly implies replaces, but this was never confirmed in the TUI.

---

## Test environment

Herdr 0.8.0 (protocol 19) client and server, macOS arm64, Docker Desktop
(`desktop-linux`, linux/arm64 containers), VS Code devcontainer running Claude Code
2.1.245 as user `vscode` at `/workspaces/herdr-plugins`. Release binaries verified
against the sha256 values in `https://herdr.dev/latest.json`.
