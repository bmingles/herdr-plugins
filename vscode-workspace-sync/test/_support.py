"""Shared test helpers.

`src` is put on `sys.path` the same way `bin/sync` does it (the interpreter is handed
`src/main.py`, so `src` becomes `sys.path[0]`). Note that no module in `src` may be named
after a stdlib module for exactly this reason (a `types.py` there shadows stdlib `types`).
"""

import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(TEST_DIR)
SRC = os.path.join(PLUGIN_ROOT, "src")
FIXTURES = os.path.join(TEST_DIR, "fixtures")

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def fixture_path(name):
    return os.path.join(FIXTURES, name)


def fixture(name):
    with open(fixture_path(name), "r") as fh:
        return fh.read()


WORKSPACE_FIXTURES = (
    "example.code-workspace",
    "no-folders.code-workspace",
    "empty-root.code-workspace",
    "brackets.code-workspace",
    "block-comments.code-workspace",
    "four-space-indent.code-workspace",
    "vscode-written.code-workspace",
    "canonical.code-workspace",
    # Inbound-direction fixtures. They exist for `test_adopt`, but every one with a
    # valid top-level `folders` member is also a free shape for the tokenizer sweep --
    # a mixed-type array, and one whose entries are all relative paths.
    "adopt-basic.code-workspace",
    "adopt-relative.code-workspace",
    "adopt-messy.code-workspace",
)


class FakeConfig(object):
    """A `config.Config` stand-in for the pure folder-computation tests."""

    def __init__(self, mode="mirror", pinned_folders=()):
        self.mode = mode
        self.pinned_folders = list(pinned_folders)
        self.workspace_file = "/does/not/matter.code-workspace"
        self.source_path = "/does/not/matter/config.json"
        self.warnings = []
