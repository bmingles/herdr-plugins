# caffeinate-grace-tuning

Decide `agent-caffeinate`'s default `idleGraceSec` from measured data instead of
judgement. Was **60**; the open question was whether **30** is safe.

**CLOSED 2026-08-28 — the answer is no, and 60 stays.** The observed false-idle ceiling is
**22.1 s**; 30 clears it on observed events but gives only 1.36x margin from six samples in
three hours, below this plan's 1.5x rule, and being generous costs essentially nothing. See
`## Run 2` for the numbers and `## Run 1` for the instrument bug that made the first attempt
worthless. The method and tooling below are kept because they are the reusable part —
`tools/gap-report.py` answers this question again from any future log.

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

### Run 2 — 2026-08-28, complete. **60 stands.**
Daemons were restarted onto the `info` build at 2026-08-27 18:15:34, and no restart has
happened since, so the whole window is one continuous measurement at `grace=60`.

- [x] Confirm transition lines exist — 139 across three logs
- [x] Note the `grace=` value(s) and restarts — 60.0 s throughout, zero restarts in-window
- [x] Run `tools/gap-report.py` — 55 per-pane gaps
- [x] Sanity-check the window: **~3 h active** (56 min on 08-27 evening + 2 h 01 m on 08-28
      morning; the 17 h clock span is mostly overnight), **1 h 33 m assertion-held** across
      17 spans, 9% duty cycle, 4 panes, 3 sessions, **all Claude Code**
- [x] Read the `[30, 60)` band — **empty, because nothing is there.** Not thin data: no gap
      of *any* kind (false idle, prompt-wait or absence) falls between 22.1 s and 214.9 s
- [x] Identify the largest sub-grace idle-only gap — **22.1 s**, `w9:p5`, ended
      2026-08-28 10:53:19, composition `done:10s idle:12s`, with 26 s of work immediately
      before and 52 s immediately after. That is the floor.
- [x] Ambiguous gaps in the band — none to ask about; the band is empty
- [x] Apply the decision rule — **no change. `idle_grace_sec` stays 60.**
- [x] Default unchanged, so `config.example.json` / `test_config.py` untouched
- [x] Run 2's numbers folded into `README.md`, replacing run 1's
- [x] Suite: 95 tests pass
- [x] Plan archived and `PLAN.md` updated

**The result, in full.** Excluding `blocked` prompt-waits and the 2 s poll floor, 16 gaps
were idle-only and the distribution is bimodal with a three-minute canyon:

| | count | range |
| --- | --- | --- |
| Detection blips — false idles, held straight through | 6 | 6.0 – **22.1 s** |
| Human absence — released the assertion | 10 | 214.9 s – 3.3 h |
| In between | **0** | — |

All six false idles sat between real working spans, so each is confirmed by composition and
context rather than by anyone's memory — which is what the `blocked` split and the
transition lines were added to make possible.

**Why not 30, given an empty band.** Nothing observed would have broken under 30: it still
covers 22.1 s. But 30 is 1.36x a ceiling estimated from **six** samples over **three
hours**, below this plan's own 1.5x margin (22.1 × 1.5 = 33.2). The margin exists for the
tail that was not sampled, and three hours does not pin a tail. Since being generous costs
essentially nothing, 60 is kept at 2.7x. **45** (2.0x) is the value the data would support
if a shorter grace is ever wanted; 30 is not, at this sample size.

**Run 1's two 62 s spans, resolved.** They were per-*session* release/re-hold pairs of
unknown composition. Run 2's per-pane data shows nothing at all in `[60, 120)`, while
`blocked` prompt-waits do occur and run long (one was 727 s). They were almost certainly
prompt-waits, not false idles. No longer a reason for concern.

**If this is ever reopened**, the one thing that would change the answer is a full working
day showing the 22 s ceiling holds — that would justify 45, or 30 as a deliberate override.
Nothing else in the method needs revisiting.

## Validation

- [x] The report to the user states **how many** gaps were observed, over **how long** a
      period — reported as 55 gaps over ~3 h *active* (1 h 33 m held), explicitly correcting
      the 17 h clock span, which is mostly overnight
- [x] Every gap cited as a false idle is confirmed by the user, or is idle-only
      (`idle`/`done` throughout, **no** `blocked` leg) **and** sub-grace — all six qualify,
      and each was additionally shown sandwiched between real working spans. No
      `entries.jsonl` evidence was used
- [x] No conclusion rests on a gap at or below the ~4 s poll floor — 39 of the 55 were
      excluded on that basis or as prompt-waits
- [x] Any gap that spanned a daemon restart is reported as unmeasurable — zero restarts
      in-window, so none
- [x] An empty `[30, 60)` band is reported explicitly as **"nothing there"**, evidenced by
      the 22.1 s → 214.9 s canyon being empty for *all* gap types
- [x] Default unchanged, so no `Config.idle_grace_sec` assertion to update
- [x] `cd agent-caffeinate/test && python3 -m unittest discover -s .` — 85 pass
- [x] `./bin/agent-caffeinate doctor` shows `idleGraceSec: 60` with no config file
- [x] The README no longer justifies a number it does not use — run 1's per-session spans
      replaced by run 2's per-pane distribution
- [x] `python3 tools/gap-report.py` runs clean against the real logs, and against a
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
