# Herdr daemon and activity signals — observed facts

Evidence for [`.plans/herdr-daemon-discovery.md`](../.plans/herdr-daemon-discovery.md),
gathered for the `agent-caffeinate` and `workspace-time-tracker` plugins.

**Test bed differs from the earlier probes.** Everything here ran **inside the
devcontainer** — Linux arm64, Herdr **0.8.2 (protocol 20)** — against a Herdr server
booted in-container under a pty. `docs/herdr-vscode-sync-facts.md` was macOS host,
0.8.0/protocol 19. Where the two disagree, assume a version or platform difference and
re-check rather than assuming one is stale. Probes needing macOS (`caffeinate`) or a real
agent are marked **UNRUN** below.

## Summary of the gating answers

| Question | Answer |
| --- | --- |
| Does a daemon spawned from `[[startup]]` survive? | **YES.** Reparents to PID 1, keeps running, hook returns in **31 ms**. |
| Does it die when the server stops? | **NO — it outlives the server.** An unattended daemon becomes a permanent orphan. |
| `events.subscribe` params shape | `{"subscriptions":[{"type":"workspace.focused"}, …]}` — array of **objects**. |
| Can one subscription cover all agent panes? | **NO.** `pane.agent_status_changed` requires a concrete existing `pane_id`. |
| Is the socket connection reusable for polling? | **NO.** The server closes it after answering any non-subscribe request. |
| Does `panes[].revision` track activity? | **NO.** It bumps on structural change (cwd), not output or keystrokes. |
| Is a screen hash a usable activity token? | **YES for plain panes** (0.59 ms/call), **NO for animating ones**. |

**Verdict: daemon model is GO**, but two findings reshape both plugins — see
[Consequences](#consequences-for-the-two-plugins).

## A. Daemon survival — **GO**

`[[startup]]` fired on server boot (not on `plugin link`, matching probe 12).
`HERDR_PLUGIN_EVENT` was the literal string `startup` and `HERDR_PLUGIN_EVENT_JSON` was
unset, both as previously recorded. `HERDR_PLUGIN_CONTEXT_JSON` was populated.

The hook double-forked a heartbeat and returned. From `herdr plugin log list`:

```json
{"command":["./probe-startup.sh"],"event":"startup","exit_code":0,
 "started_unix_ms":1787843735821,"finished_unix_ms":1787843735852,"status":"succeeded"}
```

**31 ms**, exit 0 — the hook is not held open by the surviving child. Meanwhile:

```
$ ps -eo pid,ppid,pgid,sess,args | grep heartbeat
23535     1 23482 23482 /bin/sh .../probe-startup.sh
24176 23535 23482 23482 sleep 2
```

`PPID 1` — fully reparented. Note `pgid`/`sess` remain **23482, the server's**, because
this run double-forked without `setsid`. Use `setsid` so a group-wide signal to the
server cannot take the daemon with it.

### A1b — the daemon OUTLIVES its server (unplanned, and the important one)

```
before stop: heartbeat 23535 alive? yes, lines=32
$ herdr session stop probe2
stopped session probe2
after stop:  heartbeat 23535 alive? yes, lines=35     <- still ticking
```

Herdr does **not** reap plugin-spawned daemons when the server exits. Observed twice.
A daemon that does not notice server death runs forever — and for `agent-caffeinate`
that means an assertion held forever, on a machine with no Herdr running. **Detecting
server death is a correctness requirement, not a nicety.**

**UNRUN:** A2 (foreground-hook timeout), A5 (`update --handoff`). A4 is implied — each
server boot produced its own invocation with its own `HERDR_SOCKET_PATH` and derived key.

## B. The socket API

### B1 — `events.subscribe` params, verbatim from the 0.8.2 schema

`schemas/request/$defs/EventsSubscribeParams`:

```json
{"properties":{"subscriptions":{"items":{"$ref":"#/schemas/request/$defs/Subscription"},
 "type":"array"}},"required":["subscriptions"],"type":"object"}
```

`Subscription` is a 27-variant `oneOf` of `{"type": "<dotted name>"}`. Success reply:

```json
{"id":"t","result":{"type":"subscription_started"}}
```

Three variants take extra fields, and **require** them:

| Type | Extra properties | Required |
| --- | --- | --- |
| `pane.agent_status_changed` | `pane_id`, `agent_status` | **`pane_id`** |
| `pane.scroll_changed` | `pane_id` | **`pane_id`** |
| `pane.output_matched` | `pane_id`, `source`, `match`, `lines`, `strip_ansi` | **`pane_id`, `source`, `match`** |

### B1b — no wildcard for pane-scoped subscriptions

```
agent_status_changed, NO pane_id     -> error invalid_request: missing field `pane_id`
agent_status_changed, pane_id='*'    -> error pane_not_found: pane * not found
agent_status_changed, pane_id=''     -> error pane_not_found: pane  not found
agent_status_changed, real pane      -> {"result":{"type":"subscription_started"}}
agent_status + agent_status filter   -> {"result":{"type":"subscription_started"}}
all 24 broadcast types in one call   -> {"result":{"type":"subscription_started"}}
```

So a daemon wanting agent status for **every** pane must subscribe **per pane** and
maintain those subscriptions as panes appear and disappear. There is no session-wide
agent-status stream.

### B2 — the connection is closed after any non-subscribe request

```
session.snapshot -> {"result":{"type":"session_snapshot", …}}
    after response, recv gave: b''        <- EOF, server closed
pane.list        -> {"result":{"type":"pane_list", …}}
    after response, recv gave: b''        <- EOF, server closed
```

One request per connection. Only `events.subscribe` keeps a connection open. A daemon
therefore needs a **fresh connection per poll**, which is cheap (below) — and a
subscription cannot be reused to also fetch state.

### B3 — subscribe delivers events that hooks never see

24 broadcast types subscribed, session driven with workspace create/focus/rename/close,
tab create, pane split, pane focus. 24 frames in 22 s:

| Event | Count | Hook verdict (probe 18) |
| --- | --- | --- |
| `pane.updated` | **7** | **INERT as hook — SUBSCRIBE-ONLY** |
| `layout.updated` | **3** | **INERT as hook — SUBSCRIBE-ONLY** |
| `pane.created` | 3 | fires |
| `pane.focused`, `workspace.focused`, `tab.focused`, `tab.created` | 2 each | fires |
| `workspace.created`, `workspace.renamed`, `workspace.closed` | 1 each | fires |

`pane.updated` carries the **full pane record including `cwd`**:

```json
{"data":{"pane":{"agent_status":"unknown","cwd":"/tmp","pane_id":"w2:p2",
 "revision":1,"terminal_title":"vscode@8178a2876953:/tmp","workspace_id":"w2"},
 "type":"pane_updated"},"event":"pane_updated"}
```

This confirms probe 18's suspicion from the other side: the event a cwd-tracking plugin
wants is delivered over a subscription and never to a hook. It is also directly relevant
to the existing `vscode-workspace-sync`, whose "cd doesn't move the Space" gap a daemon
would close.

Envelope is `{"event":"<underscored>","data":{"type":"<underscored>", …}}`, one JSON
object per line, as previously recorded. Connection stayed **alive for the full 22 s**.

**UNRUN:** B4 (5-minute idle keepalive), B5 (EOF on server stop over a live
subscription — inferred from B2's per-request EOF but not directly observed).

### B6 — no replay on subscribe

No initial-state burst followed `subscription_started`. One `pane.updated` arrived at
t+0.00 s before the driver began, cause unidentified; treat state seeding as required.

### D5 — cost, measured

100 calls each, new connection per call, no process spawn:

| Call | Per call | At 1/s | At 1/10s |
| --- | --- | --- | --- |
| `session.snapshot` | **0.35 ms** | 0.035% of a core | 0.0035% |
| `pane.list` | **0.31 ms** | 0.031% | 0.0031% |
| `pane.read` (visible) | **0.59 ms** | 0.059% | 0.0059% |
| `herdr` CLI subprocess | 0.86 ms | — | for comparison |

Polling the socket directly is ~3x cheaper than spawning the CLI and **cheap in
absolute terms**: a 1 Hz snapshot poll costs 0.035% of a core.

## C. Agent status

`pane.list` and `session.snapshot` pane records both carry `agent_status` (`"unknown"`
for a plain shell) alongside `cwd`, `foreground_cwd`, `focused`, `workspace_id`,
`tab_id`, `revision`, `terminal_title` and a `scroll` object. Seeding and re-seeding from
a snapshot is confirmed viable.

**UNRUN — needs a real agent:** C1 (the working/blocked/idle/done sequence and whether
repeats carry unchanged values), C2 (what arrives when an agent exits), C3 (killing a
pane mid-task).

### C4 — a socket-only reader sees `done`, essentially never `idle`

Observed 2026-08-27 from `agent-caffeinate`'s transition log, and explained by
`docs/herdr-research-notes.md`: the enum is `idle | working | blocked | done | unknown`,
and **`done` is `idle` whose tab has not been seen in the focused UI — CLI reads do not
mark a tab seen.**

A daemon that only polls the socket therefore never marks anything seen, so a finished
agent stays `done` until the human personally clicks into that tab. In practice `done` is
the normal post-work status and `idle` the exception: every gap in the first minutes of
observation was `done:Ns`, and `idle` appeared only after the tab was visited.

Consequences for any plugin that reasons about agents going quiet:

- Treat `done` and `idle` as the same thing. Matching only `idle` finds nothing.
- `done` is **not** a terminal or success signal despite the name. It carries no more
  information than `idle` does about whether the agent actually stopped — both can be the
  `default_known_agent_idle_fallback` rule firing on an unrecognised screen.
- `blocked` is the one status that positively means "waiting on a human", which makes it
  the only reliable way to separate a prompt-wait from a possible detection failure.

## D. Activity signals

### D2 — `panes[].revision` is NOT an activity signal

Controlled: each step verified the pane actually responded, by reading the screen back
and confirming a marker string and the changed `cwd`. (A first attempt reported "no
change" for everything because the pty had **2 rows** and the pane was effectively blank
— a false negative worth remembering when driving Herdr headlessly.)

```
baseline                 : revision=1
(a) after command output : revision=1  NO CHANGE
(b) after keystrokes only: revision=1  NO CHANGE
(c) after cd             : revision=2 (cwd=/tmp)  CHANGED
```

`revision` tracks **structural** change, not content. It is a usable cheap signal for
"the pane's cwd/title changed", and useless for "the human is doing something".

### D3 — a screen hash IS a usable activity token, except when the pane animates

`pane.read {source: "visible"}`, sha256 of `result.read.text`:

```
stability over 6s idle : ['8020d60bac50','8020d60bac50','8020d60bac50'] -> STABLE
after new output       : CHANGED   (activity detected)
after typing, no enter : CHANGED   (typing detected)
```

Valid sources are `visible`, `recent`, `recent_unwrapped`, `detection` — **`source` is
required**; `screen` and `scrollback` are not valid values.

**The animation case, tested with `top -d 1` as a proxy for an animated agent UI:**

```
source=visible    4 distinct hashes in 4 samples over 8s -> pane looks permanently active
source=detection  4 distinct hashes in 4 samples over 8s -> pane looks permanently active
after quitting top: STABLE again
```

Both sources fail. Any pane running a redrawing TUI — `top`, `htop`, `watch`, a progress
bar, **or an agent with an animated spinner** — reads as permanently active and would
hold a time entry open forever.

**UNRUN:** the same test against a real Claude pane, and D4 (client attach/detach).

## Consequences for the two plugins

### 1. Drop `events.subscribe` from both designs; poll instead

Both plans specified a subscription-driven daemon. Given B1b (no session-wide agent
status), B2 (one request per connection) and D5 (a snapshot costs 0.35 ms), a plain
**poll loop is simpler and strictly more robust**:

```
every pollIntervalSec:
    connect -> session.snapshot -> close       # 0.35 ms
    decide from the pane records
    connect refused  ->  the server is gone  ->  release and exit
```

This removes per-pane subscription bookkeeping, removes the missed-event class of bug
entirely (the periodic re-seed *becomes* the mechanism rather than a backstop), and
gives server-death detection for free via a failed connect — which A1b proved is a
correctness requirement. A 2 s poll costs ~0.02% of a core.

A1b has a second consequence, found while implementing: because Herdr never reaps a
plugin daemon, a daemon that is alive-but-stuck is not cleaned up by anything, and its
lock is keyed on the socket path — so it blocks its own replacement across a Herdr
restart, leaving that server permanently unmanaged. A plugin daemon therefore needs
**two** liveness mechanisms, not one: it must notice its server dying (failed connect),
and a starting daemon must be able to displace a predecessor that has stopped making
progress. `agent-caffeinate` implements the latter by writing `updated_at` on every poll
and treating a several-interval-stale holder as displaceable, after a confirmation delay
so a machine returning from sleep is not mistaken for a wedge.

The `[[events]]` hook on `workspace.focused` still earns its place for instant
self-heal, and a subscription remains the right tool if sub-second latency is ever
wanted.

### 2. The time tracker's activity token is the screen hash, with an agent carve-out

`revision` is out (D2). Per-pane rule:

- Pane **has a detected agent** → trust `agent_status` from the snapshot. Never hash an
  agent pane's screen; D3 shows an animated UI defeats it.
- Pane is a **plain shell** → hash `pane.read {source:"visible"}` (0.59 ms).

Known false positive to document: a plain pane running an animating TUI reads as
permanently active. `pane.process_info` exists and could refine this later.

## Reproducing

The harness is `.plans/scratch/herdr-daemon-probe/` (gitignored). A server can be booted
headlessly in the container with a pty — **set the window size**, or every screen probe
silently measures a 2-row blank buffer:

```python
pid, fd = pty.fork()
if pid == 0:
    os.environ.pop("HERDR_ENV", None)
    os.execvp("herdr", ["herdr", "--session", "probe"])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 140, 0, 0))
```

`herdr pane list` already emits JSON; **`--json` is not a valid flag at 0.8.2**.
