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
