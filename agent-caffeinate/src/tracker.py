"""The active/idle decision, as a pure state machine.

No I/O and no clock of its own -- `clock` is injected -- so every timing rule here is
testable without sleeping. `main` owns the polling; this owns the meaning of the
statuses it collects.

The rule: while any pane reports a status in `active_statuses`, the inhibitor is held.
When none does, a grace period starts, and the inhibitor is released once it elapses.
Any pane going active again before then cancels the release without touching the
inhibitor at all.

`blocked` is deliberately not an active status by default: it means the agent is waiting
on a human, so no work is in flight and there is nothing to protect.
"""

import time

START = "start"
STOP = "stop"


class TransitionJournal(object):
    """Observes per-pane status changes and how long each status lasted.

    Purely diagnostic -- nothing here influences the inhibitor. It exists to answer one
    question with data instead of guesswork: **how long does a working agent falsely
    read as idle?** Claude's detection has a documented
    `default_known_agent_idle_fallback` rule -- "identity known, no rule matched" -- so
    `idle` is an absence of evidence, not evidence of absence. `idleGraceSec` has to be
    longer than the longest such gap, and the log lines this emits make those gaps
    directly greppable:

        status w4:p2 idle -> working (was idle for 8.4s)

    The `was idle for Ns` on a return to `working` *is* the false-idle gap.

    These are logged at **info**, deliberately. At `debug` the measurement only existed
    when someone remembered to turn it on, and the first real multi-hour run recorded 28
    grace releases and not one gap duration.
    """

    __slots__ = ("clock", "since")

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.since = {}

    def observe(self, statuses):
        """Return a list of human-readable change lines for this poll."""
        now = self.clock()
        lines = []
        for pane_id in sorted(statuses):
            status = statuses[pane_id]
            known = self.since.get(pane_id)
            if known is None:
                self.since[pane_id] = (status, now)
                lines.append("status %s appeared as %s" % (pane_id, status))
                continue
            previous, started = known
            if previous != status:
                lines.append("status %s %s -> %s (was %s for %.1fs)"
                             % (pane_id, previous, status, previous, now - started))
                self.since[pane_id] = (status, now)
        for pane_id in sorted(set(self.since) - set(statuses)):
            previous, started = self.since.pop(pane_id)
            lines.append("status %s vanished while %s (after %.1fs)"
                         % (pane_id, previous, now - started))
        return lines


class Tracker(object):
    """Decides when the inhibitor should be running.

    `update()` is fed the whole `{pane_id: agent_status}` map on every poll rather than
    individual transitions. That is what makes a missed event impossible: the map *is*
    the truth, so a pane that vanishes while it was last seen `working` simply stops
    counting on the next poll instead of stranding the inhibitor forever.
    """

    __slots__ = ("active_statuses", "idle_grace_sec", "clock", "statuses",
                 "idle_since", "holding")

    def __init__(self, active_statuses, idle_grace_sec, clock=time.monotonic):
        self.active_statuses = frozenset(active_statuses)
        self.idle_grace_sec = float(idle_grace_sec)
        self.clock = clock
        self.statuses = {}
        self.idle_since = None
        self.holding = False

    def active_panes(self):
        """Pane ids currently counting as active, sorted for stable logging."""
        return sorted(p for p, s in self.statuses.items() if s in self.active_statuses)

    def update(self, statuses):
        """Feed one poll's worth of state. Returns START, STOP, or None."""
        self.statuses = dict(statuses)
        now = self.clock()

        if self.active_panes():
            self.idle_since = None
            if not self.holding:
                self.holding = True
                return START
            return None

        if self.idle_since is None:
            self.idle_since = now
        if self.holding and (now - self.idle_since) >= self.idle_grace_sec:
            self.holding = False
            return STOP
        return None

    def idle_for(self):
        """Seconds since the last active pane, or None while something is active."""
        if self.idle_since is None:
            return None
        return self.clock() - self.idle_since

    def seconds_until_stop(self):
        """Seconds until a STOP would fire, or None when no stop is pending."""
        if not self.holding or self.idle_since is None:
            return None
        return max(0.0, self.idle_grace_sec - (self.clock() - self.idle_since))

    def next_wakeup(self, poll_interval_sec):
        """How long the loop may sleep: the poll interval, or sooner if a stop is due."""
        pending = self.seconds_until_stop()
        if pending is None:
            return poll_interval_sec
        return max(0.0, min(poll_interval_sec, pending))
