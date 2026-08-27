# herdr-daemon-discovery

Answer the unknowns that **both** planned plugins — `agent-caffeinate` and
`workspace-time-tracker` — depend on, before either is built.

Deliverable: **`docs/herdr-daemon-facts.md`**, verbatim host output plus a go/no-go
verdict, in the style of [`docs/herdr-vscode-sync-facts.md`](../docs/herdr-vscode-sync-facts.md).

## Why this exists

Both plugins need something this repo has never proven:

1. A **long-lived daemon** started from `[[startup]]` that outlives the hook process.
2. A **`events.subscribe` socket subscription** held open on `$HERDR_SOCKET_PATH`.
3. A usable **human-activity signal**, because probe 18 established that a plain-shell
   pane fires *nothing* on `cd` or command output.

Probe 18 also established the cost of the alternative: `pane.agent_status_changed`
reaches hooks at **0.85/s per agent pane**, and a ~90 ms no-op hook is ~8% of a core per
agent. A daemon is the design that makes both plugins cheap — if it survives. If it does
not, both plugins change shape, so this runs first.

Everything here must run **on the macOS host**, not in the devcontainer: the devcontainer
has the `herdr` binary but no server, and `caffeinate` does not exist there.

## Probe harness

One throwaway plugin at `.plans/scratch/herdr-daemon-probe/`, linked with
`herdr plugin link`. Scratch is gitignored (this plan adds the root `.gitignore` entry);
delete the directory and `herdr plugin unlink herdr-daemon-probe` when done.

**Use a second named session for anything that restarts a server.** Per the research
notes, the live server is usually a child of a VS Code integrated terminal, and
`herdr server stop` kills every pane including the one running the probe:

```sh
env -u HERDR_ENV herdr --session probe        # start it
HERDR_SOCKET_PATH=~/.config/herdr/sessions/probe/herdr.sock herdr <cmd>
herdr session stop probe && herdr session delete probe
```

## Probes

### A. Does a daemon spawned from `[[startup]]` survive?

The single gating question. `[[startup]]` runs on **server boot**, not on
`plugin link` (probe 12), so each of these needs a fresh `probe` session.

- **A1 — detached child survives.** Startup command double-forks + `setsid`, writes its
  pid to `$HERDR_PLUGIN_STATE_DIR/probe.pid`, then appends `<epoch>` to
  `heartbeat.log` every 2 s. Boot the probe session, wait 120 s, then check the
  heartbeat file is still growing and `ps -p <pid>` still resolves. **Record the parent
  pid after detach** (`ps -o ppid= -p <pid>`) — 1 or a launchd shim means fully
  reparented.
- **A2 — is the hook killed on a timeout?** A second startup command that stays in the
  **foreground** for 90 s. Read `herdr plugin log list --plugin herdr-daemon-probe`:
  does it report an exit code, a timeout, or a kill, and after how long? This bounds how
  long the startup hook may take before detaching.
- **A3 — is `setsid` actually required?** Repeat A1 with a plain background child
  (`cmd &`, no `setsid`, no double fork). If that survives too, note it; the
  implementation uses double-fork + `setsid` regardless, but the answer tells us whether
  Herdr reaps the process group.
- **A4 — one daemon per server.** Confirm `[[startup]]` fires once per server boot, and
  that the default session and the `probe` session each produce their own invocation
  with distinct `HERDR_SOCKET_PATH`. This is what makes a **per-socket lock file** the
  right singleton key rather than a single global lock.
- **A5 — handoff (best effort).** If `herdr update --handoff` can be exercised safely on
  the probe session, record whether `[[startup]]` fires again for the new server and
  whether the old daemon's socket read returns EOF. Mark UNTESTED rather than guessing.

**If A1 fails**, stop and record the fallback shape in the facts doc: `[[events]]` hook
on `pane.agent_status_changed` writing a heartbeat file, plus a one-shot detached
`sleep <grace>; re-check; maybe stop` reaper — and the measured per-agent CPU cost of
paying 0.85 hook spawns/second.

### B. `events.subscribe` framing over a held-open connection

- **B1 — exact request shape.** From the bundled schema, not from guessing:
  ```sh
  herdr api schema --json > /tmp/schema.json
  jq '.. | objects | select(has("method")) | select(.method|test("events"))' /tmp/schema.json
  jq '.definitions | keys | map(select(test("Event|Subscribe";"i")))' /tmp/schema.json
  ```
  Record verbatim: the `events.subscribe` params object (does it take an event-type
  array? filters? a subscription id?), its success response, and whether
  `events.unsubscribe` exists.
- **B2 — delivery on the same connection.** Hold the socket open with a small script,
  send one subscribe request, and capture the **first three raw lines** received
  verbatim. Confirm the envelope is `{"event":"<underscored>","data":{…}}` on the same
  connection, one JSON object per line, and note whether events are interleaved with
  responses to later requests on that same connection (i.e. whether the daemon may reuse
  one connection for both, or needs two).
- **B3 — which types actually deliver over subscribe.** Subscribe to all 27 and exercise
  the session exactly as probe 18 did (cd, split, focus pane, new tab, focus tab, rename,
  focus workspace, run a command, close tab, close workspace). Produce **the same table
  as probe 18** so the hook-vs-subscribe difference is directly comparable. The ones that
  matter most: `pane.updated`, `pane.exited`, `pane.closed`, `pane.agent_status_changed`,
  `pane.output_matched`, `pane.scroll_changed`, `workspace.focused`, `workspace.closed`.
- **B4 — idle keepalive.** Hold a subscription open for **5 minutes with no session
  activity**. Does the server ping, does it close the connection, is there a read
  timeout? Record whether the daemon needs its own keepalive.
- **B5 — server death is visible.** Stop the probe session's server while subscribed.
  Confirm the client read returns **EOF (empty read) promptly**, and record how long it
  took. This is how the daemon learns to shut down and release its inhibitor.
- **B6 — no replay.** Confirm `events.subscribe` delivers no initial-state snapshot, so
  the daemon must seed from `herdr api snapshot`.

### C. Agent-status signal quality (`agent-caffeinate`)

- **C1 — the real status sequence.** With a subscription open, run one Claude Code task
  end to end. Capture every `pane_agent_status_changed` payload verbatim. Record: which
  of `idle|working|blocked|done|unknown` appear, in what order, whether `done` ever
  arrives, and whether repeat events carry an **unchanged** status (probe 18's 0.85/s
  strongly suggests yes — the daemon must dedupe on `(pane_id, status)`).
- **C2 — agent exit.** Quit the agent (`/exit`) and record which event, if any, reports
  identity clearing. If nothing does, a pane can be stranded at `working` forever — that
  is the case the periodic re-seed exists to fix, and the facts doc should say so
  explicitly.
- **C3 — kill the pane** while its agent is `working` (close the tab). Which of
  `pane.closed` / `pane.exited` / `tab.closed` arrives over subscribe?
- **C4 — seeding source.** Print one **agent** pane record from
  `herdr api snapshot | jq '.result.snapshot.panes[]'` verbatim, and the whole
  `.result.snapshot.agents` value. Confirm a pane record carries `agent` and
  `agent_status`, since that is what the daemon seeds and re-seeds from.

### D. Human-activity signal (`workspace-time-tracker`)

- **D1 — does a plain shell emit anything over subscribe?** Repeat probe 18's plain-shell
  test with a *subscription* rather than hooks: type without pressing enter, run a
  command that produces output, scroll back. Record which events fire and their
  timestamps. Probe 18 says hooks saw nothing; subscribe may see `pane.updated` or
  `pane.scroll_changed`.
- **D2 — does `panes[].revision` increment on terminal output?** *(highest-value probe
  here.)* The pane record in probe 4 carries `"revision":1`. Sample it twice around
  output:
  ```sh
  herdr pane list --json | jq '.result.panes[] | {pane_id, revision}'
  # produce output in a pane, then sample again
  ```
  Test three separate causes: (a) command output, (b) keystrokes that only redraw a
  prompt with no command run, (c) a `cd`. If `revision` bumps on output, the time
  tracker's activity probe is a single cheap `pane list` call and no screen reading is
  needed at all.
- **D3 — screen-hash fallback cost.** If D2 is negative, measure
  `herdr pane read <pane> --source detection`: output shape, and `time` over 20
  iterations to get a per-poll cost. Also confirm the read is **stable when nothing
  changes** (two consecutive reads of a quiet pane hash identically) — a spinner or a
  clock in the prompt would make everything look permanently active, which would break
  idle detection entirely. Test against a Claude pane specifically, since its UI
  animates.
- **D4 — attached/detached.** Record `herdr status client --json` verbatim while
  attached and after detaching (`ctrl+b q`), and whether any event announces it. If a
  detach is observable, the tracker can stop accruing time when nobody is looking at the
  session.
- **D5 — cost of the chosen probe.** Time 20 iterations of whichever of D2/D3 wins, and
  state the CPU cost at a 10 s poll interval.

### E. `caffeinate` behaviour under a daemon

- **E1 — the assertion lands.** From a detached daemon, spawn `caffeinate -i -s` and
  confirm with `pmset -g assertions` that `PreventUserIdleSystemSleep` is held and
  attributed to the caffeinate pid. Confirm it disappears when the child is killed.
- **E2 — orphaning.** `kill -9` the daemon. Does `caffeinate` linger? Record the pid
  state. This determines whether the daemon must record the inhibitor pid to disk and
  reap a stale one on next start (the plan assumes it must).
- **E3 — flags sanity.** Confirm `caffeinate -i -s` does **not** keep the display awake
  and does not prevent the screen lock, i.e. that the chosen default behaves as
  documented. (`-m` is a spinning-disk assertion and is a no-op on SSD; `-u` without
  `-t` asserts user-active for a default of 5 s and wakes the display, so neither is in
  the default.)
- **E4 — Linux equivalent (informational).** Note whether `systemd-inhibit` exists on
  any Linux box available. Not gating — the devcontainer has no systemd and the plugin
  is specified to degrade to a logged no-op there.

## Checklist

- [x] Create `.plans/scratch/herdr-daemon-probe/` (manifest + probe scripts) and add
      `.plans/scratch/` to a new root `.gitignore` — built; `subscribe.py` smoke-tested
      against a fake server (negotiation fallback, frame capture, EOF detection and the
      subscribe-vs-hook table all verified). Run order is in the harness README.
- [x] Probe A1–A4 run and recorded (in-container, 0.8.2); **A2 and A5 remain UNRUN**
- [x] Probe B1–B3 and B6 run and recorded, including the delivery table; **B4 (5-min
      idle keepalive) and B5 (EOF over a live subscription) remain UNRUN**
- [x] C4 recorded (pane records carry `agent_status`); **C1–C3 UNRUN — need a real agent**
- [x] D2, D3, D5 run and recorded — **D2 verdict: NO**, screen hash wins, animation
      breaks it; **D1 and D4 UNRUN**
- [ ] Probe E1–E4 run and recorded — **UNRUN, needs macOS**
- [x] `docs/herdr-daemon-facts.md` written
- [x] Facts doc states the verdict: daemon model is **GO**
- [x] Activity probe chosen: screen hash at 0.59 ms, with an agent-pane carve-out
- [x] Corrections folded into both plugin plans
- [ ] `docs/herdr-research-notes.md` updated with anything that contradicts or extends it
      (especially: subscribe-vs-hook event delivery, keepalive, `revision` semantics)
- [ ] Root `README.md` docs table gains a row for `docs/herdr-daemon-facts.md`
- [ ] Scratch probe deleted and unlinked

## Validation

Every item below is "run it and paste the output into the facts doc". A probe with no
verbatim output in the doc does not count as run.

- [ ] `herdr --version` and `herdr plugin list --json` confirm the probe plugin is linked
- [ ] A1: heartbeat file has **≥ 50 lines** 120 s after boot, and `ps -p <pid>` resolves
- [ ] A2: `herdr plugin log list --plugin herdr-daemon-probe` shows the foreground
      command's exit code **and duration**
- [ ] A4: two distinct startup invocations recorded with two distinct
      `HERDR_SOCKET_PATH` values
- [ ] B1: verbatim `events.subscribe` params + response schema fragments
- [ ] B2: three raw received lines, verbatim, unedited
- [ ] B3: a fired/never-fired table covering all 27 types
- [ ] B4: an explicit "held N minutes, connection alive/closed" line
- [ ] B5: an explicit "EOF observed after N ms" line
- [ ] C1: the full status sequence of one agent task, verbatim
- [ ] C4: one agent pane record and the `agents` object, verbatim
- [ ] D2: an explicit YES/NO on `revision` incrementing, with before/after values for all
      three causes (a) (b) (c)
- [ ] D3 (if reached): two consecutive hashes of a quiet **Claude** pane, and whether
      they match
- [ ] D5: measured per-probe milliseconds and the derived cost at a 10 s interval
- [ ] E1: `pmset -g assertions` output with and without the inhibitor running
- [ ] E2: an explicit "orphan survives / does not survive" line
- [ ] `herdr plugin unlink herdr-daemon-probe` succeeds and
      `ls .plans/scratch` is empty or absent

## Relevant Files

| File | Change |
| --- | --- |
| `docs/herdr-daemon-facts.md` | **New.** The deliverable. |
| `docs/herdr-research-notes.md` | Extend: subscribe-vs-hook delivery, keepalive, `revision`, daemon survival. |
| `.claude/skills/herdr-plugin-authoring/SKILL.md` | Add the daemon/subscribe findings if they generalise beyond these two plugins. |
| `README.md` | Docs table row for `docs/herdr-daemon-facts.md`. |
| `.gitignore` | **New**, root. Ignore `.plans/scratch/`. |
| `.plans/scratch/herdr-daemon-probe/` | **New, throwaway.** Deleted by the last checklist item. |
| `.plans/PLAN.md` | Status + phase row. |
| `.plans/agent-caffeinate.md` | `## Discovery corrections` section. |
| `.plans/workspace-time-tracking.md` | `## Discovery corrections` section. |
