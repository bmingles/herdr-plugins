"""The append-only log: format, tolerance, recovery, concurrency."""

import _support  # noqa: F401
import json
import os
import subprocess
import sys
import unittest

import store
from _support import TempDirCase

ENTRY = {"v": 1, "workspace_id": "w1", "label": "alpha",
         "start": "2026-08-27T09:12:03-05:00", "end": "2026-08-27T09:41:55-05:00",
         "seconds": 1792, "end_reason": "switch", "session": "default", "host": "h"}


class StoreTest(TempDirCase):
    def test_append_writes_exactly_one_line(self):
        path = self.path("entries.jsonl")
        store.append_entry(path, ENTRY)
        with open(path) as fh:
            content = fh.read()
        self.assertEqual(content.count("\n"), 1)
        self.assertTrue(content.endswith("\n"))
        self.assertEqual(json.loads(content), ENTRY)

    def test_append_creates_missing_directories(self):
        path = self.path("deep", "nested", "entries.jsonl")
        store.append_entry(path, ENTRY)
        self.assertTrue(os.path.exists(path))

    def test_round_trip(self):
        path = self.path("entries.jsonl")
        for i in range(5):
            store.append_entry(path, dict(ENTRY, seconds=i))
        entries = store.read_entries(path)
        self.assertEqual([e["seconds"] for e in entries], [0, 1, 2, 3, 4])

    def test_malformed_lines_are_skipped_and_reported(self):
        path = self.path("entries.jsonl")
        store.append_entry(path, ENTRY)
        with open(path, "a") as fh:
            fh.write('{"truncated": tru\n')      # a torn line from a killed process
        store.append_entry(path, ENTRY)
        bad = []
        entries = store.read_entries(path, on_bad_line=lambda n, r: bad.append(n))
        self.assertEqual(len(entries), 2)
        self.assertEqual(bad, [2])

    def test_blank_lines_are_ignored_silently(self):
        path = self.path("entries.jsonl")
        with open(path, "w") as fh:
            fh.write("\n\n")
        store.append_entry(path, ENTRY)
        bad = []
        self.assertEqual(len(store.read_entries(path, lambda n, r: bad.append(n))), 1)
        self.assertEqual(bad, [])

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(store.read_entries(self.path("nope.jsonl")), [])

    def test_non_entry_objects_are_rejected(self):
        path = self.path("entries.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"hello": "world"}) + "\n")
        bad = []
        self.assertEqual(store.read_entries(path, lambda n, r: bad.append(n)), [])
        self.assertEqual(bad, [1])

    def test_concurrent_appends_never_tear_a_line(self):
        """Two daemons share one entries file; interleaved lines are fine, torn are not."""
        path = self.path("entries.jsonl")
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "import store\n"
            "entry = %r\n"
            "for i in range(500):\n"
            "    store.append_entry(%r, dict(entry, seconds=i, host=sys.argv[1]))\n"
            % (_support.SRC, ENTRY, path))
        procs = [subprocess.Popen([sys.executable, "-c", script, tag])
                 for tag in ("A", "B")]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=60), 0)

        bad = []
        entries = store.read_entries(path, on_bad_line=lambda n, r: bad.append(n))
        self.assertEqual(bad, [], "a line was torn by concurrent appends")
        self.assertEqual(len(entries), 1000)
        self.assertEqual(len([e for e in entries if e["host"] == "A"]), 500)
        self.assertEqual(len([e for e in entries if e["host"] == "B"]), 500)

    def test_current_round_trip_and_clear(self):
        path = self.path("current.json")
        state = {"workspace_id": "w1", "start": 1000.0, "last_activity": 1200.0}
        self.assertTrue(store.write_current(path, state))
        self.assertEqual(store.read_current(path), state)
        store.clear_current(path)
        self.assertIsNone(store.read_current(path))

    def test_current_ignores_garbage(self):
        path = self.path("current.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        self.assertIsNone(store.read_current(path))

    def test_current_ignores_an_object_without_a_start(self):
        path = self.path("current.json")
        store.write_current(path, {"workspace_id": "w1"})
        self.assertIsNone(store.read_current(path))

    def test_entries_path_honours_the_env_override(self):
        self.assertEqual(
            store.entries_path("/state", {"HERDR_TRACK_ENTRIES_PATH": "/tmp/x.jsonl"}),
            "/tmp/x.jsonl")
        self.assertEqual(store.entries_path("/state", {}),
                         os.path.join("/state", "entries.jsonl"))


if __name__ == "__main__":
    unittest.main()
