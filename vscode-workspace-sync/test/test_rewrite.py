import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support  # noqa: E402

import jsonc  # noqa: E402
import rewrite  # noqa: E402
import write as write_mod  # noqa: E402
from herdr import FolderEntry  # noqa: E402

ONE = [FolderEntry("/abs/one")]
TWO = [FolderEntry("/abs/one"), FolderEntry("/abs/two", "api")]


def split_around_folders(text):
    """`(before, folders_value, after)` for a text with a top-level `folders` member."""
    member = jsonc.find_top_level_member(text, "folders")
    assert member is not None, "fixture has no folders member"
    return (
        text[: member.value_start],
        text[member.value_start : member.value_end],
        text[member.value_end :],
    )


class TestRenderFolders(unittest.TestCase):
    def test_exact_rendering(self):
        self.assertEqual(
            rewrite.render_folders(TWO, ""),
            '[\n'
            '  { "path": "/abs/one" },\n'
            '  { "path": "/abs/two", "name": "api" }\n'
            ']',
        )

    def test_no_trailing_comma_after_last_entry(self):
        rendered = rewrite.render_folders(TWO, "")
        self.assertNotIn(",\n]", rendered)

    def test_base_indent_is_applied(self):
        self.assertEqual(
            rewrite.render_folders(ONE, "    "),
            '[\n      { "path": "/abs/one" }\n    ]',
        )

    def test_empty_renders_as_empty_array(self):
        self.assertEqual(rewrite.render_folders([], "  "), "[]")

    def test_paths_and_names_are_json_escaped(self):
        entry = FolderEntry('/a/"quoted"/b\\c', 'na"me')
        rendered = rewrite.render_folders([entry], "")
        self.assertEqual(json.loads(rendered)[0]["path"], '/a/"quoted"/b\\c')
        self.assertEqual(json.loads(rendered)[0]["name"], 'na"me')

class TestSplicePreservesEverythingElse(unittest.TestCase):
    def test_non_folders_bytes_are_unchanged_for_every_fixture(self):
        for name in _support.WORKSPACE_FIXTURES:
            text = _support.fixture(name)
            new_text = rewrite.splice_folders(text, TWO)
            if jsonc.find_top_level_member(text, "folders") is None:
                continue  # the insert path is covered separately
            before, _, after = split_around_folders(text)
            new_before, _, new_after = split_around_folders(new_text)
            self.assertEqual(before, new_before, "prefix changed in %s" % name)
            self.assertEqual(after, new_after, "suffix changed in %s" % name)

    def test_example_fixture_settings_comments_and_trailing_commas_survive(self):
        text = _support.fixture("example.code-workspace")
        new_text = rewrite.splice_folders(text, TWO)
        settings_start = new_text.index('"settings"')
        self.assertEqual(new_text[settings_start:], text[text.index('"settings"'):])
        # The settings block's own trailing commas are untouched.
        self.assertIn('"titleBar.inactiveBackground": "#1f8d13",\n    },\n  },\n}\n',
                      new_text)
        # And the plugin's own array has none.
        self.assertIn('{ "path": "/abs/two", "name": "api" }\n  ],', new_text)

    def test_four_space_indent_is_copied_from_the_folders_line(self):
        text = _support.fixture("four-space-indent.code-workspace")
        new_text = rewrite.splice_folders(text, TWO)
        self.assertIn('    "folders": [\n      { "path": "/abs/one" },\n'
                      '      { "path": "/abs/two", "name": "api" }\n    ]', new_text)

    def test_result_still_parses_for_every_fixture(self):
        for name in _support.WORKSPACE_FIXTURES:
            new_text = rewrite.splice_folders(_support.fixture(name), TWO)
            parsed = jsonc.loads(new_text)
            self.assertEqual(len(parsed["folders"]), 2, name)

    def test_splice_is_idempotent(self):
        for name in _support.WORKSPACE_FIXTURES:
            once = rewrite.splice_folders(_support.fixture(name), TWO)
            twice = rewrite.splice_folders(once, TWO)
            self.assertEqual(once, twice, name)


class TestRoundTrip(unittest.TestCase):
    """Splicing a file's own folders back in must not disturb it."""

    def entries_from(self, text):
        items = rewrite.read_folders(text)
        return [FolderEntry(i["path"], i.get("name")) for i in items]

    def test_canonical_fixture_is_byte_identical(self):
        for name in ("canonical.code-workspace",
                     "brackets.code-workspace",
                     "block-comments.code-workspace"):
            text = _support.fixture(name)
            self.assertEqual(
                rewrite.splice_folders(text, self.entries_from(text)), text, name
            )

    def test_non_canonical_fixtures_keep_every_byte_outside_folders(self):
        # The plugin owns the `folders` array outright and always renders it in canonical
        # form -- so a fixture whose array has a trailing comma, or one-property-per-line
        # objects, is reformatted *inside the array* and nowhere else.
        for name in ("example.code-workspace",
                     "four-space-indent.code-workspace",
                     "vscode-written.code-workspace"):
            text = _support.fixture(name)
            new_text = rewrite.splice_folders(text, self.entries_from(text))
            before, _, after = split_around_folders(text)
            new_before, _, new_after = split_around_folders(new_text)
            self.assertEqual(before, new_before, name)
            self.assertEqual(after, new_after, name)
            self.assertEqual(jsonc.loads(new_text)["folders"],
                             jsonc.loads(text)["folders"], name)

class TestInsertWhenAbsent(unittest.TestCase):
    def test_inserted_as_first_root_member(self):
        text = _support.fixture("no-folders.code-workspace")
        new_text = rewrite.splice_folders(text, TWO)
        self.assertTrue(
            new_text.startswith('{\n  "folders": [\n    { "path": "/abs/one" },\n'
                                '    { "path": "/abs/two", "name": "api" }\n  ],\n'),
            new_text[:200],
        )

    def test_everything_that_was_there_is_still_there(self):
        text = _support.fixture("no-folders.code-workspace")
        new_text = rewrite.splice_folders(text, TWO)
        self.assertIn("// No \"folders\" member at all", new_text)
        self.assertTrue(new_text.endswith(text[1:]))

    def test_remains_parseable(self):
        text = _support.fixture("no-folders.code-workspace")
        new_text = rewrite.splice_folders(text, TWO)
        # Strict JSON.parse-equivalent after only comment stripping, as the plan requires.
        parsed = json.loads(jsonc.strip_comments(new_text))
        self.assertEqual(list(parsed.keys())[0], "folders")
        self.assertEqual(len(parsed["folders"]), 2)
        self.assertEqual(parsed["settings"]["window.title"], "NO-FOLDERS-FIXTURE")

    def test_empty_root_object(self):
        text = _support.fixture("empty-root.code-workspace")
        new_text = rewrite.splice_folders(text, ONE)
        self.assertEqual(jsonc.loads(new_text), {"folders": [{"path": "/abs/one"}]})

    def test_non_object_root_raises(self):
        with self.assertRaises(ValueError):
            rewrite.splice_folders("[1, 2]", ONE)


class TestReadFolders(unittest.TestCase):
    def test_reads_trailing_comma_array(self):
        items = rewrite.read_folders(_support.fixture("example.code-workspace"))
        self.assertEqual([i["path"] for i in items],
                         ["/Users/jdoe/code/tools/herdr-plugins",
                          "/Users/jdoe/code/tools/devc-tools"])

    def test_reads_name_before_path(self):
        items = rewrite.read_folders(_support.fixture("vscode-written.code-workspace"))
        self.assertEqual(items[1], {"name": "GAMMA-RENAMED", "path": "spaces/gamma"})

    def test_no_member_returns_none(self):
        self.assertIsNone(
            rewrite.read_folders(_support.fixture("no-folders.code-workspace"))
        )

    def test_unparseable_returns_none(self):
        self.assertIsNone(rewrite.read_folders('{ "folders": [ { "path": ] }'))
        self.assertIsNone(rewrite.read_folders('{ "folders": "not an array" }'))
        self.assertIsNone(rewrite.read_folders('{ "folders": [1, 2] }'))


class TestResolvedComparison(unittest.TestCase):
    """VS Code writes relative paths; the unchanged-check must resolve before comparing."""

    def abs_entries(self):
        base = _support.FIXTURES
        return [
            FolderEntry(os.path.join(base, "spaces", "alpha")),
            FolderEntry(os.path.join(base, "spaces", "gamma"), "GAMMA-RENAMED"),
            FolderEntry(os.path.join(base, "spaces", "beta")),
        ]

    def test_relative_paths_resolve_to_the_absolute_equivalent(self):
        text = _support.fixture("vscode-written.code-workspace")
        resolved = rewrite.resolved_existing_folders(text, _support.FIXTURES)
        self.assertEqual(resolved,
                         [(e.path, e.name) for e in self.abs_entries()])

    def test_relative_file_reports_unchanged_against_absolute_entries(self):
        text = _support.fixture("vscode-written.code-workspace")
        self.assertTrue(
            write_mod.is_unchanged(text, self.abs_entries(), _support.FIXTURES)
        )
        # ...even though a naive text compare would say it changed.
        self.assertNotEqual(rewrite.splice_folders(text, self.abs_entries()), text)

    def test_a_name_change_is_detected(self):
        text = _support.fixture("vscode-written.code-workspace")
        entries = self.abs_entries()
        entries[1] = FolderEntry(entries[1].path, "DIFFERENT")
        self.assertFalse(write_mod.is_unchanged(text, entries, _support.FIXTURES))

    def test_an_order_change_is_detected(self):
        text = _support.fixture("vscode-written.code-workspace")
        entries = self.abs_entries()
        entries.reverse()
        self.assertFalse(write_mod.is_unchanged(text, entries, _support.FIXTURES))

    def test_falls_back_to_text_compare_when_unparseable(self):
        text = '{ "folders": [ { "path": ] }'
        self.assertFalse(write_mod.is_unchanged(text, ONE, "/tmp"))

    def test_unchanged_when_no_folders_member_and_nothing_to_add(self):
        text = _support.fixture("no-folders.code-workspace")
        self.assertFalse(write_mod.is_unchanged(text, ONE, "/tmp"))


class TestSyncFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vws-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scratch(self, fixture_name, dest_name=None):
        dest = os.path.join(self.tmp, dest_name or fixture_name)
        shutil.copy(_support.fixture_path(fixture_name), dest)
        return dest

    def test_writes_then_reports_unchanged(self):
        target = self.scratch("example.code-workspace")
        self.assertEqual(write_mod.sync_file(target, TWO), "wrote")
        self.assertEqual(write_mod.sync_file(target, TWO), "unchanged")
        with open(target) as fh:
            self.assertEqual(len(jsonc.loads(fh.read())["folders"]), 2)

    def test_mode_is_preserved(self):
        target = self.scratch("example.code-workspace")
        os.chmod(target, 0o640)
        write_mod.sync_file(target, TWO)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o640)

    def test_symlinked_target_is_written_through_the_link(self):
        real = self.scratch("canonical.code-workspace", "real.code-workspace")
        link = os.path.join(self.tmp, "link.code-workspace")
        os.symlink(real, link)
        self.assertEqual(write_mod.sync_file(link, TWO), "wrote")
        self.assertTrue(os.path.islink(link))
        with open(real) as fh:
            self.assertIn("/abs/one", fh.read())

    def test_relative_path_file_is_not_rewritten(self):
        target = os.path.join(self.tmp, "vscode-written.code-workspace")
        shutil.copy(_support.fixture_path("vscode-written.code-workspace"), target)
        os.makedirs(os.path.join(self.tmp, "spaces", "alpha"))
        os.makedirs(os.path.join(self.tmp, "spaces", "beta"))
        os.makedirs(os.path.join(self.tmp, "spaces", "gamma"))
        entries = [
            FolderEntry(os.path.join(self.tmp, "spaces", "alpha")),
            FolderEntry(os.path.join(self.tmp, "spaces", "gamma"), "GAMMA-RENAMED"),
            FolderEntry(os.path.join(self.tmp, "spaces", "beta")),
        ]
        with open(target) as fh:
            before = fh.read()
        self.assertEqual(write_mod.sync_file(target, entries), "unchanged")
        with open(target) as fh:
            self.assertEqual(fh.read(), before)

    def test_no_temp_files_left_behind(self):
        target = self.scratch("canonical.code-workspace")
        write_mod.sync_file(target, TWO)
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
