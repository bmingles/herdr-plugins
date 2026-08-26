"""Read authoritative Space state from Herdr.

`herdr api snapshot` is the single source of truth: one call returns the focused
workspace id, the workspace records, **and** the pane records. Workspace records carry
no `cwd`, so each Space's directory comes from the pane join; see `src/types.py`.

Herdr is reached through `$HERDR_BIN_PATH` rather than the raw socket, per the plugin
docs' portability guidance, and by absolute path because the server's `PATH` is whatever
launched the server and is therefore unknowable.

`HERDR_VSCODE_SYNC_FAKE_SNAPSHOT=<path>` reads that file instead of executing Herdr.
This is a supported contract, not a test-only hack -- `--doctor` honours it too.
"""

import json
import os
import subprocess

from config import ENV_FAKE_SNAPSHOT


class HerdrError(Exception):
    """Herdr could not be reached, or returned something unusable."""


def herdr_bin(env=None):
    if env is None:
        env = os.environ
    return env.get("HERDR_BIN_PATH") or "herdr"


def snapshot_source(env=None):
    """Human-readable description of where the snapshot will come from."""
    if env is None:
        env = os.environ
    fake = env.get(ENV_FAKE_SNAPSHOT)
    if fake:
        return "fake snapshot file %s (%s)" % (fake, ENV_FAKE_SNAPSHOT)
    return "%s api snapshot" % herdr_bin(env)


def read_snapshot(env=None):
    """Return the parsed `.result.snapshot` object."""
    if env is None:
        env = os.environ
    fake = env.get(ENV_FAKE_SNAPSHOT)
    if fake:
        try:
            with open(fake, "r") as fh:
                text = fh.read()
        except (IOError, OSError) as exc:
            raise HerdrError("cannot read %s=%s: %s" % (ENV_FAKE_SNAPSHOT, fake, exc))
    else:
        cmd = [herdr_bin(env), "api", "snapshot"]
        try:
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(env)
            )
        except OSError as exc:
            raise HerdrError("cannot run %s: %s" % (" ".join(cmd), exc))
        if proc.returncode != 0:
            raise HerdrError(
                "%s exited %d: %s"
                % (
                    " ".join(cmd),
                    proc.returncode,
                    proc.stderr.decode("utf-8", "replace").strip(),
                )
            )
        text = proc.stdout.decode("utf-8", "replace")

    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise HerdrError("snapshot is not valid JSON: %s" % exc)
    if not isinstance(doc, dict):
        raise HerdrError("snapshot is not a JSON object")

    # Note the three envelope levels: id / result / snapshot.
    result = doc.get("result")
    if not isinstance(result, dict):
        raise HerdrError("snapshot has no .result object")
    snap = result.get("snapshot")
    if not isinstance(snap, dict):
        raise HerdrError(
            "snapshot has no .result.snapshot object (result.type=%r)" % result.get("type")
        )
    return snap


def plugin_context(env=None):
    """Parse `HERDR_PLUGIN_CONTEXT_JSON`, or `{}` when absent or malformed."""
    if env is None:
        env = os.environ
    raw = env.get("HERDR_PLUGIN_CONTEXT_JSON")
    if not raw:
        return {}
    try:
        ctx = json.loads(raw)
    except ValueError:
        return {}
    return ctx if isinstance(ctx, dict) else {}


def _pane_path(workspace_id, active_tab_id, panes):
    """Pick one pane's `cwd` deterministically for a Space.

    The pane whose `tab_id` matches the Space's `active_tab_id`, else the lowest
    `pane_id`. `foreground_cwd` is ignored -- it drifts more than `cwd` does.
    """
    mine = [p for p in panes if isinstance(p, dict) and p.get("workspace_id") == workspace_id]
    if not mine:
        return None
    if active_tab_id:
        for pane in sorted(mine, key=lambda p: str(p.get("pane_id") or "")):
            if pane.get("tab_id") == active_tab_id and pane.get("cwd"):
                return pane.get("cwd")
    for pane in sorted(mine, key=lambda p: str(p.get("pane_id") or "")):
        if pane.get("cwd"):
            return pane.get("cwd")
    return None


def reduce_snapshot(snap, context=None):
    """Reduce a snapshot to `(spaces, focused_workspace_id)`.

    `spaces` is in sidebar order -- array order is authoritative, verified against a
    reorder; `number` is a redundant 1-based confirmation of it.

    `context` is `HERDR_PLUGIN_CONTEXT_JSON`. Its `workspace_cwd` wins for the Space
    named in the hook; every other Space falls back to the pane join.

    It is **not** a stable root -- an earlier version of this docstring claimed it was.
    Measured: after `cd docs` in a Space's active pane, both `panes[].cwd` and
    `context.workspace_cwd` reported the new subdirectory. Herdr has no stored Space
    root to fall back on: `cwd` appears only in `workspace.create` across the whole
    socket API, so a Space's directory is *derived* live from its active pane and `cd`
    is the only mechanism that changes it after creation. Using context here is simply
    a direct read of that same value for the subject Space, not a stabler one.
    """
    context = context or {}
    workspaces = snap.get("workspaces") or []
    panes = snap.get("panes") or []
    focused_id = snap.get("focused_workspace_id")

    ctx_id = context.get("workspace_id")
    ctx_cwd = context.get("workspace_cwd")

    spaces = []
    for rec in workspaces:
        if not isinstance(rec, dict):
            continue
        wid = rec.get("workspace_id")
        if not wid:
            continue
        label = rec.get("label") or ""
        path = None
        if ctx_cwd and wid == ctx_id:
            path = ctx_cwd
        if not path:
            path = _pane_path(wid, rec.get("active_tab_id"), panes)
        spaces.append(Space(wid, label, path))
    return spaces, focused_id


def load_spaces(env=None):
    """Convenience: read the snapshot and reduce it in one step."""
    if env is None:
        env = os.environ
    snap = read_snapshot(env)
    return reduce_snapshot(snap, plugin_context(env))


# ---------------------------------------------------------------------------
# Observed Herdr JSON, 0.8.0 / protocol 19 (see docs/herdr-vscode-sync-facts.md).
#
#   {"id": ..., "result": {"type": "session_snapshot", "snapshot": {
#       "focused_workspace_id": "w4",
#       "workspaces": [{"workspace_id", "label", "number", "focused",
#                       "active_tab_id", "pane_count", "tab_count",
#                       "agent_status", "worktree"?}],   # <- NO "cwd"
#       "panes":      [{"pane_id", "workspace_id", "tab_id", "cwd",
#                       "foreground_cwd", "focused", ...}]}}}
#
# The workspace record carries no path: join panes -> workspaces on workspace_id.
# "worktree".checkout_path exists on some records but attaches lazily; do not use it.
# ---------------------------------------------------------------------------

class Space(object):
    """A Herdr Space reduced to what folder computation needs.

    ``path`` is resolved by :mod:`herdr` from the pane join or from the hook context;
    it may be ``None`` when no pane reported a cwd.
    """

    __slots__ = ("id", "label", "path")

    def __init__(self, id, label, path):
        # type: (str, str, Optional[str]) -> None
        self.id = id
        self.label = label
        self.path = path

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Space(id=%r, label=%r, path=%r)" % (self.id, self.label, self.path)

    def __eq__(self, other):
        if not isinstance(other, Space):
            return NotImplemented
        return (self.id, self.label, self.path) == (other.id, other.label, other.path)


class FolderEntry(object):
    """One rendered ``folders[]`` object: an absolute ``path`` and an optional ``name``."""

    __slots__ = ("path", "name")

    def __init__(self, path, name=None):
        # type: (str, Optional[str]) -> None
        self.path = path
        self.name = name

    def __repr__(self):  # pragma: no cover - debugging aid
        return "FolderEntry(path=%r, name=%r)" % (self.path, self.name)

    def __eq__(self, other):
        if not isinstance(other, FolderEntry):
            return NotImplemented
        return (self.path, self.name) == (other.path, other.name)
