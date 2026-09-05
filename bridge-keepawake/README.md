# bridge-keepawake

A [Herdr](https://herdr.dev) plugin that keeps the **devcontainer host** awake while
your coding agents work **inside** the container — by pinging
[`devc-bridge`](https://github.com/bmingles/devc-tools/tree/main/devc-bridge) instead
of running an inhibitor itself.

**Use this instead of [`agent-caffeinate`](../agent-caffeinate/) when Herdr runs
*inside* a devcontainer.** Never run both in one session — see
[Which plugin, when](#which-plugin-when).

## Which plugin, when

| Where Herdr runs | Plugin |
| --- | --- |
| On your machine directly (the normal case) | [`agent-caffeinate`](../agent-caffeinate/) |
| Inside a devcontainer (Herdr's client, server, and agents all in the container) | **`bridge-keepawake`** (this one) |

`agent-caffeinate` asserts a sleep inhibitor (`caffeinate`/`systemd-inhibit`) on the
machine Herdr runs on. Inside a devcontainer neither exists in a useful form — the
container has no `caffeinate`, and a Linux inhibitor asserted in a container namespace
tells the host Mac nothing. `bridge-keepawake` doesn't try to inhibit anything itself:
it pings `devc-bridge`, and the **host** (which already owns the inhibitor and the idle
timeout) starts/stops `caffeinate` on its own.

That's what makes this plugin nearly empty. Everything `agent-caffeinate` has to get
right — starting/stopping the inhibitor, tuning a grace period against false-idle
gaps, adoption and manual-stop-wins semantics — already lives on the host, behind
`devc-bridge`'s own `DEVC_BRIDGE_KEEPAWAKE_IDLE_MS` idle timeout (default 5 minutes).
This plugin has exactly one job: **while any pane in this Herdr session reports a
working agent, ping the bridge.** No grace period, no held state, no inhibitor code.

## Setup

Needs Herdr 0.8.0+, Deno on the container's `PATH`, and `devc-bridge` wired into the
devcontainer (Feature + token mount — see
[devc-tools' devc-bridge Feature](https://github.com/bmingles/devc-tools/tree/main/features/devc-bridge)).
**Linux only** — this plugin only makes sense inside a Linux container.

### 1. Install

```sh
herdr plugin install bmingles/herdr-plugins/bridge-keepawake
```

### 2. Start it

The daemon starts itself whenever a Herdr server boots (`[[startup]]`), so it's live
the next time you start Herdr in the container. To start it right now without
restarting, focus a different workspace — that fires the `workspace.focused` hook that
starts it. It has to be a real focus change; re-focusing the one you're already on
emits no event.

### 3. Check it

Every command lives at one fixed path, which the daemon writes when it starts:

```sh
~/.local/state/herdr/plugins/bridge-keepawake/bridge-keepawake doctor
```

Look for `daemon : running (pid …)` and `devc-bridge ping : ok`. If the state file
doesn't exist yet, the daemon hasn't started — see
[When nothing happens](#when-nothing-happens).

> **Why the state directory and not the plugin directory?** A plugin installed from
> GitHub lives at `~/.config/herdr/plugins/github/bridge-keepawake-<hash>/…`, where the
> hash changes on reinstall. The state directory never moves, so the daemon keeps a
> one-line shim there pointing at wherever the plugin currently is — the same
> convention `agent-caffeinate` established. It's rewritten on every daemon start, so
> the path above keeps working after an upgrade.

## Status indicator (optional)

A `☕ keepawake` in the Herdr tab bar, shown while the plugin is pinging.

**This reflects local ping state, not host inhibitor state.** The container cannot
cheaply know whether the Mac is still awake — the host starts and stops `caffeinate` on
its own idle timer (`DEVC_BRIDGE_KEEPAWAKE_IDLE_MS`, 5 minutes by default), and asking it
would mean a subprocess and a round trip on every tab-bar refresh, which this indicator
never does. So `☕ keepawake` means "this plugin is pinging the bridge right now (or was,
within the last `indicatorHoldSec`)", not "the Mac is awake right now" — those two can
diverge by up to that idle window after the last ping.

Herdr's config file is `~/.config/herdr/config.toml`. **Create it if it does not exist**
(`herdr --default-config > ~/.config/herdr/config.toml` writes the fully commented
default — do this only if you have no config file yet, it overwrites). Add:

```toml
[ui]
tab_bar_right = [
  { type = "command", command = "~/.local/state/herdr/plugins/bridge-keepawake/bridge-keepawake indicator", interval_seconds = 5, timeout_seconds = 2 },
]
tab_bar_right_separator = " · "
```

Then `herdr server reload-config`. Paste that path verbatim — it is the same fixed shim
from step 3, and Herdr runs the entry through `/bin/sh -lc`, so `~` expands. Keep each
`{ … }` entry on **one line**: TOML inline tables cannot span newlines.

What it renders:

| Daemon | Renders |
| --- | --- |
| pinging, or last active within `indicatorHoldSec` | `☕ keepawake` |
| running, last ping failed (`lastPingOk: false`) | `⚠ keepawake` |
| running, idle beyond the hold | nothing — `--show-idle` renders `○ keepawake` |
| status file older than the staleness bound (wedged) | `⚠ keepawake` |
| not running, or no status file yet | nothing |

"Nothing" is a real state rather than a blank gap: Herdr clears the entry on empty
output, and separators appear only between *visible* entries.

`⚠ keepawake` for a failing ping is the one place this indicator says something a naive
"is the Mac awake" reading couldn't: a reachable daemon whose pings are being refused
means the bridge or the host is down, which is exactly when you'd ask why your Mac slept.

Flags: `--label TEXT` (default `keepawake`), `--icon GLYPH` for the holding state
(default `☕`), and `--show-idle` to also render `○ keepawake` while running but idle.

**Why the hold.** The plugin holds no state, so a raw "any pane active right now" read
would flicker the icon across the ~2s gap between a turn ending and the CLI printing its
next status line — see `indicatorHoldSec` in [Config](#config-optional). The daemon's own
behavior — what it pings, and when — is unaffected either way; the hold only smooths what
the tab bar shows.

**No socket call, no `devc-bridge` subprocess.** `indicator` reads `daemon.json` and the
pid file only, the same "must be cheap and must never block" constraint
`agent-caffeinate`'s indicator documents.

## Why Deno

This is the first non-Python plugin in this repo, and the first container-only one.
`herdr-plugins`' "Python 3.9+, standard library only, no build step" convention exists
to serve **host** users: cross-OS support and an install that needs no toolchain on a
machine we don't control. A devcontainer inverts both premises — one known OS, one
known image, and a toolchain we put there ourselves. Neither thing Python was buying
is being bought here.

Deno is already the house runtime: `devc`, `devc-core` and `devc-bridge` are all
written in it, it lives at `/usr/local/bin/deno` rather than behind `nvm` like `node`,
and it brings a test runner and permission flags with nothing extra to install.
(The stdlib gap is a footnote, not the argument: a typical devcontainer image ships
`python3-minimal`, missing `json`/`unittest`/`pty` — the existing plugins couldn't run
in a container even if we wanted them to.)

**No `[[build]]` block.** `herdr-plugin.toml`'s `[[startup]]` command runs
`bin/bridge-keepawake`, a shell launcher that execs `deno run` against source in the
plugin root directly — the same no-build-step property the Python plugins have, just
achieved with `deno run` instead of running a `.py` file in place. Whether Herdr's
`plugin install` even supports a `[[build]]` block invoking `deno compile` was
unverified when this plugin was built, so it deliberately doesn't depend on the
answer.

## Permissions, corrected

The plan this plugin was built from assumed it would need no `--allow-net` at all,
reasoning that every hop is a Unix socket or a subprocess. That turned out to be
wrong about Deno specifically, and it's worth stating precisely so the next person
doesn't rediscover it the hard way:

**`Deno.connect({ transport: "unix" })` requires `--allow-net` *and* `--allow-read`
*and* `--allow-write` on the socket path itself** — verified against this container's
Deno 2.9.5. All three, not just one. The fix keeps the spirit of "no network access"
intact: `--allow-net` is scoped to exactly the one socket
(`--allow-net=unix:$HERDR_SOCKET_PATH`), not a bare `--allow-net`, so the daemon still
cannot reach anything else on the network.

The other permission surprise: **`Deno.kill()` demands the *unscoped* `--allow-run`
flag, regardless of which signal is sent** — a scoped allowlist like
`--allow-run=kill` is not enough to authorize `Deno.kill()` itself. Rather than widen
`--allow-run` to "anything" just to send signals, this plugin shells out to the real
`kill(1)` binary for liveness probes and TERM/KILL, keeping `--allow-run` an explicit
list: `kill` and whatever `devc-bridge` resolves to. Nothing else.

Full grant, computed per-invocation in `bin/bridge-keepawake` from the environment:

```
--allow-read=<state dir>,<config dir>,<socket path>
--allow-write=<state dir>,<socket path>
--allow-net=unix:<socket path>
--allow-run=kill,devc-bridge
--allow-env
```

`deno task test` (used by the test suite, not the daemon) grants much broader
permissions than this — it needs to create temp directories, real unix sockets, and
spawn fake subprocesses anywhere, none of which the daemon itself needs at runtime.
Don't read the test task's permissions as the daemon's actual grant; `bin/bridge-keepawake`
is the source of truth for that.

## Singleton locking, and why it isn't in the Deno code

Whether a second `herdr plugin install`/session should be allowed to run a second
daemon against the same Herdr server needs an atomic "try to acquire, fail fast if
someone else already has it" primitive. `agent-caffeinate` gets this from
`fcntl.flock(fd, LOCK_EX | LOCK_NB)`. Deno's equivalent, `Deno.FsFile.lock()`, has
**no non-blocking mode** — verified: calling it when another process holds the lock
blocks the *caller* until the lock frees up, and closing the file while that call is
pending does not cancel it (the pending acquire is a leaked background operation that
can still fire later against a resource the caller has already moved on from). There is
no way, from Deno alone, to ask "is this free right now" without risking an indefinite
hang.

So locking lives in **`bin/bridge-keepawake`**, via the real `flock(1)` command:

```sh
setsid -f sh -c "exec flock -n '$LOCKFILE' $DENO_RUN daemon >>'$LOGFILE' 2>&1 </dev/null"
```

`flock -n` atomically tries a non-blocking exclusive lock and only runs the command if
it succeeds, holding the lock for that command's entire life and releasing it
automatically (kernel-level, crash-safe — a `kill -9` releases it too) when the
command exits for any reason. `setsid -f` is this script's counterpart to
agent-caffeinate's double-fork + `setsid` in Python (Deno has no `os.fork()`): it forks
and starts a new session before exec'ing, then returns immediately, so a hook that
calls `daemon` returns fast regardless of whether the lock was actually acquired.

One consequence: **a version of util-linux's `flock(1)` this container ships
(2.39.3) does not accept the `--` option-terminator its own `--help` output
advertises** — `flock -n LOCKFILE -- CMD` fails with
`flock: failed to execute --: No such file or directory`. The usage is
`flock [options] <file> <command> [<argument>...]`, no separator. Verified directly;
worth knowing before "fixing" this script's lack of one.

The plain `daemon` invocation and `daemon --ensure` (still accepted, for the manifest's
`[[events]]` block below) now behave **identically**: both attempt the same
`flock -n`-guarded start, both no-op silently if it's already held. **What's simplified
relative to `agent-caffeinate`:** a manual, non-hook `daemon` call no longer prints
"already running" when one already is — `flock -n` failing produces no output at all
by design. And **passive wedged-daemon takeover isn't automatic** the way
agent-caffeinate's staleness-based `_take_over` is: `flock` only ever tells you "alive
or dead" (a crashed process's lock releases itself), never "alive but hung". The
`restart` action is this plugin's whole answer to that case — it unconditionally
kills whatever pid is on record before attempting a fresh start, rather than trying to
detect staleness automatically:

```sh
~/.local/state/herdr/plugins/bridge-keepawake/bridge-keepawake daemon --restart
```

or the "Restart bridge-keepawake daemon" action from Herdr's UI.

## Config (optional)

`$HERDR_PLUGIN_CONFIG_DIR/config.json`, JSONC, every key optional and shown at its
default — see [`config.example.json`](config.example.json). No `idleGraceSec` and no
`inhibitorCommand`: unlike `agent-caffeinate`, this plugin holds no state and starts no
inhibitor of its own. Their absence is the design — adding either would be
re-implementing the host's job.

## Not depend on `pi-herdr` or `agent-caffeinate`

This plugin makes no code dependency on either. `pi-herdr` and this plugin both call
the same `herdr` surfaces independently — a code dependency would couple this to a
third-party release cadence for no gain. `agent-caffeinate`'s protocol module
(`src/sock.py`) is the **design** this plugin's `src/sock.ts` follows (same
request/reply shape, same `ServerGone`/`ProtocolError` split), reimplemented rather
than shared, because the two plugins run in different languages and serve different
machines.

## What was considered instead

- **Claude Code hooks alone.** They already do this, today (see
  `devc-tools/devc-bridge/README.md`'s "Wiring into Claude Code hooks"). But they cover
  one agent kind, need per-agent wiring, and know nothing about the other panes. This
  plugin covers **every** agent kind uniformly from one place — that's its whole added
  value over hooks alone.
- **A `[[events]]` hook on `pane.agent_status_changed`.** Rejected on measurement (see
  `docs/herdr-daemon-facts.md`): ~0.85 events/s per agent pane, ~8% of a core per
  agent for a no-op hook. A 2-second poll of `session.snapshot` costs ~0.02% of a
  core.

## When nothing happens

- **State file missing.** The daemon hasn't started yet — focus a different workspace,
  or start Herdr fresh.
- **`devc-bridge : NOT FOUND on --allow-run allowlist / PATH`** in `doctor`'s output.
  Either the `devc-bridge` Feature isn't installed in this container, or
  `bridgeCommand` in `config.json` points somewhere `bin/bridge-keepawake`'s
  `--allow-run` allowlist doesn't cover — see
  [Permissions, corrected](#permissions-corrected).
- **`devc-bridge ping : FAILED`.** The host bridge isn't reachable — check
  `/run/devc-bridge/token` exists (the mount from the host's `devc-bridge start`) and
  that the host's `devc-bridge` is actually running.
- **Host never wakes up / assertion never appears.** Confirm from `doctor` that a pane
  is actually reporting `working` while you expect it to, and that `last ping` in
  `status` says `ok`, not `FAILING`.
