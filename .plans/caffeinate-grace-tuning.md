# caffeinate-grace-tuning

Decide `agent-caffeinate`'s default `idleGraceSec` from measured data instead of
judgement. Currently **60**; the open question is whether **30** is safe.

This plan exists to be picked up by an agent with **no prior context**, after the user
has run the plugin for a day. Everything needed is here or linked.

## The question, precisely

`idleGraceSec` is how long the machine stays awake after the last agent stops reporting
`working`. Choosing it is **not** a preference about how soon sleep should happen — it is
a bound on a measurement error.

Claude Code's status comes from screen-detection rules, and those rules include one named
`default_known_agent_idle_fallback`: *identity known, no rule matched*. So `idle` is an
absence of evidence, not evidence the agent stopped. A working agent that renders a screen
none of the ~16 rules cover reports `idle` while still working — a **false idle**.

- If `idleGraceSec` **exceeds** the longest false-idle gap: the machine never sleeps
  mid-task. Cost of being too generous is near zero — the plugin holds `caffeinate -i -s`,
  not `-d`, so the display still sleeps and locks on schedule, and all that is delayed is
  the start of macOS's own multi-minute idle countdown.
- If it is **shorter**: the machine can sleep during a task. That is the exact failure the
  plugin exists to prevent.

Asymmetric costs, so the default should sit comfortably above the observed maximum.

### The status vocabulary — read this before writing any grep

Herdr's enum is `idle | working | blocked | done | unknown`
(`docs/herdr-research-notes.md`). Only `working` is in `activeStatuses`, so **all four
others count down the grace**:

| Status | What it means | In gap analysis |
| --- | --- | --- |
| `working` | Agent is working | Holds the assertion. A gap is the span between two of these. |
| `idle` | Agent idle, tab **has** been seen in the focused UI | Genuine idle, or a false idle via `default_known_agent_idle_fallback` |
| `done` | Agent idle, tab has **not** been seen | Same ambiguity as `idle`. **This is the common case** — see below |
| `blocked` | Waiting on a human | A prompt-wait. Deliberately not active; **not** a detection error |
| `unknown` | No agent detected in this pane | A plain shell. Not an agent gap at all |

**`done`, not `idle`, is what this plugin actually sees.** Herdr's `done` is "idle whose
tab has not been seen in the focused UI, and **CLI reads do not mark a tab seen**". The
daemon only ever reads over the socket, so it never marks anything seen: a finished agent
in a tab you have not personally clicked into stays `done`. Run 2's first minutes bore
this out — every observed gap was `done`, none was `idle`. Any query matching only
`idle -> working` finds **nothing**. Match `-> working` instead.

**`blocked` is the free win.** It is the one status that positively identifies a
prompt-wait, so the transition lines separate "the human was the bottleneck" from "detection
lost a working agent" **without asking anyone what they were doing**. Run 1 could not do
this, which is why it leaned on `entries.jsonl` and got a useless answer.

**The poll floor.** `pollIntervalSec` is 2, so no gap is measurable finer than 2 s and
every brief real gap reads as exactly `2.0s`. Ignore gaps at or below ~4 s; they carry no
information.

## Prerequisite: collect the data

The user runs `agent-caffeinate` for a normal working day. No configuration is needed —
the per-pane status transitions are logged at `info`, which is the default level. (They
were at `debug` until run 1 proved that unworkable; see `## Run 1` below.)

**Before analysing, confirm the data is real:**

```sh
ls -la ~/.local/state/herdr/plugins/agent-caffeinate/*/daemon.log*
grep -ch 'status .* -> ' ~/.local/state/herdr/plugins/agent-caffeinate/*/daemon.log*
```

Zero transition lines means the running daemon predates the `info` change, or was started
before it and never restarted (the daemon holds its code and settings from startup).

**The repo is not what runs.** `plugins.json` names the `plugin_root` actually in use — a
GitHub-managed copy under `~/.config/herdr/plugins/github/` pinned to a commit. Confirm the
*installed* file carries the change before blaming the daemon:

```sh
grep -n 'log.info(line)' ~/.config/herdr/plugins/github/agent-caffeinate-*/agent-caffeinate/src/main.py
```

**There is one daemon per Herdr session, and each needs its own restart.** The registered
`restart` action is `global` context but runs only against the session that invokes it.
Sessions come and go — three existed on 2026-08-27 (`default`, `vscode`, `devc-tools`) where
run 1 saw two — so enumerate the sockets rather than hardcoding them:

```sh
BIN=~/.config/herdr/plugins/github/agent-caffeinate-e3f912ff634f/agent-caffeinate/bin/agent-caffeinate
for s in ~/.config/herdr/herdr.sock ~/.config/herdr/sessions/*/herdr.sock; do
  HERDR_SOCKET_PATH="$s" "$BIN" daemon --restart          # silent on success
done
for s in ~/.config/herdr/herdr.sock ~/.config/herdr/sessions/*/herdr.sock; do
  HERDR_SOCKET_PATH="$s" "$BIN" status | head -3
done
```

`--restart` also *starts* a daemon that was not running, so it doubles as the way to pick
up a session that appeared later. Note each session gets its own log directory keyed by a
hash of its socket path, so the analysis glob must stay a glob.

Say so and stop; do **not** analyse an empty set and report a conclusion.

## Log format

One line per event: `<iso8601> <level> <message>`. The lines that matter:

| Line | Meaning |
| --- | --- |
| `status <pane> appeared as <s>` | First time this pane was seen |
| `status <pane> <a> -> <b> (was <a> for N s)` | A status change, and how long the previous one lasted |
| `status <pane> vanished while <s> (after N s)` | Pane closed |
| `inhibitor start pid=P argv=... trigger=<pane>` | The assertion was taken |
| `inhibitor stop pid=P reason=idle-grace idle_for=N s` | Released after the grace |
| `inhibitor stop pid=P reason=shutdown` | Released because the daemon or server stopped |
| `server gone (...); releasing and exiting` | Herdr server died |
| `daemon start ... grace=N s ...` | **The grace in force for the lines that follow** |
| `signal N received` / `daemon exit pid=P` | The daemon was stopped — almost always a restart |

Read `daemon start` first. There will normally be **several** per log, because every
`daemon --restart` writes one; a mid-day config change also writes one, and gaps must be
interpreted against the grace that was in force at the time.

**A restart destroys the journal's memory.** `TransitionJournal` keeps its per-pane
"status since" map in process memory, so after a restart every pane reappears with an
`appeared as <s>` line and no prior status. Any gap in flight across the restart is
**unmeasurable** — the report counts these and prints the count; do not try to bridge
them by hand. This also means a fresh restart yields no gaps at all until agents have
left and re-entered `working`, which is why run 2 needs a normal working *day*, not an
hour.

## Analysis

### 0. Run the report

`agent-caffeinate/tools/gap-report.py` does steps 1–3. It reconstructs each pane's status
timeline from the transition lines, so it handles `done`, the `blocked` split, daemon
restarts and the poll floor — all of which a grep gets wrong.

```sh
cd agent-caffeinate && python3 tools/gap-report.py
# or against a specific log / an archived copy:
python3 tools/gap-report.py "$HOME/.local/state/herdr/plugins/agent-caffeinate/*/daemon.log*"
```

It prints, in order: how many gaps over how long a period; how many were unmeasurable
because they spanned a daemon restart; the grace(s) in force; every gap ranked by duration
with its composition and flags; the `[30,60)` decision band; and the largest sub-grace
idle-only gap, which is the floor.

Read the sections below to interpret it — **do not** just paste its output at the user.

### 1. Every gap, ranked

A **gap** is one pane leaving `working` and returning to it. Per-pane, not per-session:
`idleGraceSec` has to cover a single agent's false idle even when that agent is the only
one running, so a gap masked by another pane still working still counts. (Run 1 measured
per-*session* spans from inhibitor release/re-hold pairs, because it had nothing better.
Run 2's numbers are per-pane and are **not** directly comparable to run 1's below 60 s.)

The `composition` column is the point — `done:44s blocked:210s` is a prompt-wait, and
`done:44s` alone is not.

### 2. Classify, without asking the user anything

Two independent axes, and you need both:

**Composition** — does the gap contain a `blocked` leg?
- **Yes** → prompt-wait. The human was the bottleneck. Not a detection error, not
  evidence about `idleGraceSec`. Discard.
- **No** (`idle`/`done` only) → detection *may* have lost a working agent. Necessary but
  nowhere near sufficient: a 900 s idle-only gap is somebody at lunch.

**Whether the assertion survived it** — the `SUB-GRACE` flag.
- **Sub-grace** (shorter than the grace in force) → the inhibitor was **held straight
  through**. The agent read as not-working and the machine correctly stayed awake anyway.
  **These are the false idles that set the floor.** The largest one is the number that
  matters; lowering `idleGraceSec` below it re-opens a gap that is currently covered.
- **Longer than the grace** → there is a matching `inhibitor stop reason=idle-grace` line
  (the `REL` column) and the machine was released. Usually a genuine idle: the user
  stepped away. A long idle-only gap is not promoted to a false idle just for lacking a
  `blocked` leg.

### 3. The decision: what would 30 have broken?

The band is **[30, 60)** — idle-only gaps in there are moments a 30 s default would have
allowed sleep and 60 did not. The report prints the band explicitly.

- **Zero in the band, from a full day with a healthy gap count** → 30 is safe on this
  evidence.
- **Zero in the band, but few gaps overall or a short observation window** → inconclusive.
  Keep 60. An empty band from thin data is not the same finding, and run 1 is the cautionary
  tale: its band read zero because the instrument could not see into it at all.
- **A handful** → each is judged individually. Take its timestamp to the user and ask
  whether an agent was mid-task then. This is the one point where a human's memory is
  genuinely required; do not guess.
- **Many** → keep 60, and say why.

### 4. Corroborate with the time tracker — but read the caveat first

**This step does not do what it was written to do.** Run 1 established why, from
`workspace-time-tracker/src/activity.py`:

- the tracker's `active_statuses` also default to `["working"]`, so its agent signal is
  the *same* signal `agent-caffeinate` uses — not an independent one; and
- it **never screen-hashes agent panes** (deliberately: a spinner defeats the hash).

So during a caffeinate gap, when no pane is `working`, a tracker entry can only be kept
alive by a plain shell's changing screen or by a focus change. An entry spanning a gap is
therefore evidence **the human was at the keyboard**, not that the agent was working —
and a human at the keyboard answering a permission prompt is exactly the `blocked` case
this plan warns must not be counted as a false idle. It is weak circumstantial evidence at
best, and it points the wrong way as often as the right one.

Use the transition lines instead: they name the status, so `idle` and `blocked` are
distinguishable. Run the query below only for context on what was in focus, and never
promote a spanning entry to a confirmed false idle on its own.

`workspace-time-tracker` writes `entries.jsonl` with the Spaces that were active and when.

```sh
python3 -c "
import json, glob
for path in glob.glob('$HOME/.local/state/herdr/plugins/workspace-time-tracker/entries.jsonl'):
    for line in open(path):
        if line.strip():
            e = json.loads(line)
            print(e['start'], e['end'], e['seconds'], e['label'], e['end_reason'])
"
```

Caveat: an entry can be kept alive by the user typing in a plain shell, so an entry
spanning a gap is suggestive, not proof.

## Decision rule

Set the default to the **smallest round number comfortably above the largest gap confirmed
to be a false idle**, with a safety margin — at least 1.5x. "Confirmed" means either the
user says an agent was mid-task at that timestamp, or the gap is idle-only **and**
sub-grace (held straight through, so detection demonstrably lost an agent the machine was
still protecting).

If the evidence is thin — few gaps, short window, no confirmed false idles — keep 60 and
record that the data was inconclusive. **Do not lower on an empty decision band alone.**

**Run 1 raised the bar for lowering.** It observed two 62 s spans where the assertion was
released on the 60 s deadline and re-taken on the very next poll. Those were per-session
spans and their composition is unknown, so they do not by themselves prove anything — but
they mean 60 was already operating near its edge twice in five hours. Lowering to 30 needs
positive evidence that nothing lives in `[30, 60)`, not merely an absence of evidence that
something does.

Whatever is chosen, the reasoning and the observed numbers go into the README so the next
person does not re-litigate it.

## Run 1 — 2026-08-27, inconclusive (instrument not armed)

**The measurement did not happen.** `logLevel` was the default `info` and the transition
lines were at `debug`, so both daemon logs held zero of them. The tuning question is
**still open**; the default stays at **60**.

What the `info` lines did show, over 11:48:54–16:52:34 (~5 h 04 m) across two Herdr
sessions (`961cd3a7037f` default socket, `387573b5e65e` vscode session socket), `grace=60`
throughout with no mid-day change:

- 28 grace releases; 27 followed by an agent working again, giving 27 measurable
  "nothing working for this long, then working" spans.
- Distribution: `[60,70)` **2**, `[70,90)` 2, `[90,120)` 4, `[120,300)` 10, `[300,∞)` 9.
- The two in `[60,70)` were both **62 s** — released on the 60 s deadline, re-taken on the
  next 2 s poll (13:58:24→13:59:26 and 14:00:04→14:01:06, session `961cd3a7037f`).

**The trap this run exposed.** Step 3's grep would have printed `0 gap(s) in [30,60)` and
that would have looked like a green light for 30. It is not: a gap *shorter* than the
grace produces **no log line at all** at `info`, so the entire decision band is invisible.
Zero there was the absence of the instrument, not evidence.

**Fixed for run 2**, all in the same pass:

- Transition lines moved from `debug` to `info` (`src/main.py`), so a default-configured
  run collects them. Locked in by
  `test_e2e.py::test_status_transitions_are_logged_at_info`, which was verified to fail
  when reverted.
- `tools/gap-report.py` added — it *is* steps 1–3, and handles the four things a grep gets
  wrong (`done`, the `blocked` split, restart boundaries, the poll floor).
- The `done` discovery: a socket-only reader sees `done`, essentially never `idle`, so run
  1's planned `grep 'idle -> working'` would have returned zero gaps in run 2 as well.
  Recorded in `docs/herdr-daemon-facts.md` § C4 and in the status-vocabulary section above.
- Step 4's `entries.jsonl` corroboration found unsound — see the caveat in that section.
- Restart is per-session-socket, and there are three sessions now where run 1 saw two;
  command in the prerequisite.

**Read the 62 s pair before lowering anything.** Twice in five hours the grace was already
marginal. Whether those two were false idles or `blocked` prompt-waits is exactly what run
2's transition lines will say.

## Checklist

### Run 1 — done
- [x] Confirm transition lines exist; stop and report if not — **zero found, reported**
- [x] Note the `grace=` value(s) from `daemon start` lines — 60.0 s, both sessions
- [x] Produce the ranked gap list (step 1) — 27 spans, from release/re-hold pairs
- [x] Count and list gaps in `[30, 60)` (step 3) — **unmeasurable at `info`**; 2 at 62 s
- [x] Cross-reference against `entries.jsonl` (step 4) — done, and found unsound; see § 4
- [x] Move the transition lines to `info` so run 2 can measure the decision band
- [x] Record run 1's numbers and the `60` rationale in `agent-caffeinate/README.md`

### Run 2 — the instrument is armed; needs a normal working day
Daemons were restarted onto the `info` build at 2026-08-27 18:15, so the clock starts
there. Everything before that timestamp has no transition lines and cannot be analysed.

- [ ] Confirm transition lines exist (`tools/gap-report.py` says so, or reports why not);
      stop and report if not
- [ ] Note the `grace=` value(s) and how many restarts split the log
- [ ] Run `tools/gap-report.py`; sanity-check the gap count and observation window are
      enough to conclude anything at all
- [ ] Read the `[30, 60)` band, and state whether it is empty *because nothing is there*
      or *because the data is thin* — these are different findings
- [ ] Identify the largest sub-grace idle-only gap; that is the floor
- [ ] For any ambiguous gap in the band, ask the user what was happening at that timestamp
- [ ] Apply the decision rule and change `Config.idle_grace_sec` in
      `agent-caffeinate/src/config.py` **only** if the data supports it
- [ ] If the default changed: `config.example.json`, `README.md`'s "Why the default is 60",
      and `test_config.py::test_defaults_without_a_file`
- [ ] Either way: fold run 2's observed numbers into `README.md`, replacing run 1's
- [ ] Run the suite: `cd agent-caffeinate/test && python3 -m unittest discover -s .`
- [ ] Move this plan to `.plans/archived/` and update `.plans/PLAN.md`

## Validation

- [ ] The report to the user states **how many** gaps were observed, over **how long** a
      period — a conclusion from three gaps in one hour is not the same as thirty over a
      day, and must not be presented as though it were
- [ ] Every gap cited as a false idle is confirmed by the user, or is idle-only
      (`idle`/`done` throughout, **no** `blocked` leg) **and** sub-grace. An
      `entries.jsonl` entry spanning the gap is **not** sufficient — see § 4
- [ ] No conclusion rests on a gap at or below the ~4 s poll floor
- [ ] Any gap that spanned a daemon restart is reported as unmeasurable, not bridged
- [ ] An empty `[30, 60)` band is reported as "nothing there" or "data too thin" —
      explicitly, one or the other, never left ambiguous
- [ ] If the default changed: `python3 -c "import sys; sys.path.insert(0,'src'); import config; print(config.Config().idle_grace_sec)"` prints the new value
- [ ] `cd agent-caffeinate/test && python3 -m unittest discover -s .` — all pass
- [ ] `./bin/agent-caffeinate doctor` shows the new `idleGraceSec` with no config file
- [ ] The README no longer justifies a number it does not use
- [ ] `python3 tools/gap-report.py` runs clean against the real logs, and against a
      non-matching glob (prints "No logs matched", exit 1)

## Relevant Files

| File | Change |
| --- | --- |
| `agent-caffeinate/src/config.py` | `Config.idle_grace_sec` default, if the data supports a change. |
| `agent-caffeinate/config.example.json` | The commented default. |
| `agent-caffeinate/README.md` | Replace the rationale with the measured numbers. **Done in run 1.** |
| `agent-caffeinate/src/main.py` | Transition lines at `info`, not `debug`. **Done in run 1.** |
| `agent-caffeinate/src/tracker.py` | `TransitionJournal` docstring records why `info`. **Done in run 1.** |
| `agent-caffeinate/test/test_e2e.py` | `test_status_transitions_are_logged_at_info`. **Done in run 1.** |
| `agent-caffeinate/tools/gap-report.py` | The analysis itself — steps 1–3. **Added in run 1.** Extend here rather than writing new greps. |
| `agent-caffeinate/test/test_config.py` | `test_defaults_without_a_file` asserts the default. |
| `.plans/PLAN.md` | Status + phase row. |
| `docs/herdr-daemon-facts.md` | The `done`-vs-`idle` consequence for socket-only readers. **Recorded in run 1.** Add anything further run 2 reveals about detection. |
