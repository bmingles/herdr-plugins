"""Deciding whether a human is actually working in the focused Space.

Two signals, because neither alone is enough:

1. **Agent status.** Any pane in the focused Space reporting a status in
   `active_statuses` counts. Authoritative and free -- it comes from the same snapshot
   poll the tracker already makes.

2. **A screen hash of the focused pane.** Necessary because a plain shell emits *no
   Herdr events at all* on `cd` or command output (measured), so an event- or
   agent-driven tracker would call you idle while you work in a terminal for an hour.
   `panes[].revision` was the cheap candidate and does **not** work: it bumps on
   structural change such as cwd, not on output or keystrokes. Hashing
   `pane.read {source: "visible"}` does work -- it is stable across a quiet pane, and
   changes on both output and typing-without-enter. It costs 0.59 ms.

**Agent panes are never hashed.** An animating UI defeats the hash: with `top -d 1`
standing in for an agent's spinner, four samples over eight seconds produced four
different hashes for both `visible` and `detection`, so such a pane would read as
permanently active and its entry would never close. Panes with a detected agent have a
better signal available anyway -- their status -- so the hash is only used for plain
shells.

The residual false positive, documented rather than solved: a *plain* pane left running
`top`, `htop`, `watch` or a progress bar looks permanently active.
"""

import hashlib

UNKNOWN_STATUS = "unknown"


def pane_has_agent(pane):
    """Whether Herdr believes this pane is running an agent.

    A plain shell reports `agent_status: "unknown"` and carries no `agent`; a pane with
    a detected agent reports a real status. Either signal is enough.
    """
    if pane.get("agent"):
        return True
    status = pane.get("agent_status")
    return bool(status) and status != UNKNOWN_STATUS


def hash_text(text):
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


class ActivityProbe(object):
    """Samples the focused pane's screen, and remembers the last hash per pane."""

    __slots__ = ("read_text", "source", "tokens")

    def __init__(self, read_text, source="visible"):
        self.read_text = read_text          # (pane_id) -> str or None
        self.source = source
        self.tokens = {}

    def changed(self, pane_id):
        """True when this pane's screen differs from the last time we looked.

        The first sample of a pane establishes a baseline and reports no change: a
        newly-focused pane should not be counted as activity merely for existing (the
        focus change itself already counts).
        """
        try:
            text = self.read_text(pane_id)
        except Exception:
            return False
        if text is None:
            self.tokens.pop(pane_id, None)
            return False
        token = hash_text(text)
        previous = self.tokens.get(pane_id)
        self.tokens[pane_id] = token
        if previous is None:
            return False
        return token != previous

    def forget(self, pane_ids):
        """Drop panes that no longer exist, so the map cannot grow without bound."""
        for pane_id in list(self.tokens):
            if pane_id not in pane_ids:
                del self.tokens[pane_id]


def agent_activity(panes, workspace_id, active_statuses):
    """Pane ids in this Space whose agent status counts as activity."""
    hits = []
    for pane in panes:
        if pane.get("workspace_id") != workspace_id:
            continue
        if pane.get("agent_status") in active_statuses:
            hits.append(pane.get("pane_id"))
    return sorted(p for p in hits if p)
