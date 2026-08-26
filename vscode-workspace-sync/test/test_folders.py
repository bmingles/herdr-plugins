import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support  # noqa: E402

import folders as folders_mod  # noqa: E402
import herdr  # noqa: E402
from herdr import FolderEntry, Space  # noqa: E402


def always_dir(path):
    return True


def only(*paths):
    allowed = set(paths)
    return lambda p: p in allowed


class TestResolvePath(unittest.TestCase):
    def test_strips_trailing_separator(self):
        self.assertEqual(folders_mod.resolve_path("/a/b/"), "/a/b")
        self.assertEqual(folders_mod.resolve_path("/a/b//"), "/a/b")

    def test_normalises_without_resolving_symlinks(self):
        self.assertEqual(folders_mod.resolve_path("/a/./b/../c"), "/a/c")
        # /tmp is a symlink to /private/tmp on macOS; realpath would rewrite it and
        # surprise a user who configured /tmp. resolve_path must not.
        self.assertEqual(folders_mod.resolve_path("/tmp"), "/tmp")

    def test_expands_tilde(self):
        self.assertEqual(
            folders_mod.resolve_path("~/x"), os.path.join(os.path.expanduser("~"), "x")
        )

    def test_root_survives(self):
        self.assertEqual(folders_mod.resolve_path("/"), "/")


class TestComputeFolders(unittest.TestCase):
    def spaces(self):
        return [
            Space("w1", "devc-wksp", "/x/devc-wksp"),
            Space("w4", "herdr-plugins", "/x/herdr-plugins"),
            Space("w6", "renamed", "/x/docs"),
            Space("w5", "devc-wksp", "/x/devc-wksp"),
        ]

    def test_mirror_keeps_sidebar_order(self):
        entries = folders_mod.compute_folders(
            self.spaces(), "w4", _support.FakeConfig(), isdir=always_dir
        )
        self.assertEqual([e.path for e in entries],
                         ["/x/devc-wksp", "/x/herdr-plugins", "/x/docs"])

    def test_dedupe_first_occurrence_wins(self):
        entries = folders_mod.compute_folders(
            self.spaces(), "w4", _support.FakeConfig(), isdir=always_dir
        )
        self.assertEqual(len(entries), 3)  # w5 duplicates w1's path

    def test_name_emitted_only_when_label_differs_from_basename(self):
        entries = folders_mod.compute_folders(
            self.spaces(), "w4", _support.FakeConfig(), isdir=always_dir
        )
        by_path = dict((e.path, e.name) for e in entries)
        self.assertIsNone(by_path["/x/devc-wksp"])     # label == basename
        self.assertIsNone(by_path["/x/herdr-plugins"])  # label == basename
        self.assertEqual(by_path["/x/docs"], "renamed")  # label != basename

    def test_missing_path_dropped(self):
        spaces = self.spaces() + [Space("w9", "no-pane", None)]
        entries = folders_mod.compute_folders(
            spaces, "w4", _support.FakeConfig(), isdir=always_dir
        )
        self.assertNotIn(None, [e.path for e in entries])
        self.assertEqual(len(entries), 3)

    def test_nonexistent_directory_dropped(self):
        entries = folders_mod.compute_folders(
            self.spaces(), "w4", _support.FakeConfig(),
            isdir=only("/x/herdr-plugins"),
        )
        self.assertEqual([e.path for e in entries], ["/x/herdr-plugins"])

    def test_pinned_folders_come_first_and_get_no_name(self):
        cfg = _support.FakeConfig(pinned_folders=["/pin/one", "/pin/two"])
        entries = folders_mod.compute_folders(
            self.spaces(), "w4", cfg, isdir=always_dir
        )
        self.assertEqual([e.path for e in entries][:2], ["/pin/one", "/pin/two"])
        self.assertEqual([e.name for e in entries][:2], [None, None])

    def test_pinned_nonexistent_dropped(self):
        cfg = _support.FakeConfig(pinned_folders=["/pin/gone"])
        entries = folders_mod.compute_folders(
            self.spaces(), "w4", cfg, isdir=only("/x/docs")
        )
        self.assertEqual([e.path for e in entries], ["/x/docs"])

    def test_active_mode_is_focused_space_only(self):
        cfg = _support.FakeConfig(mode="active")
        entries = folders_mod.compute_folders(
            self.spaces(), "w6", cfg, isdir=always_dir
        )
        self.assertEqual([e.path for e in entries], ["/x/docs"])
        self.assertEqual(entries[0].name, "renamed")

    def test_active_mode_plus_pinned(self):
        cfg = _support.FakeConfig(mode="active", pinned_folders=["/pin/one"])
        entries = folders_mod.compute_folders(
            self.spaces(), "w6", cfg, isdir=always_dir
        )
        self.assertEqual([e.path for e in entries], ["/pin/one", "/x/docs"])

    def test_active_mode_with_no_focused_space(self):
        cfg = _support.FakeConfig(mode="active")
        self.assertEqual(
            folders_mod.compute_folders(self.spaces(), None, cfg, isdir=always_dir), []
        )

    def test_empty_space_list_is_empty(self):
        self.assertEqual(
            folders_mod.compute_folders([], None, _support.FakeConfig(),
                                        isdir=always_dir),
            [],
        )


class TestReduceSnapshot(unittest.TestCase):
    def snap(self, name="snapshot.json"):
        with open(_support.fixture_path(name)) as fh:
            return json.load(fh)["result"]["snapshot"]

    def test_reads_the_snapshot_level(self):
        env = {"HERDR_VSCODE_SYNC_FAKE_SNAPSHOT": _support.fixture_path("snapshot.json")}
        snap = herdr.read_snapshot(env)
        self.assertEqual(snap["protocol"], 19)
        self.assertEqual(snap["focused_workspace_id"], "w4")

    def test_missing_snapshot_level_is_an_error(self):
        import tempfile

        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            # The `workspace list` envelope: same records, no `snapshot` level.
            fh.write('{"id":"x","result":{"type":"workspace_list","workspaces":[]}}')
        try:
            with self.assertRaises(herdr.HerdrError):
                herdr.read_snapshot({"HERDR_VSCODE_SYNC_FAKE_SNAPSHOT": path})
        finally:
            os.unlink(path)

    def test_order_is_array_order(self):
        spaces, focused = herdr.reduce_snapshot(self.snap())
        self.assertEqual([s.id for s in spaces], ["w1", "w4", "w6", "w5", "w7"])
        self.assertEqual(focused, "w4")

    def test_path_comes_from_the_pane_join(self):
        spaces, _ = herdr.reduce_snapshot(self.snap())
        by_id = dict((s.id, s) for s in spaces)
        self.assertEqual(by_id["w1"].path, "/Users/bingles/code/spikes/devc-wksp")
        self.assertEqual(by_id["w7"].path,
                         "/Users/bingles/.herdr/worktrees/herdr-plugins/probe-x")

    def test_active_tab_pane_wins_over_lowest_pane_id(self):
        spaces, _ = herdr.reduce_snapshot(self.snap())
        by_id = dict((s.id, s) for s in spaces)
        # w4 has two panes: w4:p1 (tab w4:t1, .../docs) and w4:pC (active tab w4:t3,
        # the repo root). The active_tab_id match must win even though w4:p1 sorts first.
        self.assertEqual(by_id["w4"].path, "/Users/bingles/code/tools/herdr-plugins")

    def test_context_workspace_cwd_wins_for_the_event_subject(self):
        context = {"workspace_id": "w1", "workspace_cwd": "/stable/root"}
        spaces, _ = herdr.reduce_snapshot(self.snap(), context)
        by_id = dict((s.id, s) for s in spaces)
        self.assertEqual(by_id["w1"].path, "/stable/root")
        # Every other Space still comes from the pane join.
        self.assertEqual(by_id["w5"].path, "/Users/bingles/code/spikes/devc-wksp")

    def test_worktree_checkout_path_is_never_used(self):
        snap = self.snap()
        # Strip w7's pane so only worktree.checkout_path could supply a path.
        snap["panes"] = [p for p in snap["panes"] if p["workspace_id"] != "w7"]
        spaces, _ = herdr.reduce_snapshot(snap)
        by_id = dict((s.id, s) for s in spaces)
        self.assertIsNone(by_id["w7"].path)

    def test_labels_survive_verbatim_including_duplicates(self):
        spaces, _ = herdr.reduce_snapshot(self.snap())
        labels = [s.label for s in spaces]
        self.assertEqual(labels.count("devc-wksp"), 2)

    def test_empty_snapshot(self):
        spaces, focused = herdr.reduce_snapshot(self.snap("snapshot-empty.json"))
        self.assertEqual(spaces, [])
        self.assertIsNone(focused)

    def test_end_to_end_against_the_portable_fixture(self):
        env = {"HERDR_VSCODE_SYNC_FAKE_SNAPSHOT":
               _support.fixture_path("snapshot-portable.json")}
        spaces, focused = herdr.load_spaces(env)
        entries = folders_mod.compute_folders(spaces, focused, _support.FakeConfig())
        # /usr/share once (deduped), /usr/lib named, /nonexistent dropped.
        self.assertEqual([(e.path, e.name) for e in entries],
                         [("/usr/share", None), ("/usr/lib", "the-libs")])


if __name__ == "__main__":
    unittest.main()
