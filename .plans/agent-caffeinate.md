# agent-caffeinate

A Herdr plugin that holds a sleep-inhibiting assertion for exactly as long as coding
agents are working, and releases it after a minute of quiet.

**Python 3.9+, standard library only, no build step, no dependencies** — same runtime
contract as `vscode-workspace-sync`, so `herdr plugin install <owner>/<repo>/agent-caffeinate`
is the whole install story.

> **Prerequisite:** [`herdr-daemon-discovery.md`](herdr-daemon-discovery.md) probe **A**
> must return GO. If a daemon spawned from `[[startup]]` does not survive, the design in
> this plan is wrong and the fallback recorded in the facts doc replaces it.

## What it does

While any pane in the session reports agent status `working`, the machine does not go to
sleep. `idleGraceSec` (default **60**) after the last one stops working, the assertion is
released.

### Why the flags are `caffeinate -i -s`

The goal is "an agent's work is not interrupted while I step away", nothing more.

| Flag | Verdict |
| --- | --- |
| `-i` | **In.** Prevents system idle sleep. This is the one that matters. |
| `-s` | **In.** Prevents system sleep on AC power. Harmless alongside `-i`. |
| `-d` | **Out of the default.** Display-only. A sleeping display never suspends a process, so it buys the agent nothing and leaves an unlocked screen unattended. Available via config. |
| `-m` | **Out.** Disk idle sleep — a spinning-platter assertion, a no-op on SSD. |
| `-u` | **Out.** Without `-t` it asserts user-active for a default of **5 seconds** and wakes the display. Useless as a long-lived assertion. |

## Design

### One daemon per Herdr server

`[[startup]]` fires once per **server boot** (probe 12), and plugin registration is
global across sessions (probe 11), so every running session's server starts one daemon.
That is correct here — each session has its own agents — and the singleton key is
therefore **per socket**, not global:

```
$HERDR_PLUGIN_STATE_DIR/<session-key>/daemon.lock    # flock, held for the daemon's life
$HERDR_PLUGIN_STATE_DIR/<session-key>/daemon.json    # pid, started_at, inhibitor pid
$HERDR_PLUGIN_STATE_DIR/<session-key>/daemon.log     # rotating, 1 MB, one rollover
```

`<session-key>` is the **first 12 hex characters of `sha256($HERDR_SOCKET_PATH)`**, with
the full socket path stored inside `daemon.json` so a human can tell which is which.

Startup sequence: acquire the lock **non-blocking**; if it is held, exit 0 silently
(another daemon owns this session). Otherwise double-fork + `setsid`, redirect stdio to
`daemon.log`, and let the hook process return immediately — probe A2 bounds how long the
hook may take, and detaching well inside it is the whole point.

### The tracking loop

1. **Seed** from `$HERDR_BIN_PATH api snapshot`, reading
   `.result.snapshot.panes[]` into `{pane_id: agent_status}`. `events.subscribe` has no
   replay (probe B6), so without this a daemon started mid-task never sees the
   already-working agent.
2. **Subscribe** on `$HERDR_SOCKET_PATH` to `pane.agent_status_changed`, plus whichever
   of `pane.exited` / `pane.closed` / `tab.closed` probe C3 shows actually delivers.
3. **Loop** on `select()` with a timeout of `min(seconds until the pending stop, 5)`, so
   the grace period expires without needing an event to wake the loop.
4. **Re-seed every `reseedSec` (default 60)** from the snapshot, replacing the map
   wholesale. This is the safety net for the C2 case — an agent that exits without a
   final status event would otherwise strand its pane at `working` and pin the assertion
   forever. Correctness does not depend on any single event being delivered.
5. **EOF on the socket means the server is gone**: stop the inhibitor, release the lock,
   exit 0.

Dedupe on `(pane_id, status)` — probe 18 measured 0.85 events/s per agent pane, and most
carry an unchanged value. Only a genuine transition may touch the inhibitor.

### Active / idle decision

- **active** = any pane's status is in `activeStatuses` (default `["working"]`).
- `blocked` is deliberately **not** active: it means the agent is waiting on a human, so
  no work is in flight and there is nothing to protect.
- active and no inhibitor running → start it.
- not active → record `idle_since` (once; do not reset while still idle). When
  `now - idle_since >= idleGraceSec` → stop the inhibitor. Any pane going active again
  before then clears `idle_since` and the inhibitor is never touched.

### Inhibitor process handling

- Spawned with `start_new_session=True` so it is not in the daemon's foreground group.
- Its pid is written to `daemon.json` **before** any further work.
- Stopped with `SIGTERM`, then `SIGKILL` after 2 s if still alive.
- On daemon start, if `daemon.json` names a live inhibitor pid **whose executable
  matches the configured argv[0]**, kill it — that is a leak from a `kill -9`'d daemon
  (probe E2).
- `SIGTERM`/`SIGINT`/normal exit all stop the inhibitor. `atexit` as a backstop.

### Degrading where there is no inhibitor

If `shutil.which(argv[0])` is None — the devcontainer case — the daemon logs **one**
warning and runs in **dry mode**: it tracks status and logs every transition it *would*
have acted on, but spawns nothing. This is what makes the plugin harmless on Linux and
makes the offline tests trivial: the fake inhibitor is just a different argv.

## Contract

### Layout

```
agent-caffeinate/
  herdr-plugin.toml
  README.md
  config.example.json
  bin/agent-caffeinate          # sh shim -> src/main.py, same python3 probe as bin/sync
  src/main.py                   # CLI dispatch
  src/config.py
  src/jsonc.py                  # copied verbatim from vscode-workspace-sync/src/jsonc.py
  src/sock.py                   # NDJSON client: request/response + subscribe iterator
  src/daemonize.py              # lock, double-fork, log rotation, pidfile
  src/inhibitor.py              # spawn/stop/adopt-stale
  src/tracker.py                # status map + active/idle state machine (pure, clock-injected)
  test/                         # unittest, plus fake-caffeinate and fake_server.py
```

`src/jsonc.py` is a **deliberate copy**, not a shared module: `herdr plugin install`
fetches a single subdirectory, so a plugin must be self-contained. Note that in the file
header.

### CLI

`bin/agent-caffeinate <command>`:

| Command | Behaviour |
| --- | --- |
| `daemon` | Detach and run. Exit 0 immediately if this session's lock is held. |
| `daemon --ensure` | Same, but intended for the event hook — always exit 0, never log noise when already running. |
| `daemon --foreground` | Run in-process, log to stderr. For tests and debugging. |
| `daemon --restart` | Signal the running daemon to exit, wait up to 5 s, start a new one. |
| `stop` | Stop this session's daemon and its inhibitor. Exit 0 if none. |
| `status` | Human-readable: daemon pid/uptime, inhibitor pid or `dry`, per-pane statuses, and either `holding` or `stopping in Ns`. |
| `status --json` | The same as one JSON object. |
| `doctor` | Config source and parsed values, socket path, session key, whether argv[0] resolves, and the resolved inhibitor argv. |

Exit codes: `0` success, `1` config error (message on stderr), `2` cannot reach the
Herdr socket.

### Manifest

```toml
id = "agent-caffeinate"
name = "Agent Caffeinate"
version = "0.1.0"
min_herdr_version = "0.8.0"
description = "Keep the machine awake while coding agents are working"
platforms = ["macos", "linux"]

[[startup]]
command = ["./bin/agent-caffeinate", "daemon"]

# Self-heal only. The daemon already sees every focus change on its subscription; this
# exists so a daemon that died, or a plugin linked mid-session, recovers on the next
# navigation instead of at the next server boot. `--ensure` exits immediately when the
# lock is held, so the cost is one short process spawn per workspace focus.
[[events]]
on = "workspace.focused"
command = ["./bin/agent-caffeinate", "daemon", "--ensure"]

[[actions]]
id = "restart"
title = "Restart caffeinate daemon"
contexts = ["global"]
command = ["./bin/agent-caffeinate", "daemon", "--restart"]
```

**No `status` action**, for the reason `vscode-workspace-sync` gives for having no
`doctor` action: action stdout reaches the user only through
`herdr plugin log list`, JSON-escaped — the least legible channel available. `restart` is
an *operation*, not a diagnostic, so its output not being read is fine.

`pane.agent_status_changed` is **not** hooked. At 0.85/s per agent pane that is ~8% of a
core per agent for something the daemon already receives for free.

### Config — `$HERDR_PLUGIN_CONFIG_DIR/config.json`, entirely optional

Unlike `vscode-workspace-sync`, **the plugin works with no config file at all**. There is
nothing a user must tell it.

```jsonc
{
  // Seconds of no working agent before the assertion is released.
  "idleGraceSec": 60,

  // Statuses that count as "an agent is working". `blocked` means waiting on a human,
  // so it is deliberately not here by default.
  "activeStatuses": ["working"],

  // Omit to use the platform default:
  //   macOS -> ["caffeinate", "-i", "-s"]
  //   Linux -> ["systemd-inhibit", "--what=idle:sleep",
  //             "--why=herdr agent working", "--mode=block", "sleep", "infinity"]
  // Add "-d" here if you also want the display kept awake.
  "inhibitorCommand": null,

  // Full snapshot re-seed interval; the safety net for missed events.
  "reseedSec": 60,

  // "error" | "warn" | "info" | "debug"
  "logLevel": "info"
}
```

Unknown keys warn and are ignored. `idleGraceSec` and `reseedSec` accept floats and must
be `> 0`. Env overrides, for tests only: `HERDR_CAFFEINATE_INHIBITOR_COMMAND` (a JSON
array), `HERDR_CAFFEINATE_IDLE_GRACE_SEC`, `HERDR_CAFFEINATE_RESEED_SEC`,
`HERDR_CAFFEINATE_LOG_LEVEL`.

### Log lines

One line per event, `<iso8601> <level> <message>`. Transitions must be greppable:

```
2026-08-27T09:12:03-05:00 info  inhibitor start pid=44120 argv=caffeinate -i -s trigger=w4:p2 working
2026-08-27T09:41:55-05:00 info  inhibitor stop pid=44120 reason=idle-grace idle_for=60.4s
2026-08-27T09:41:55-05:00 info  dry-run: would stop inhibitor (no caffeinate on PATH)
```

## Faking it in the devcontainer

`caffeinate` does not exist here and no Herdr server is running, so the whole plugin is
exercised against two fakes. **Both fakes are test fixtures in the repo, not
production code paths** — the daemon has no idea it is being faked, it just gets a
different argv and a different socket path.

- **`test/fake-caffeinate`** — appends `START <pid> <epoch>` to `$FAKE_CAFFEINATE_LOG`,
  traps `TERM`/`INT` to append `STOP <epoch>`, then sleeps forever. Wired in with
  `HERDR_CAFFEINATE_INHIBITOR_COMMAND='["./test/fake-caffeinate"]'`.
- **`test/fake_server.py`** — binds a unix socket in a tempdir, answers
  `events.subscribe`, and then emits a **scripted timeline** of
  `pane_agent_status_changed` frames from a list of `(delay, pane_id, status)` tuples.
  Also used to simulate the failure modes: repeated identical statuses, a pane that goes
  `working` and never comes back (tests the re-seed), and an abrupt close (tests EOF
  handling).
- **`test/fake-herdr`** — a script printing a canned `api snapshot` envelope, wired in
  via `HERDR_BIN_PATH`, so seeding and re-seeding are exercised offline.

With `idleGraceSec=1` and `reseedSec=1` the end-to-end tests run in about a second each.

## Checklist

- [x] `agent-caffeinate/` skeleton: manifest, `bin/agent-caffeinate` shim (copy the
      python3-probe pattern from `vscode-workspace-sync/bin/sync`), `src/` layout
- [x] `src/jsonc.py` copied verbatim, header noting why it is duplicated
- [x] `src/config.py`: defaults, optional file, validation, env overrides, `doctor` data
- [x] `src/sock.py`: connect, one-shot request/response, `ServerGone` on an
      unreachable server. **The subscribe iterator was dropped** — see
      `## Discovery corrections`.
- [x] `src/daemonize.py`: flock singleton keyed on the socket hash, double-fork +
      `setsid`, stdio redirect, size-capped log with one rollover, `daemon.json` write
- [x] `src/inhibitor.py`: spawn with `start_new_session=True`, TERM-then-KILL stop,
      stale-pid adoption, `shutil.which` dry-mode detection
- [x] `src/tracker.py`: pure state machine — status map, dedupe, active predicate,
      `idle_since`, `next_deadline()`; clock injected, **no I/O**
- [x] `src/main.py`: CLI dispatch, poll loop with a deadline-aware sleep, signal
      handlers. Every poll is a full re-seed, so no separate re-seed timer exists.
- [x] `test/fake-caffeinate`, `test/fake_server.py`, `test/fake-herdr`
- [x] Unit tests for `tracker` covering: dedupe, two agents overlapping, grace reset,
      grace expiry, pane vanishing while `working` (re-seed rescue), unknown status
- [x] Unit tests for `config` covering: absent file, bad JSON, unknown key warning,
      non-positive `idleGraceSec`, env override precedence
- [x] End-to-end test: fake server + fake inhibitor, assert START then STOP with the
      right ordering and timing
- [x] End-to-end test: server closes the socket → inhibitor stopped, exit 0
- [x] End-to-end test: second `daemon` invocation exits 0 without a second inhibitor
- [x] `agent-caffeinate/README.md`: install, the flags rationale table, config, how to
      read the log, how to fake it
- [x] Root `README.md`: plugin table row
- [x] `.plans/PLAN.md` updated

## Validation

### Offline — must pass in the devcontainer

> **Interpreter gotcha — read before running any test command.** This devcontainer ships
> **`python3-minimal`**: `json`, `unittest`, `socket` and most of the stdlib are *absent*
> (`ls /usr/lib/python3.12 | wc -l` is 91, and `import json` raises
> `ModuleNotFoundError`). The sibling plugin's "147 tests on `/usr/bin/python3` 3.9.6"
> were run on the **macOS host**, whose system Python is 3.9.6 — not in here. Before
> running this plan's offline suite, either
> `sudo apt-get update && sudo apt-get install -y python3` in the container (and record it
> in `.devc/devc.jsonc` so it survives a rebuild), or run the suite on the host. The 3.9
> floor comes from the host interpreter and stays regardless.

- [x] `cd agent-caffeinate && python3 -m unittest discover -s test` — **58 tests pass**
- [ ] Same suite passes on the **oldest supported interpreter available** — **not run**:
      this container only has 3.12. No 3.10+ syntax is used; verify on the host's 3.9.
- [x] `./bin/agent-caffeinate doctor` prints resolved config, session key and socket path
      with no server running, exit code 0
- [x] `./bin/agent-caffeinate status` with no daemon prints `daemon: not running`, exit 0
- [x] `python3 -c "import ast,sys;[ast.parse(open(f).read()) for f in sys.argv[1:]]" src/*.py`
      — no syntax errors
- [x] No non-stdlib import anywhere in `src/`:
      `! grep -rnE '^\s*(import|from) (?!os|sys|json|time|socket|select|signal|errno|fcntl|hashlib|shutil|subprocess|argparse|logging|atexit|typing|unittest|tempfile|contextlib|datetime|re)' src/`
- [x] E2E: fake server scripted `working` → `$FAKE_CAFFEINATE_LOG` gains `START` within
      1 s; scripted `idle` → gains `STOP` between `idleGraceSec` and
      `idleGraceSec + 1` s after
- [x] E2E: two panes, one goes idle while the other still works → **no** `STOP` line
- [x] E2E: pane disappears from the snapshot while last known `working` → `STOP` arrives
      within `reseedSec + idleGraceSec + 1` s
- [x] E2E: 200 identical `working` frames produce exactly **one** `START` line
- [x] E2E: fake server closes the connection → `STOP` written, process exits 0 within 2 s
- [x] E2E: `daemon --foreground` twice concurrently → second exits 0, one `START` total

### Host — needs a real Herdr server and a Mac

Several of these were satisfied **in the devcontainer against a real Herdr server**
(0.8.2) with only the inhibitor faked; those are marked `[x]` with a note. The remainder
need macOS.


- [x] `herdr plugin link ./agent-caffeinate` then restart a **probe** session; the daemon
      appears in `ps` and `daemon.log` shows the seed — done in-container
- [ ] Run a real Claude task: `pmset -g assertions` shows `PreventUserIdleSystemSleep`
      within ~2 s of the agent going `working` — **macOS only.** The equivalent with a
      real Herdr `working` status and a fake inhibitor passed in-container
- [ ] ~60 s after the task finishes the assertion is gone, and `daemon.log` records
      `reason=idle-grace`
- [x] `herdr plugin log list --plugin agent-caffeinate` shows the startup invocation
      exiting **0 quickly** — measured **42 ms**, exit 0; the `--ensure` hook 46 ms
- [x] `kill -9` the daemon, then focus a workspace → the `workspace.focused` hook restarts
      it, and no orphan remains. Verified in-container (hook 37 ms, new pid). **Note:**
      the hook needs a *genuine* focus change — re-focusing the already-focused Space
      emits no event, and at 0.8.2 `workspace create` does not focus the new Space.
- [x] `herdr session stop probe` → no inhibitor survives. Verified in-container: the
      daemon logged `server gone`, released, and exited 0 within 2 s
- [x] Two sessions running concurrently produce two daemons, two session keys, and
      independent assertions — verified in-container: one session held while the other
      stayed idle.

## Relevant Files

| File | Change |
| --- | --- |
| `agent-caffeinate/herdr-plugin.toml` | **New.** |
| `agent-caffeinate/README.md` | **New.** |
| `agent-caffeinate/config.example.json` | **New.** |
| `agent-caffeinate/bin/agent-caffeinate` | **New.** sh shim. |
| `agent-caffeinate/src/main.py` | **New.** CLI + loop. |
| `agent-caffeinate/src/config.py` | **New.** |
| `agent-caffeinate/src/jsonc.py` | **New.** Verbatim copy from `vscode-workspace-sync/src/jsonc.py`. |
| `agent-caffeinate/src/sock.py` | **New.** |
| `agent-caffeinate/src/daemonize.py` | **New.** |
| `agent-caffeinate/src/inhibitor.py` | **New.** |
| `agent-caffeinate/src/tracker.py` | **New.** |
| `agent-caffeinate/test/*` | **New.** unittest suite + three fakes. |
| `agent-caffeinate/.gitignore` | **New.** Mirror the sibling plugin's. |
| `README.md` | Plugin table row. |
| `docs/herdr-daemon-facts.md` | Produced by the discovery plan; cited here. |
| `.plans/PLAN.md` | Status + phase row. |

## Discovery corrections

Folded in from [`docs/herdr-daemon-facts.md`](../docs/herdr-daemon-facts.md)
(devcontainer, Herdr **0.8.2 / protocol 20**). The daemon model is **GO**, but the
event-driven core of the design above is superseded.

### 1. There is no session-wide agent-status stream — **poll instead of subscribe**

`pane.agent_status_changed` requires a concrete, existing `pane_id`; `*` and `""` are
both rejected with `pane_not_found`, and omitting it is a hard `invalid_request`. A
subscription-driven daemon would have to maintain one subscription per pane and rebuild
them as panes come and go.

Since the server also **closes the connection after any non-subscribe request**, and a
`session.snapshot` over a fresh connection costs **0.35 ms** (0.035% of a core at 1 Hz),
the daemon becomes a poll loop instead:

```
every pollIntervalSec (default 2):
    connect -> session.snapshot -> close
    statuses = {pane_id: agent_status} from .result.snapshot.panes
    active   = any status in activeStatuses
    ... existing idle_since / grace logic, unchanged ...
    connect refused -> server gone -> stop inhibitor, release lock, exit 0
```

**`src/sock.py` shrinks to a one-shot request helper — the subscribe iterator is not
needed.** `reseedSec` disappears as a separate concept: every poll *is* a re-seed, so
the "pane stranded at `working`" failure mode is structurally impossible rather than
patched over. Replace `idleGraceSec`'s companion key with `pollIntervalSec` (default
`2`, must be `> 0`).

### 2. Detecting server death is a correctness requirement, not a nicety

Measured twice: **a plugin-spawned daemon outlives its server.** `herdr session stop`
left the heartbeat ticking, reparented to PID 1. Without the failed-connect exit above,
`agent-caffeinate` would hold a `caffeinate` assertion forever on a machine running no
Herdr at all. Keep `setsid` in the detach path so a group signal aimed at the server
cannot kill the daemon mid-release.

### 3. Startup hook timings are comfortable

The detached hook returned in **31 ms**, exit 0 (`plugin log list`), with the daemon
surviving. The host validation item asserting "exits 0 quickly" has a measured baseline.

### 4. Still unrun — needs a Mac or a real agent

- **Group E entirely** (`caffeinate`, `pmset -g assertions`, orphan behaviour). The
  default flags `-i -s` and the stale-pid adoption logic remain **unverified**.
- **C1–C3**: the real `working`/`blocked`/`idle`/`done` sequence, whether repeat events
  carry unchanged values, and what arrives when an agent exits or its pane is killed.
  The poll design is insensitive to all three, which is a further argument for it.
