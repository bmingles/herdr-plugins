# Showing devcontainer agent status in host Herdr

Herdr normally recognises a coding agent by inspecting the foreground process in a
pane. An agent running inside a devcontainer lives in a different PID namespace, so
the host only sees `docker` — the pane shows no agent and no status.

The fix is one environment variable on the **host** side of the launch command. No
plugin, no socket tunnel, no Herdr server inside the container.

```sh
HERDR_AGENT=claude docker exec -it <container> <agent-command>
```

`HERDR_AGENT` tells Herdr which agent screen manifest to apply to that foreground
process. Everything else — `idle`, `working`, `blocked`, `done`, tab/workspace
rollups, notifications — then works exactly as it does for a local agent, because
Herdr's detection rules read terminal output, and terminal output crosses the
container boundary unchanged.

## Quick start

Find the container for your project:

```sh
docker ps --format '{{.Names}}'
```

From a Herdr pane, launch the agent with the prefix:

```sh
HERDR_AGENT=claude docker exec -it \
  -u vscode \
  -w /workspaces/<project> \
  <container> \
  /home/vscode/.local/bin/claude
```

The pane's agent status should populate within a few seconds.

### Why the full path

`docker exec` does not run a login shell, so `~/.local/bin` is not on `PATH` and a
bare `claude` fails with `executable file not found in $PATH`. Either give the
absolute path as above, or force a login shell:

```sh
HERDR_AGENT=claude docker exec -it -u vscode -w /workspaces/<project> \
  <container> sh -lc 'exec claude'
```

Keep `exec` so the agent replaces the shell and stays the foreground process.

## Verify it worked

```sh
herdr agent list
herdr agent explain <pane-id>
```

`explain` shows which detection rule matched and the evidence it used — the fastest
way to tell "not detected" from "detected but state looks wrong":

```
agent: claude
state: idle
manifest: remote:.../claude.toml 2026.08.21.1
rule: live_prompt_box (region=prompt_box_body priority=950)
evidence: "❯\n"
```

If `herdr agent list` shows nothing, the prefix is not taking effect — see
Troubleshooting.

## Making it convenient

A shell function keeps the prefix from being forgotten, which is the main failure
mode:

```sh
# ~/.zshrc or ~/.bashrc
dcagent() {
  local container="$1" agent="${2:-claude}" workdir="${3:-/workspaces}"
  HERDR_AGENT="$agent" docker exec -it -u vscode -w "$workdir" \
    "$container" sh -lc "exec $agent"
}
```

```sh
dcagent devc-myproject-1a2b3c claude /workspaces/myproject
```

Adapt the agent name to any kind Herdr knows (`claude`, `codex`, `droid`,
`opencode`, …). `HERDR_AGENT` selects an **existing** manifest; it cannot introduce
a new agent type.

## How detection works

Understanding this explains every quirk below, so it's worth the two minutes.

Herdr continuously watches each pane's **foreground process group**, and answers two
separate questions about it.

**1. Identity — which agent is this?** Two paths:

- *Local agent:* Herdr matches an executable **name** in the foreground group
  against the agent kinds it knows. A locally-run `claude` is identified purely
  because `claude` is in that group; no environment variable is involved.
- *Through a wrapper:* the foreground name is `docker`, which matches nothing, so
  identity fails. `HERDR_AGENT` supplies the name the process tree cannot reveal.

Identity is name-based and nothing more. A shell script merely *named* `claude`,
whose real processes are `sh` and `sleep`, is identified as Claude.

**2. State — what is it doing?** Once identity exists, Herdr evaluates its TOML
detection manifests against the pane's live bottom-buffer screen snapshot and OSC
title. This is why containers work at all: terminal output crosses the `docker exec`
boundary unchanged, so the same rules apply with no transport, no socket, and no
Herdr server inside the container.

(Some agents — not Claude Code — instead report state through lifecycle hooks, which
then become authoritative and disable screen rules. See `herdr.dev/docs/agents`.)

### The lifetime asymmetry — why local and container panes differ

Identity lives exactly as long as the process it attached to. Both cases are
process-scoped; they just attach to **different** processes:

| | Identity rides | Consequence |
|---|---|---|
| Local | the agent process itself | Cannot exist before the agent starts or after it exits. Boundaries match the agent's life exactly. |
| `docker exec` | the **wrapper** process | Starts before the agent inside it, and survives whatever happens to it. |

Verified locally: with a process named `claude` running, `agent='claude'`; after
Ctrl-C returns the pane to `bash`, `agent=None`. Nothing tears identity down
explicitly — the recognised name simply leaves the foreground group.

This is the root cause of the container caveats: through `docker exec` you are
watching the lifetime of the *container command*, not the lifetime of the *agent*.
Locally, "agent present" implies the agent process exists. Through docker, it only
implies `docker exec` is running.

## What the states mean

| State | Meaning |
|---|---|
| `working` | Agent is mid-turn. Usually driven by the OSC title spinner. |
| `idle` | No detection rule matched. See the warning below — this is weaker than it looks. |
| `done` | Same underlying idle state, for work that finished in a tab you haven't looked at. Focusing the tab clears it. |
| `blocked` | Herdr recognised an approval, permission, or question dialog. |
| `unknown` | An agent is present but Herdr cannot classify the current screen. Does **not** mean finished. |

### `HERDR_AGENT` asserts identity — it does not detect it

Because identity rides the wrapper, Herdr never inspects the container. It reads the
environment of the `docker exec` process and takes `HERDR_AGENT` at face value.

**The agent therefore appears before the real agent starts.** Verified: launching
`HERDR_AGENT=claude docker exec -it <container> sleep 45` — with no Claude involved
anywhere — reports `agent=claude`, `status=idle`.

Herdr skips a few trivial utilities (`sleep` and `cat` run directly on the host are
ignored), but any substantial process is taken at face value: both `docker` and
`python3` are accepted.

**And `idle` is the fallback state, not a healthy one.** When identity is asserted
but no screen rule matches, Herdr reports `idle` via
`default_known_agent_idle_fallback`. That state therefore covers all of:

- the agent is genuinely at its prompt, ready for input
- the container command hasn't finished starting the agent yet
- the agent failed to launch, or exited into a shell
- the agent is stuck on something Herdr has no rule for — an auth prompt, for
  instance, which reads as `idle` despite needing your attention
- the prefix was used on a command that isn't an agent at all

`working` and `blocked` are trustworthy, because both require a manifest rule to
actually match observed output. Treat `idle` as "nothing matched" and confirm with
`herdr agent explain <pane>`, which names the matched rule or reports the fallback
reason.

## Troubleshooting

**Agent not detected (`agent=None`).** The variable must be set on the host side of
the command, as a prefix to `docker exec`. Setting it inside the container — in the
Dockerfile, `containerEnv`, `remoteEnv`, or the container's shell profile — has no
effect, because Herdr reads it from the host-visible wrapper process. Do not export
it globally in your host shell either, or every process inherits the hint.

**`executable file not found in $PATH`.** See "Why the full path" above.

**Key chords don't reach the agent.** `shift+tab` did not cycle Claude Code's
permission mode through `docker exec -it`; the chord doesn't survive the encoding.
Use an equivalent CLI flag instead — for Claude Code, launch with
`--permission-mode default`. Plain keys and `herdr agent prompt` work normally.

**Status seems stuck.** Screen detection re-evaluates on pane output. An agent that
has emitted nothing for a while keeps its last state. Confirm with `agent explain`,
which always evaluates the current snapshot.

## Limitations

**Native session identity does not work.** Integrations like
`herdr integration install claude` report session references over Herdr's Unix
socket, and a containerised agent cannot reach the host socket. On Docker Desktop
for macOS, bind-mounting `herdr.sock` into a container does not work — the socket
node appears but connections are refused, because the listener doesn't cross the
VM boundary. Consequence: agent *state* is fully available, but Herdr cannot resume
container agents into their native conversation sessions after a server restart
(`[session] resume_agents_on_restore`).

**`blocked` relies on screen shape.** Detection is deliberately strict: Herdr marks
`blocked` only when the snapshot matches a known approval/question UI, otherwise it
falls back to `idle`. In testing, a Claude Code write-approval dialog matched a
low-priority fallback rule rather than a primary one — it worked, but it's a weaker
signal that could regress if the agent's dialog shape changes. A new prompt shape
shows as `idle` until Herdr's manifests learn it.

**Menus report `unknown` by design.** Claude Code's `/model` picker, for example,
reports `unknown` with `skipped_update_reason` rather than `blocked`. Menus are not
treated as blocking prompts.

## Alternatives considered

Running a full Herdr server *inside* the container also works, and the host can
reach it by proxying a Unix socket over `docker exec -i`. But that server owns its
own agents, so you get a second Herdr UI rather than container agents appearing in
your host session — which is not what most people want here. `herdr --remote` is an
`ssh -T` stdio bridge and requires sshd in the container plus exactly matching
client/server protocol versions.

## Verification status

The launch method, all four states, and `herdr agent prompt` / `send-keys` control
were verified against Claude Code 2.1.245 in a VS Code devcontainer on macOS with
Docker Desktop, using Herdr 0.8.0 (protocol 19). The control case — the same command
without `HERDR_AGENT` — produced no detection, confirming the prefix is what does
the work.

The assertion behaviour was verified separately by running the prefix against
non-agent commands and comparing which foreground processes Herdr honours
(`sleep` and `cat` ignored; `docker` and `python3` accepted).

Name-based local identity was verified with a shell script named `claude`
(detected as Claude despite its processes being `sh`/`sleep`), and identity
teardown by returning the pane to its shell (`agent=None`).

For the wider investigation behind this document — Herdr's plugin system, the
socket API, and the approaches that turned out to be dead ends — see
[herdr-research-notes.md](./herdr-research-notes.md).

The `dcagent` helper and the `sh -lc 'exec claude'` form are adaptations of the
verified command rather than the exact tested invocation.

## References

- [Agents](https://herdr.dev/docs/agents) — detection, status authority, `HERDR_AGENT`
- [Socket API](https://herdr.dev/docs/socket-api) — `pane.report_agent` and state reporting
- `herdr.dev/llms.txt` — documentation index with raw sources pinned per release
