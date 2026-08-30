---
name: herdr-devcontainer-agent-running
description: "Surfaces coding-agent status from a devcontainer or other process wrapper in a host Herdr session using the HERDR_AGENT prefix. Use when an agent launched through docker exec shows no agent or no status in a Herdr pane, when launching Claude Code or another agent inside a devcontainer from a Herdr pane, or when diagnosing why a pane reports idle, unknown, or agent=None. Keywords: HERDR_AGENT, devcontainer, docker exec, agent not detected, herdr agent explain, agent detection manifest, idle fallback."
---

# Running devcontainer agents in Herdr

Herdr identifies an agent by the executable **name** in a pane's foreground process
group. An agent inside a devcontainer lives in another PID namespace, so the host
only sees `docker` — no agent, no status.

The fix is one environment variable on the **host** side of the launch command. No
plugin, no socket tunnel, no Herdr server in the container.

```sh
HERDR_AGENT=claude docker exec -it <container> <agent-command>
```

Everything downstream — `idle`, `working`, `blocked`, `done`, tab/workspace rollups,
notifications — then works as it does for a local agent, because Herdr's state rules
read terminal output, and terminal output crosses the container boundary unchanged.
Container agent status was never a transport problem.

**This is a different topology from Herdr running *inside* a devcontainer.** This
skill covers a **host** Herdr watching an agent launched via `docker exec` — the
`HERDR_AGENT` prefix above is a host-side concern. When Herdr's client, server, and
agents all run **inside** the container instead, the worktree-path rule and
bind-mount guard for `herdr worktree create` live in `devc-dev`'s
[`herdr-devcontainer-worktrees`](https://github.com/bmingles/devc-dev/blob/main/.claude/skills/herdr-devcontainer-worktrees/SKILL.md)
skill — a different concern, not a duplicate of this one.

## Launch an agent

Find the container, then launch from a Herdr pane with the prefix and the agent's
**absolute path**:

```sh
docker ps --format '{{.Names}}'

HERDR_AGENT=claude docker exec -it \
  -u vscode \
  -w /workspaces/<project> \
  <container> \
  /home/vscode/.local/bin/claude
```

Status populates within a few seconds.

The absolute path is required: `docker exec` does not run a login shell, so
`~/.local/bin` is off `PATH` and a bare `claude` fails with `executable file not
found in $PATH`. If the path is unknown, force a login shell instead — keep `exec`
so the agent replaces the shell and stays the foreground process:

```sh
HERDR_AGENT=claude docker exec -it -u vscode -w /workspaces/<project> \
  <container> sh -lc 'exec claude'
```

Any agent kind Herdr already knows works (`claude`, `codex`, `droid`, `opencode`, …).
`HERDR_AGENT` selects an **existing** manifest; it cannot introduce a new agent type.
A new kind requires a Herdr binary update.

Forgetting the prefix is the main failure mode, so wrap it:

```sh
# ~/.zshrc or ~/.bashrc — host side
dcagent() {
  local container="$1" agent="${2:-claude}" workdir="${3:-/workspaces}"
  HERDR_AGENT="$agent" docker exec -it -u vscode -w "$workdir" \
    "$container" sh -lc "exec $agent"
}
```

## Verify and diagnose

```sh
herdr agent list                  # empty => the prefix is not taking effect
herdr agent explain <pane-id>     # which rule matched, and on what evidence
herdr agent explain <pane-id> --verbose   # every rule with ✓/✗, matchers, region preview
herdr pane read <pane-id> --source detection   # the snapshot the rules see
```

`agent explain` always re-evaluates the current snapshot, so it is the fastest way to
separate "not detected" from "detected but state looks wrong":

```
agent: claude
state: idle
manifest: remote:.../claude.toml 2026.08.21.1
rule: live_prompt_box (region=prompt_box_body priority=950)
evidence: "❯\n"
```

Reason codes worth recognizing:

| Code | Meaning |
|---|---|
| `default_known_agent_idle_fallback` | Identity known, **no rule matched**. Idle is a fallback, not a positive signal. |
| `skipped_update_reason: matched_rule:<id>` | A rule deliberately declined to classify — e.g. Claude's `/model` picker reports `unknown`. |
| `screen_detection_skip_reason` | A lifecycle-hook authority is in charge; screen rules are disabled. |

`explain` also works offline against a captured snapshot, using the real rule engine:

```sh
herdr agent explain --file snap.txt --agent claude
```

## Trust `working` and `blocked`; distrust `idle`

`HERDR_AGENT` **asserts** identity — Herdr never inspects the container. It reads the
variable off the host-visible `docker exec` process and takes it at face value.

Two consequences, both verified:

**The agent appears before the real agent starts.** `HERDR_AGENT=claude docker exec
-it <container> sleep 45`, with no Claude anywhere, reports `agent=claude`,
`status=idle`. Herdr skips a few trivial host utilities (`sleep`, `cat` run directly
on the host) but honours anything substantial — `docker` and `python3` are both
accepted.

**`idle` is the fallback state, not a healthy one.** It covers all of:

- the agent is genuinely at its prompt, ready for input
- the container command has not finished starting the agent yet
- the agent failed to launch, or exited into a shell
- the agent is stuck on something with no rule — an auth prompt reads as `idle`
  despite needing attention
- the prefix was used on a command that is not an agent at all

| State | Meaning |
|---|---|
| `working` | Mid-turn. Usually driven by the OSC title spinner. Trustworthy. |
| `blocked` | An approval, permission, or question dialog matched. Trustworthy. |
| `idle` | Nothing matched. Confirm with `agent explain`. |
| `done` | The same idle state, for work that finished in an unfocused tab. Focusing clears it. |
| `unknown` | An agent is present but the screen is unclassifiable. Does **not** mean finished. |

`working` and `blocked` require a manifest rule to match observed output, which is why
they are the only positive signals.

### The lifetime asymmetry

Identity lives exactly as long as the process it attached to — but local and container
panes attach to **different** processes:

| | Identity rides | Consequence |
|---|---|---|
| Local | the agent process itself | Cannot exist before the agent starts or after it exits. |
| `docker exec` | the **wrapper** process | Starts before the agent inside it, and survives whatever happens to it. |

Through `docker exec` you are watching the lifetime of the *container command*, not of
the *agent*. This is the root cause of every caveat above.

## Troubleshooting

**`agent=None`.** The variable must be a prefix to `docker exec` on the **host**.
Setting it inside the container — Dockerfile, `containerEnv`, `remoteEnv`, shell
profile — has no effect, because Herdr reads it from the host-visible wrapper process.
Do not export it globally in the host shell either, or every process inherits the hint.

**`executable file not found in $PATH`.** Use the absolute path or the `sh -lc` form
above.

**Key chords don't reach the agent.** `shift+tab` does not survive `docker exec -it`
key encoding, so it will not cycle Claude Code's permission mode. Use the equivalent
CLI flag — launch with `--permission-mode default`. Plain keys and
`herdr agent prompt` work normally. How many other chords are affected is unknown.

**Status seems stuck.** Screen detection re-evaluates on pane output; an agent that has
emitted nothing keeps its last state. Confirm with `agent explain`.

## Limitations

**Native session identity does not work.** `herdr integration install claude` reports
session references over Herdr's Unix socket, which a containerised agent cannot reach.
Agent *state* is fully available, but Herdr cannot resume container agents into their
native conversation sessions after a server restart
(`[session] resume_agents_on_restore`).

**`blocked` is a screen-shape match.** A Claude Code write-approval dialog matched a
low-priority fallback rule (`legacy_no_prompt_blocker`, priority 300) rather than a
primary blocked rule. It works, but could regress if the dialog shape changes. A new
prompt shape reads as `idle` until Herdr's manifests learn it.

**Menus report `unknown` by design.** Menus are not treated as blocking prompts.

## Do not retry these

Each was tested and rejected; the evidence is here so it is not rediscovered.

- **Bind-mounting `herdr.sock` into a container.** Fails on Docker Desktop for macOS:
  the socket node passes through virtiofs (`is_sock: True`) but connections are
  refused — the listener does not cross the VM boundary. Likely works on native Linux
  Docker. A bind-mounted **directory** works fine, so a spool-file transport is
  viable — but unnecessary, since detection needs no transport.
- **A Herdr server inside the container.** Works, and the host can even reach it by
  proxying a Unix socket over `docker exec -i`. But that server owns its own agents,
  so the result is a *second* Herdr UI, not container agents in the host session.
- **`herdr --remote`.** An `ssh -T` stdio bridge with the transport hardcoded to
  `ssh`; `docker exec` cannot substitute, and a devcontainer would need sshd. Client
  and server protocol versions must match exactly.
- **Polling `docker ps` from the host.** Can only infer working/idle from process
  state, and could never detect `blocked`.
- **Declaring identity with `pane.report_agent` and re-reporting state.** Works, but
  makes that source a full lifecycle authority, which **disables** screen rules for the
  pane, and needs a poll-and-re-report loop. `HERDR_AGENT` is right precisely because it
  keeps Herdr's own rules in charge. See `herdr-plugin-authoring` for the authority
  semantics.

## Where the authoritative docs are

The bundled `herdr` skill covers pane/agent control but not detection internals.

```sh
herdr api schema --json     # full socket API JSON Schema
herdr --default-config      # every config.toml key, commented
```

- `https://herdr.dev/llms.txt` — index, links raw MDX pinned per release
- `https://herdr.dev/docs/agents` — detection, status authority, `HERDR_AGENT`
- `https://herdr.dev/agent-guide.md` — setup and troubleshooting oriented

Detection manifests on disk:

```
~/.local/state/herdr/agent-detection/remote/<agent>.toml   # shipped/updated by Herdr
~/.config/herdr/agent-detection/<agent>.toml               # local override
```

Rules use regions (`osc_title`, `bottom_non_empty_lines(N)`, `prompt_box_body`,
`last_non_empty_above_prompt_box`, `after_last_horizontal_rule`, `whole_recent`) and
matchers (`regex`, `line_regex`, `contains`, `all`/`any`/`not`, plus `priority` and
`visible_working`). Claude's manifest had 16 rules.

## Verification provenance

The launch method, all four states, and `herdr agent prompt` / `send-keys` control were
verified against Claude Code 2.1.245 in a VS Code devcontainer on macOS with Docker
Desktop, using Herdr 0.8.0 (protocol 19). The control case — the same command without
`HERDR_AGENT` — produced no detection. Assertion behaviour was verified by running the
prefix against non-agent commands. Name-based local identity was verified with a shell
script named `claude` (detected as Claude despite its processes being `sh`/`sleep`), and
identity teardown by returning the pane to its shell (`agent=None`). The `dcagent`
helper and the `sh -lc 'exec claude'` form are adaptations of the verified command
rather than the exact tested invocation.
