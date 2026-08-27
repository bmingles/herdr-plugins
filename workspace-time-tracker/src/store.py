"""Where entries live, and how a crashed daemon's open segment is recovered.

`entries.jsonl` sits in `$HERDR_PLUGIN_STATE_DIR`, which is keyed on plugin id and
therefore **shared across Herdr sessions** -- which is what we want, since the report
should cover all your work, not one server's. Each line records its `session` so the
origin is never lost.

Appends are a single `write()` of one line to a fd opened `O_APPEND`. Two daemons
appending concurrently can interleave *lines* but cannot corrupt one, which is the
property that matters for an append-only log read back later.
"""

import json
import os

ENTRIES_FILENAME = "entries.jsonl"
CURRENT_FILENAME = "current.json"


def entries_path(state_dir, env=None):
    override = (env or os.environ).get("HERDR_TRACK_ENTRIES_PATH")
    if override:
        return override
    return os.path.join(state_dir, ENTRIES_FILENAME)


def append_entry(path, entry):
    """Append one entry as one line. Returns the line written."""
    line = json.dumps(entry, sort_keys=True) + "\n"
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return line


def read_entries(path, on_bad_line=None):
    """Every well-formed entry, in file order. Malformed lines are reported and skipped.

    A half-written line from a killed process must never make the whole report
    unreadable, so parsing is per line and tolerant.
    """
    out = []
    try:
        with open(path, "r") as fh:
            for number, raw in enumerate(fh, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    if on_bad_line:
                        on_bad_line(number, raw)
                    continue
                if isinstance(entry, dict) and "start" in entry and "end" in entry:
                    out.append(entry)
                elif on_bad_line:
                    on_bad_line(number, raw)
    except (IOError, OSError):
        return []
    return out


def current_path(session_dir):
    return os.path.join(session_dir, CURRENT_FILENAME)


def write_current(path, state):
    """Mirror the open segment. Atomic, so a crash never leaves a torn file."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except (IOError, OSError):
        return False
    return True


def read_json(path):
    """Any JSON object this plugin wrote, or None. No shape validation."""
    try:
        with open(path, "r") as fh:
            payload = json.load(fh)
    except (IOError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def read_current(path):
    """The mirrored open segment specifically -- validated as such."""
    try:
        with open(path, "r") as fh:
            state = json.load(fh)
    except (IOError, OSError, ValueError):
        return None
    return state if isinstance(state, dict) and "start" in state else None


def clear_current(path):
    try:
        os.unlink(path)
    except OSError:
        pass
