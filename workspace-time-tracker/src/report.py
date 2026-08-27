"""Aggregating entries into something worth reading.

Days are **local** days, matching how the entries are written and how people think about
a working day. Entries never span a local day because the daemon splits them at
midnight, so filtering a range is a filter rather than a splitter.
"""

from datetime import datetime, timedelta

BY_LABEL = "label"
BY_WORKSPACE = "workspace"
BY_DAY = "day"
GROUPINGS = (BY_LABEL, BY_WORKSPACE, BY_DAY)

SCHEMA_VERSION = 1


def parse_ts(value):
    """Parse one of our own ISO-8601-with-offset timestamps."""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def parse_day(value, today=None):
    """`today` | `yesterday` | `YYYY-MM-DD` -> a `date`. Raises ValueError."""
    today = today or datetime.now().date()
    if value == "today":
        return today
    if value == "yesterday":
        return today - timedelta(days=1)
    return datetime.strptime(value, "%Y-%m-%d").date()


def entry_day(entry):
    started = parse_ts(entry.get("start"))
    return started.date() if started else None


def filter_entries(entries, since=None, until=None):
    """Entries whose local start day falls in [since, until], both inclusive."""
    out = []
    for entry in entries:
        day = entry_day(entry)
        if day is None:
            continue
        if since and day < since:
            continue
        if until and day > until:
            continue
        out.append(entry)
    return out


def _seconds(entry):
    value = entry.get("seconds")
    if isinstance(value, (int, float)):
        return int(value)
    start, end = parse_ts(entry.get("start")), parse_ts(entry.get("end"))
    if start and end:
        return int(round((end - start).total_seconds()))
    return 0


def has_overlap(entries):
    """Whether any two entries overlap in time.

    Two Herdr sessions can legitimately have different Spaces focused at once, so
    overlapping entries are real rather than a bug -- but a total that double-counts
    wall-clock time should say so.
    """
    spans = []
    for entry in entries:
        start, end = parse_ts(entry.get("start")), parse_ts(entry.get("end"))
        if start and end:
            spans.append((start, end))
    spans.sort()
    for i in range(1, len(spans)):
        if spans[i][0] < spans[i - 1][1]:
            return True
    return False


def _key(entry, by):
    if by == BY_WORKSPACE:
        return entry.get("workspace_id") or "?"
    if by == BY_DAY:
        day = entry_day(entry)
        return day.isoformat() if day else "?"
    return entry.get("label") or entry.get("workspace_id") or "?"


def summarise(entries, by=BY_LABEL, since=None, until=None):
    """The `--json` payload. This shape is the contract."""
    selected = filter_entries(entries, since, until)
    totals = {}
    counts = {}
    for entry in selected:
        key = _key(entry, by)
        totals[key] = totals.get(key, 0) + _seconds(entry)
        counts[key] = counts.get(key, 0) + 1

    if by == BY_DAY:
        ordered = sorted(totals)                       # chronological
    else:
        ordered = sorted(totals, key=lambda k: (-totals[k], k))   # biggest first

    return {
        "v": SCHEMA_VERSION,
        "range": {"since": since.isoformat() if since else None,
                  "until": until.isoformat() if until else None},
        "by": by,
        "groups": [{"key": k, "seconds": totals[k], "entries": counts[k]}
                   for k in ordered],
        "total_seconds": sum(totals.values()),
        "overlapping": has_overlap(selected),
    }


def format_duration(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return "%dh %02dm" % (hours, minutes)
    if minutes:
        return "%dm" % minutes
    return "%ds" % seconds


def render(summary, title=None):
    """The human-readable report. Deliberately plain; `--json` is for machines."""
    lines = []
    if title:
        lines.append(title)
    if not summary["groups"]:
        lines.append("  no entries")
        return "\n".join(lines) + "\n"

    width = max(len(g["key"]) for g in summary["groups"])
    width = min(max(width, 12), 40)
    for group in summary["groups"]:
        lines.append("  %-*s  %9s" % (width, group["key"][:width],
                                      format_duration(group["seconds"])))
    lines.append("  " + "-" * (width + 11))
    lines.append("  %-*s  %9s" % (width, "total",
                                  format_duration(summary["total_seconds"])))
    if summary["overlapping"]:
        lines.append("")
        lines.append("  note: some entries overlap (more than one Herdr session was")
        lines.append("        tracking at once), so the total exceeds wall-clock time.")
    return "\n".join(lines) + "\n"
