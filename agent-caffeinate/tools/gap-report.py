#!/usr/bin/env python3
"""Rank the gaps between one agent leaving `working` and returning to it.

Reads agent-caffeinate's daemon logs and reconstructs each pane's status timeline from
the transition lines. A "gap" is one pane's span from leaving `working` to returning to
it; its *composition* -- which statuses it passed through -- is what classifies it:

    blocked anywhere  -> prompt-wait. The human was the bottleneck, not detection.
    idle/done only    -> detection *may* have lost a working agent. Necessary but not
                         sufficient: a 900 s idle-only gap is somebody at lunch.

Read the `SUB-GRACE` flag alongside. A gap shorter than the grace was held straight
through -- the agent read as not-working while the machine correctly stayed awake -- and
those are the ones that set the floor for `idleGraceSec`.

Expect `done`, not `idle`. Herdr's `done` is "idle whose tab has not been seen in the
focused UI", and CLI/socket reads do not mark a tab seen -- so for a daemon that only
polls the socket, `done` is the normal status of a finished agent and `idle` the
exception. Anything grepping for `idle -> working` alone finds nothing.

Per-pane, not per-session, on purpose: idleGraceSec has to cover a single agent's false
idle even when it is the only agent running, so a gap masked by another pane still counts.

Usage: tools/gap-report.py [log-glob]
"""

import datetime as dt
import glob
import os
import re
import sys

ACTIVE = "working"
DEFAULT_LOG_GLOB = os.path.expanduser(
    "~/.local/state/herdr/plugins/agent-caffeinate/*/daemon.log*")

RE_APPEARED = re.compile(r"status (\S+) appeared as (\S+)")
RE_CHANGE = re.compile(r"status (\S+) (\S+) -> (\S+) \(was \S+ for ([0-9.]+)s\)")
RE_VANISHED = re.compile(r"status (\S+) vanished while (\S+) \(after ([0-9.]+)s\)")
RE_GRACE = re.compile(r"daemon start .* grace=([0-9.]+)s")
RE_RELEASE = re.compile(r"inhibitor stop .* reason=idle-grace")


def parse(path):
    """Yield (timestamp, kind, payload) in file order."""
    for raw in open(path):
        raw = raw.rstrip("\n")
        if not raw.strip():
            continue
        try:
            stamp, rest = raw.split(" ", 1)
            when = dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            continue
        m = RE_GRACE.search(rest)
        if m:
            yield when, "daemon-start", float(m.group(1))
            continue
        if RE_RELEASE.search(rest):
            yield when, "release", None
            continue
        m = RE_CHANGE.search(rest)
        if m:
            yield when, "change", (m.group(1), m.group(2), m.group(3),
                                   float(m.group(4)))
            continue
        m = RE_APPEARED.search(rest)
        if m:
            yield when, "appeared", (m.group(1), m.group(2))
            continue
        m = RE_VANISHED.search(rest)
        if m:
            yield when, "vanished", (m.group(1), m.group(2), float(m.group(3)))


class Gap(object):
    def __init__(self, session, pane, start, grace):
        self.session, self.pane, self.start, self.grace = session, pane, start, grace
        self.legs = []          # [(status, seconds)]
        self.released = False
        self.end = None

    @property
    def seconds(self):
        return sum(s for _, s in self.legs)

    @property
    def statuses(self):
        return [k for k, _ in self.legs]

    @property
    def kind(self):
        """Composition test only -- necessary, nowhere near sufficient.

        `blocked` anywhere means the human was the bottleneck: not a detection error.
        `idle`/`done` only means detection *might* have lost a working agent -- or the
        human simply walked away. Read `seconds` and `released` alongside; a 900 s
        idle-only gap that released the assertion is somebody at lunch, not a false idle.
        """
        if "blocked" in self.statuses:
            return "prompt-wait"
        if set(self.statuses) <= {"idle", "done"}:
            return "idle-only"
        return "mixed"

    @property
    def sub_grace(self):
        """Held straight through: detection lost the agent, the machine stayed awake.

        These set the floor for idleGraceSec -- each is a false idle the current setting
        already survives, and lowering below one re-opens it.
        """
        return self.grace is not None and self.seconds < self.grace

    @property
    def at_poll_floor(self):
        """Indistinguishable from a single-poll blip; ignore at the bottom of the list."""
        return self.seconds <= 4.0

    def __str__(self):
        legs = " ".join("%s:%.0fs" % (k, s) for k, s in self.legs)
        flags = []
        if self.sub_grace:
            flags.append("SUB-GRACE")
        if self.at_poll_floor:
            flags.append("poll-floor")
        return "%7.1fs  %-11s %-4s %-9s %-14s %-22s %s" % (
            self.seconds, self.kind, "REL" if self.released else "held",
            self.pane, self.session, legs, ",".join(flags))


def collect(log_glob):
    gaps, spanning_restart = [], 0
    for path in sorted(glob.glob(log_glob)):
        session = os.path.basename(os.path.dirname(path))
        grace, open_gaps = None, {}
        for when, kind, payload in parse(path):
            if kind == "daemon-start":
                # The journal's memory dies with the process: any gap in flight can no
                # longer be measured, and the pane reappears with no prior status.
                spanning_restart += len(open_gaps)
                open_gaps.clear()
                grace = payload
            elif kind == "release":
                for g in open_gaps.values():
                    g.released = True
            elif kind == "change":
                pane, prev, now, held = payload
                if prev == ACTIVE:
                    open_gaps[pane] = Gap(session, pane, when, grace)
                elif pane in open_gaps:
                    open_gaps[pane].legs.append((prev, held))
                    if now == ACTIVE:
                        g = open_gaps.pop(pane)
                        g.end = when
                        gaps.append(g)
            elif kind == "vanished":
                pane, prev, held = payload
                g = open_gaps.pop(pane, None)
                if g is not None:
                    g.legs.append((prev, held))   # never returned; incomplete
    return gaps, spanning_restart


def main(argv):
    log_glob = argv[1] if len(argv) > 1 else DEFAULT_LOG_GLOB
    paths = sorted(glob.glob(log_glob))
    if not paths:
        print("No logs matched %s" % log_glob)
        return 1
    gaps, spanning_restart = collect(log_glob)
    if not gaps:
        print("No completed gaps found. Either no agent has left and re-entered "
              "`working` yet, or the running daemon predates the `info` transition "
              "lines -- check `plugins.json` for the plugin_root actually in use, then "
              "`daemon --restart` per session socket.")
        return 1

    span = (max(g.end for g in gaps) - min(g.start for g in gaps))
    print("%d completed gaps over %s, %d pane(s), %d session(s)"
          % (len(gaps), span, len({g.pane for g in gaps}),
             len({g.session for g in gaps})))
    if spanning_restart:
        print("%d gap(s) unmeasurable: spanned a daemon restart" % spanning_restart)
    graces = sorted({g.grace for g in gaps if g.grace})
    print("grace in force: %s" % ", ".join("%gs" % x for x in graces))
    print()

    print("%7s  %-11s %-4s %-9s %-14s %-22s %s"
          % ("secs", "kind", "assn", "pane", "session", "composition", "flags"))
    for g in sorted(gaps, key=lambda g: -g.seconds):
        print(g)

    cands = [g for g in gaps if g.kind == "idle-only" and not g.at_poll_floor]
    print()
    print("idle-only gaps above the poll floor: %d of %d "
          "(the rest are `blocked` prompt-waits, mixed, or single-poll blips)"
          % (len(cands), len(gaps)))
    for lo, hi in ((0, 10), (10, 30), (30, 60), (60, 120), (120, 10 ** 9)):
        n = len([g for g in cands if lo <= g.seconds < hi])
        print("  [%d,%s): %d" % (lo, hi if hi < 10 ** 9 else "inf", n))
    print()
    held = [g for g in cands if g.sub_grace]
    print("held straight through the grace -- FALSE IDLES THE CURRENT SETTING "
          "SURVIVES: %d" % len(held))
    if held:
        worst_held = max(held, key=lambda g: g.seconds)
        print("  largest: %.1fs (%s, ended %s). This is the floor: do not set "
              "idleGraceSec below it." % (worst_held.seconds, worst_held.pane,
                                          worst_held.end.strftime("%Y-%m-%d %H:%M:%S")))
    band = sorted([g for g in cands if 30 <= g.seconds < 60],
                  key=lambda g: -g.seconds)
    print()
    print("THE DECISION BAND -- candidates in [30,60), where 30 differs from 60: %d"
          % len(band))
    for g in band:
        print("  %.1fs  %s  %s  ended %s"
              % (g.seconds, g.pane, g.session, g.end.strftime("%Y-%m-%d %H:%M:%S")))
    print()
    print("Each gap in the band above is a moment a 30 s default would have allowed "
          "sleep\nand 60 did not. Take its timestamp to the user: was an agent "
          "mid-task then?\nA long idle-only gap that released the assertion is "
          "somebody away from the desk,\nnot a detection failure -- do not count it.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
