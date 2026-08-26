import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support  # noqa: E402  (puts src/ on sys.path)

import jsonc  # noqa: E402


class TestFindTopLevelMember(unittest.TestCase):
    def span(self, text, key="folders"):
        member = jsonc.find_top_level_member(text, key)
        self.assertIsNotNone(member, "expected to find %r in %r" % (key, text))
        return text[member.value_start : member.value_end]

    def test_simple_array_value(self):
        text = '{\n  "folders": [\n    { "path": "/a" }\n  ],\n  "x": 1\n}\n'
        self.assertEqual(self.span(text), '[\n    { "path": "/a" }\n  ]')

    def test_key_start_points_at_opening_quote(self):
        text = '{\n  "folders": []\n}\n'
        member = jsonc.find_top_level_member(text, "folders")
        self.assertEqual(text[member.key_start], '"')
        self.assertEqual(text[member.key_start : member.key_start + 9], '"folders"')

    def test_missing_member_returns_none(self):
        self.assertIsNone(
            jsonc.find_top_level_member(_support.fixture("no-folders.code-workspace"),
                                        "folders")
        )

    def test_non_object_root_returns_none(self):
        self.assertIsNone(jsonc.find_top_level_member('[1, 2, 3]', "folders"))
        self.assertIsNone(jsonc.find_top_level_member('   ', "folders"))

    def test_nested_member_does_not_match(self):
        text = '{\n  "settings": {\n    "folders": [1]\n  },\n  "other": 2\n}\n'
        self.assertIsNone(jsonc.find_top_level_member(text, "folders"))

    def test_bracket_inside_string_does_not_terminate_array(self):
        text = _support.fixture("brackets.code-workspace")
        value = self.span(text)
        self.assertTrue(value.startswith("["))
        self.assertTrue(value.endswith("]"))
        self.assertEqual(len(json.loads(value)), 2)
        # The ] characters inside `settings` strings are still in the file, untouched.
        self.assertIn('"a]b[c}d{e"', text)

    def test_bracket_inside_line_comment_does_not_terminate_array(self):
        text = '{\n  "folders": [\n    // a ] and a } in a comment\n    { "path": "/a" }\n  ]\n}\n'
        value = self.span(text)
        self.assertIn("// a ] and a }", value)
        self.assertTrue(value.rstrip().endswith("]"))

    def test_bracket_inside_block_comment_does_not_terminate_array(self):
        text = '{\n  "folders": [\n    /* ] } "folders": [ */\n    { "path": "/a" }\n  ]\n}\n'
        value = self.span(text)
        self.assertIn('/* ] } "folders": [ */', value)

    def test_block_comments_between_members(self):
        text = _support.fixture("block-comments.code-workspace")
        self.assertEqual(len(json.loads(self.span(text))), 1)

    def test_string_value_at_top_level(self):
        text = '{\n  "name": "with a ] and a } inside",\n  "folders": [1]\n}\n'
        self.assertEqual(self.span(text), "[1]")
        self.assertEqual(self.span(text, "name"), '"with a ] and a } inside"')

    def test_literal_values_at_top_level(self):
        text = '{ "a": 1, "b": true, "c": null, "folders": [] }'
        self.assertEqual(self.span(text, "a"), "1")
        self.assertEqual(self.span(text, "b"), "true")
        self.assertEqual(self.span(text, "c"), "null")
        self.assertEqual(self.span(text), "[]")

    def test_escaped_quote_in_key(self):
        text = '{ "fol\\"ders": 1, "folders": [2] }'
        self.assertEqual(self.span(text), "[2]")

    def test_object_value_is_skipped_whole(self):
        text = '{ "settings": { "a": [1, 2] }, "folders": [3] }'
        self.assertEqual(self.span(text), "[3]")

    def test_trailing_comma_array(self):
        text = _support.fixture("example.code-workspace")
        value = self.span(text)
        self.assertTrue(value.rstrip().endswith("]"))
        self.assertIn("devc-tools", value)
        self.assertNotIn("colorCustomizations", value)


class TestFindRootObjectOpen(unittest.TestCase):
    def test_leading_comment_and_whitespace(self):
        text = '\n// hi\n/* there */\n  {\n  "a": 1\n}\n'
        self.assertEqual(text[jsonc.find_root_object_open(text)], "{")

    def test_array_root(self):
        self.assertEqual(jsonc.find_root_object_open("[1]"), -1)


class TestStripComments(unittest.TestCase):
    def test_length_preserved_and_newlines_kept(self):
        for name in _support.WORKSPACE_FIXTURES:
            text = _support.fixture(name)
            stripped = jsonc.strip_comments(text)
            self.assertEqual(len(text), len(stripped), name)
            self.assertEqual(text.count("\n"), stripped.count("\n"), name)

    def test_comments_removed(self):
        text = '{ // gone\n  "a": 1, /* also gone */ "b": 2 }'
        stripped = jsonc.strip_comments(text)
        self.assertNotIn("gone", stripped)
        self.assertEqual(json.loads(stripped), {"a": 1, "b": 2})

    def test_comment_markers_inside_strings_survive(self):
        text = '{ "a": "http://example.com // not a comment /* nor this */" }'
        self.assertEqual(jsonc.strip_comments(text), text)

    def test_offsets_line_up(self):
        text = '{\n  // comment\n  "folders": [1]\n}\n'
        stripped = jsonc.strip_comments(text)
        member = jsonc.find_top_level_member(text, "folders")
        self.assertEqual(stripped[member.value_start : member.value_end], "[1]")


class TestStripTrailingCommas(unittest.TestCase):
    def test_example_fixture_becomes_strict_json(self):
        text = _support.fixture("example.code-workspace")
        with self.assertRaises(ValueError):
            json.loads(text)
        parsed = json.loads(jsonc.strip_trailing_commas(jsonc.strip_comments(text)))
        self.assertEqual(len(parsed["folders"]), 2)

    def test_comma_inside_string_survives(self):
        text = '{ "a": "x,]" }'
        self.assertEqual(jsonc.strip_trailing_commas(text), text)

    def test_length_preserved(self):
        for name in _support.WORKSPACE_FIXTURES:
            text = _support.fixture(name)
            self.assertEqual(len(text), len(jsonc.strip_trailing_commas(text)), name)


class TestLoads(unittest.TestCase):
    def test_every_workspace_fixture_parses(self):
        for name in _support.WORKSPACE_FIXTURES:
            parsed = jsonc.loads(_support.fixture(name))
            self.assertIsInstance(parsed, dict, name)

    def test_config_example_parses(self):
        path = os.path.join(_support.PLUGIN_ROOT, "config.example.json")
        with open(path) as fh:
            parsed = jsonc.loads(fh.read())
        self.assertEqual(parsed["mode"], "mirror")
        self.assertEqual(parsed["pinnedFolders"], [])


if __name__ == "__main__":
    unittest.main()
