"""Write the workspace file safely, and only when it needs writing."""

import os
import tempfile

import rewrite

WROTE = "wrote"
UNCHANGED = "unchanged"


def read_text(path):
    with open(path, "r") as fh:
        return fh.read()


def desired_pairs(entries):
    return [(os.path.normpath(e.path), e.name) for e in entries]


def is_unchanged(text, entries, workspace_dir):
    """Would writing `entries` change anything meaningful?

    The comparison is over **resolved** paths, not raw text. VS Code writes newly added
    folder paths relative to the workspace file's directory, so after any UI folder-add
    the file holds relative paths that are equal-but-not-identical to the plugin's
    absolute ones -- a pure text compare would rewrite the file on every run forever.
    Falls back to a text compare only when the existing array cannot be parsed.
    """
    existing = rewrite.resolved_existing_folders(text, workspace_dir)
    if existing is not None:
        return existing == desired_pairs(entries)
    return rewrite.splice_folders(text, entries) == text


def atomic_write(target, text):
    """Replace `target`'s contents atomically, preserving its mode.

    `os.replace` (not `shutil.move`) is what guarantees the rename is atomic on the same
    filesystem, so VS Code's watcher sees exactly one event.
    """
    directory = os.path.dirname(target) or "."
    try:
        mode = os.stat(target).st_mode
    except OSError:
        mode = None
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".vscode-workspace-sync.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sync_file(target, entries, log=None):
    """Bring `target`'s `folders` into line with `entries`.

    Returns `"wrote"` or `"unchanged"`. `target` must already exist -- the caller checks
    that and fails loudly, because a typo in config must never silently create a stray
    workspace file.
    """
    # Resolve symlinks on the target itself so a symlinked workspace file is replaced
    # *through* the link rather than having the link clobbered.
    real = os.path.realpath(target)
    text = read_text(real)
    # Relative entries are resolved against the *configured* directory, not the
    # realpath'd one: that is the directory VS Code itself resolves against, and it
    # keeps a symlinked path (macOS `/var` -> `/private/var`) from looking changed.
    workspace_dir = os.path.dirname(os.path.abspath(target))

    if is_unchanged(text, entries, workspace_dir):
        return UNCHANGED

    new_text = rewrite.splice_folders(text, entries)
    if new_text == text:
        return UNCHANGED

    atomic_write(real, new_text)
    return WROTE
