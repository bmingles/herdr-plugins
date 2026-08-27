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

The user runs `agent-caffeinate` at `"logLevel": "debug"` for a normal working day. The
procedure is in
[`agent-caffeinate/README.md`](../agent-caffeinate/README.md#choosing-idlegracesec-from-data-not-vibes)
— note it requires `daemon --restart`, since the daemon reads config only at startup.

**Before analysing, confirm the data is real:**

```sh
ls -la ~/.local/state/herdr/plugins/agent-caffeinate/*/daemon.log*
grep -c 'debug status' ~/.local/state/herdr/plugins/agent-caffeinate/*/daemon.log*
```

Zero `debug status` lines means debug logging was never on, or the daemon was not
restarted after the config change. Say so and stop — do **not** analyse an empty set and
report a conclusion.

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

### 4. Corroborate with the time tracker, if it is installed

`workspace-time-tracker` writes `entries.jsonl` with the Spaces that were active and when.
An `idle -> working` gap falling **inside** a tracked entry is far more likely to be a
false idle than one falling in a gap between entries. Independent evidence, and it does
not depend on anyone remembering their day.

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

## Checklist

- [ ] Confirm `debug status` lines exist; stop and report if not
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
- [ ] Every gap cited as a false idle is either confirmed by the user or corroborated by
      a `entries.jsonl` entry spanning it
- [ ] If the default changed: `python3 -c "import sys; sys.path.insert(0,'src'); import config; print(config.Config().idle_grace_sec)"` prints the new value
- [ ] `cd agent-caffeinate/test && python3 -m unittest discover -s .` — all pass
- [ ] `./bin/agent-caffeinate doctor` shows the new `idleGraceSec` with no config file
- [ ] The README no longer justifies a number it does not use

## Relevant Files

| File | Change |
| --- | --- |
| `agent-caffeinate/src/config.py` | `Config.idle_grace_sec` default, if the data supports a change. |
| `agent-caffeinate/config.example.json` | The commented default. |
| `agent-caffeinate/README.md` | Replace the rationale with the measured numbers. |
| `agent-caffeinate/test/test_config.py` | `test_defaults_without_a_file` asserts the default. |
| `.plans/PLAN.md` | Status + phase row. |
| `docs/herdr-daemon-facts.md` | Only if the run reveals something about detection behaviour worth recording beyond this decision. |
