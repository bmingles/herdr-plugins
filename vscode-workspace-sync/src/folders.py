"""Compute the desired `folders` array. Pure, and the heart of the plugin.

    candidates = configured pinnedFolders, in order
               + mirror: every Space in sidebar order
                 active: the focused Space only (empty if none)

then, in this order: resolve -> exists -> dedupe -> name.
"""

import os

from config import MODE_ACTIVE
from herdr import FolderEntry


def resolve_path(path):
    """Absolutise and normalise, **without** resolving symlinks.

    `os.path.realpath` is deliberately not used: on macOS it would rewrite `/tmp/x` to
    `/private/tmp/x` and surprise a user who configured the former. `normpath` also
    strips any trailing separator, except on the filesystem root.
    """
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


def compute_folders(spaces, focused_id, config, isdir=None):
    """Return the ordered list of :class:`FolderEntry` the workspace file should hold.

    `spaces` is in sidebar order. An empty result is meaningful and is the caller's cue
    to write nothing -- see the safety valve in `main.py`.
    """
    if isdir is None:
        isdir = os.path.isdir

    if config.mode == MODE_ACTIVE:
        chosen = [s for s in spaces if focused_id is not None and s.id == focused_id]
    else:
        chosen = list(spaces)

    entries = []
    seen = set()

    # Pinned folders first, in configured order. Never excluded, never named.
    for raw in config.pinned_folders:
        path = resolve_path(raw)
        if not isdir(path):
            continue
        if path in seen:
            continue
        seen.add(path)
        entries.append(FolderEntry(path, None))

    for space in chosen:
        if not space.path:
            continue
        path = resolve_path(space.path)
        if not isdir(path):
            continue
        if path in seen:
            continue
        seen.add(path)
        entries.append(FolderEntry(path, folder_name(space, path)))

    return entries


def folder_name(space, path):
    """The `name` to emit for a Space, or `None`.

    Only when the label is non-empty **and** differs from the path's basename. Herdr
    auto-derives a Space's label from its directory basename, so that test usually fails
    and `name` is rarely emitted -- only for Spaces the user explicitly labelled, or
    worktree Spaces whose label is the branch. That is intended, not a bug.
    """
    label = space.label or ""
    if not label:
        return None
    if label == os.path.basename(path):
        return None
    return label
