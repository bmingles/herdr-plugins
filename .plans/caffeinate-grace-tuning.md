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

`blocked` is not an active status, so an agent waiting at a permission prompt also reads
as a gap. That is deliberate — nothing is in flight — but it means prompt-waits appear in
the data and must not be mistaken for false idles.

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
before it and never restarted (the daemon holds its settings from startup). Check that the
**installed** copy carries the change — `plugins.json` names the `plugin_root` actually in
use, which is not this repo — then `daemon --restart`. Say so and stop; do **not** analyse
an empty set and report a conclusion.

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

Read `daemon start` first: if the user changed the config mid-day there may be several,
and gaps must be interpreted against the grace that was in force at the time.

## Analysis

### 1. Every gap, ranked

```sh
grep -h 'idle -> working' ~/.local/state/herdr/plugins/agent-caffeinate/*/daemon.log* \
  | sed -E 's/^([0-9T:+-]+).*status ([^ ]+).*was idle for ([0-9.]+)s\)/\3\t\1\t\2/' \
  | sort -rn
```

Columns: gap seconds, when it ended, which pane.

### 2. Separate gaps the current setting survived from ones it did not

This is the classification that makes the data usable **without** asking the user what
they were doing:

- A gap **shorter than the grace in force** did not release the assertion. The inhibitor
  was held straight through, so the agent was protected. These are the interesting ones:
  each is a period where the agent read as idle but the machine correctly stayed awake.
- A gap **longer than the grace** caused a release, and there will be a matching
  `inhibitor stop reason=idle-grace` line just before the `idle -> working` line. Those
  are usually genuine idles — the user stepped away.

```sh
# Releases that actually happened, with how long the machine had been quiet
grep -h 'reason=idle-grace' ~/.local/state/herdr/plugins/agent-caffeinate/*/daemon.log*
```

### 3. The decision: what would 30 have broken?

Count gaps in **[30, 60)** — the band where lowering the default from 60 to 30 changes
behaviour. Every gap in that band is a moment the machine **would** have been allowed to
sleep under a 30 s default but was kept awake under 60.

```sh
grep -h 'idle -> working' ~/.local/state/herdr/plugins/agent-caffeinate/*/daemon.log* \
  | sed -E 's/.*was idle for ([0-9.]+)s\)/\1/' \
  | awk '$1 >= 30 && $1 < 60 { n++ } END { print (n ? n : 0), "gap(s) in [30,60)" }'
```

- **Zero gaps in [30, 60)** → 30 is safe on this evidence. Nothing observed would have
  behaved differently.
- **A handful** → each must be judged individually. Take its timestamp to the user and
  ask whether an agent was mid-task then. This is the one point where a human's memory is
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

Set the default to the **smallest round number comfortably above the largest gap
confirmed to be a false idle**, with a safety margin — at least 1.5x. If the evidence is
thin (few gaps, short day, no confirmed false idles), keep 60 and record that the data
was inconclusive rather than lowering on weak evidence.

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

**Fixed for run 2:** the transition lines moved from `debug` to `info` (`src/main.py`), so
a default-configured run collects them. Locked in by
`test_e2e.py::test_status_transitions_are_logged_at_info`. Step 4's flaw was found in the
same pass — see the caveat now in that section.

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

### Run 2 — blocked on the installed copy carrying the `info` change
- [ ] Confirm transition lines exist; stop and report if not
- [ ] Note the `grace=` value(s) from `daemon start` lines
- [ ] Produce the ranked gap list (step 1)
- [ ] Count and list gaps in `[30, 60)` (step 3)
- [ ] Cross-reference against `entries.jsonl` if the time tracker was running (step 4)
- [ ] For any ambiguous gap, ask the user what was happening at that timestamp
- [ ] Apply the decision rule and change `MACOS_INHIBITOR`'s neighbour default
      `Config.idle_grace_sec` in `agent-caffeinate/src/config.py` if the data supports it
- [ ] Update `config.example.json` if the default changed
- [ ] Record the observed numbers and the reasoning in `agent-caffeinate/README.md`,
      replacing the current "the default is 60 because..." justification
- [ ] Update `test_config.py::test_defaults_without_a_file` if the default changed
- [ ] Run the suite: `cd agent-caffeinate/test && python3 -m unittest discover -s .`
- [ ] Move this plan to `.plans/archived/` and update `.plans/PLAN.md`

## Validation

- [ ] The report to the user states **how many** gaps were observed, over **how long** a
      period — a conclusion from three gaps in one hour is not the same as thirty over a
      day, and must not be presented as though it were
- [ ] Every gap cited as a false idle is confirmed by the user, or by a transition line
      showing `idle` (not `blocked`) for the whole span. An `entries.jsonl` entry spanning
      the gap is **not** sufficient — see § 4
- [ ] If the default changed: `python3 -c "import sys; sys.path.insert(0,'src'); import config; print(config.Config().idle_grace_sec)"` prints the new value
- [ ] `cd agent-caffeinate/test && python3 -m unittest discover -s .` — all pass
- [ ] `./bin/agent-caffeinate doctor` shows the new `idleGraceSec` with no config file
- [ ] The README no longer justifies a number it does not use

## Relevant Files

| File | Change |
| --- | --- |
| `agent-caffeinate/src/config.py` | `Config.idle_grace_sec` default, if the data supports a change. |
| `agent-caffeinate/config.example.json` | The commented default. |
| `agent-caffeinate/README.md` | Replace the rationale with the measured numbers. **Done in run 1.** |
| `agent-caffeinate/src/main.py` | Transition lines at `info`, not `debug`. **Done in run 1.** |
| `agent-caffeinate/src/tracker.py` | `TransitionJournal` docstring records why `info`. **Done in run 1.** |
| `agent-caffeinate/test/test_e2e.py` | `test_status_transitions_are_logged_at_info`. **Done in run 1.** |
| `agent-caffeinate/test/test_config.py` | `test_defaults_without_a_file` asserts the default. |
| `.plans/PLAN.md` | Status + phase row. |
| `docs/herdr-daemon-facts.md` | Only if the run reveals something about detection behaviour worth recording beyond this decision. |
