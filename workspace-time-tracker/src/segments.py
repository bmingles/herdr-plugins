"""When a time entry opens, when it closes, and what timestamp it closes at.

Pure: no I/O, and the clock is injected, so backdating, the short-entry discard and the
midnight rollover are all tested deterministically instead of by waiting.

The rules, and why:

- **An idle close is backdated to the last activity.** Closing at "now" would silently
  add `idle_timeout_sec` of fiction to every entry that ends by going quiet -- which is
  most of them. The dead time is never counted.
- **A switch closes at now.** Switching Spaces is itself evidence you were present, so
  the entry runs up to the switch. This can over-count by at most `idle_timeout_sec`
  (walk away for 50s, come back, switch), which is the deliberate trade for not
  under-counting normal work.
- **Entries shorter than `min_entry_sec` are discarded.** Paging through five Spaces
  looking for one should not leave five entries behind.
- **Midnight splits an entry in two.** Keeping every entry inside one calendar day makes
  reporting a filter rather than a splitter.
"""

import time
from datetime import datetime, timedelta

SWITCH = "switch"
IDLE = "idle"
CLOSED = "closed"
ROLLOVER = "rollover"
SHUTDOWN = "shutdown"
RECOVERED = "recovered"

SCHEMA_VERSION = 1


def local_day(ts):
    """The calendar day a timestamp falls in, as `YYYY-MM-DD` in local time."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def iso(ts):
    """Local time with a UTC offset, second precision -- `datetime.fromisoformat` safe."""
    return datetime.fromtimestamp(ts).astimezone().replace(
        microsecond=0).isoformat(timespec="seconds")


def end_of_day(ts):
    """The last instant of `ts`'s local day, as an epoch timestamp."""
    start = datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0,
                                               microsecond=0)
    return (start + timedelta(days=1)).timestamp() - 1.0


def start_of_next_day(ts):
    start = datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0,
                                               microsecond=0)
    return (start + timedelta(days=1)).timestamp()


class Segment(object):
    """One open interval of attention on one Space."""

    __slots__ = ("workspace_id", "label", "cwd", "start", "last_activity")

    def __init__(self, workspace_id, label, cwd, start):
        self.workspace_id = workspace_id
        self.label = label
        self.cwd = cwd
        self.start = start
        self.last_activity = start

    def to_state(self):
        return {"workspace_id": self.workspace_id, "label": self.label,
                "cwd": self.cwd, "start": self.start,
                "last_activity": self.last_activity}

    @classmethod
    def from_state(cls, state):
        seg = cls(state["workspace_id"], state.get("label"), state.get("cwd"),
                  float(state["start"]))
        seg.last_activity = float(state.get("last_activity", seg.start))
        return seg


def make_entry(segment, end_ts, reason, session, host):
    """The on-disk record. This shape is the plugin's contract -- see the README."""
    entry = {
        "v": SCHEMA_VERSION,
        "workspace_id": segment.workspace_id,
        "label": segment.label,
        "start": iso(segment.start),
        "end": iso(end_ts),
        "seconds": int(round(end_ts - segment.start)),
        "end_reason": reason,
        "session": session,
        "host": host,
    }
    if segment.cwd:                       # omitted, never null, when unknown
        entry["cwd"] = segment.cwd
    return entry


class SegmentTracker(object):
    """Turns a stream of (focused workspace, was-there-activity) into closed entries."""

    __slots__ = ("idle_timeout_sec", "min_entry_sec", "clock", "session", "host",
                 "current", "discarded")

    def __init__(self, idle_timeout_sec, min_entry_sec, session="default", host="",
                 clock=time.time):
        self.idle_timeout_sec = float(idle_timeout_sec)
        self.min_entry_sec = float(min_entry_sec)
        self.clock = clock
        self.session = session
        self.host = host
        self.current = None
        self.discarded = 0

    # -- helpers -----------------------------------------------------------------

    def _finish(self, end_ts, reason):
        """Close the open segment at `end_ts`. Returns a list of 0 or 1 entries."""
        segment = self.current
        self.current = None
        if segment is None:
            return []
        if end_ts < segment.start:
            end_ts = segment.start
        # A zero-length entry is noise whatever `min_entry_sec` says.
        if end_ts <= segment.start:
            self.discarded += 1
            return []
        if (end_ts - segment.start) < self.min_entry_sec:
            self.discarded += 1
            return []
        return [make_entry(segment, end_ts, reason, self.session, self.host)]

    def _open(self, workspace, at):
        self.current = Segment(workspace["workspace_id"], workspace.get("label"),
                               workspace.get("cwd"), at)

    def idle_for(self):
        if self.current is None:
            return None
        return self.clock() - self.current.last_activity

    def seconds_until_idle_close(self):
        if self.current is None:
            return None
        return max(0.0, self.idle_timeout_sec - self.idle_for())

    # -- the state machine -------------------------------------------------------

    def update(self, workspace, active):
        """Advance by one poll.

        `workspace` is `{workspace_id, label, cwd}` for the focused Space, or None.
        `active` says whether anything counted as activity since the last call.
        Returns the entries closed by this call (0, 1 or 2).
        """
        now = self.clock()
        out = []

        # 1. Quiet for too long: close, backdated to the last sign of life.
        if self.current is not None and \
                (now - self.current.last_activity) >= self.idle_timeout_sec:
            out += self._finish(self.current.last_activity, IDLE)

        # 2. Midnight: split so no entry spans two calendar days. Loops in case the
        #    machine slept through a whole day.
        while self.current is not None and \
                local_day(now) != local_day(self.current.start):
            boundary = end_of_day(self.current.start)
            resume_at = start_of_next_day(self.current.start)
            carried = self.current
            out += self._finish(boundary, ROLLOVER)
            self._open({"workspace_id": carried.workspace_id,
                        "label": carried.label, "cwd": carried.cwd}, resume_at)
            self.current.last_activity = max(carried.last_activity, resume_at)

        # 3. The focused Space changed, or went away.
        if self.current is not None:
            if workspace is None:
                out += self._finish(now, CLOSED)
            elif workspace["workspace_id"] != self.current.workspace_id:
                out += self._finish(now, SWITCH)
                self._open(workspace, now)
            else:
                # Keep label and cwd fresh: a rename mid-segment uses the new name.
                self.current.label = workspace.get("label", self.current.label)
                if workspace.get("cwd"):
                    self.current.cwd = workspace["cwd"]

        # 4. Nothing open: activity on a focused Space starts a new entry.
        if self.current is None and workspace is not None and active:
            self._open(workspace, now)

        if active and self.current is not None:
            self.current.last_activity = now

        return out

    def close(self, reason=SHUTDOWN):
        """Close whatever is open, at the last activity. Used on shutdown."""
        if self.current is None:
            return []
        return self._finish(self.current.last_activity, reason)

    def recover(self, state):
        """Adopt a segment left behind by a crashed daemon and close it out.

        The daemon mirrors its open segment to disk after every activity update, so a
        `kill -9` loses at most one poll interval rather than the whole entry.
        """
        try:
            segment = Segment.from_state(state)
        except (KeyError, TypeError, ValueError):
            return []
        self.current = segment
        return self._finish(segment.last_activity, RECOVERED)
