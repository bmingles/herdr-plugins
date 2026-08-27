"""Spawn / kill / reap -- the mechanism that would drive `caffeinate`.

The real `caffeinate` flags are documented and stable; what needs proving is that this
plugin starts one process, kills exactly that process, records its pid so a crashed
daemon's orphan can be reaped, and degrades safely where the command does not exist.
`test/fake-caffeinate` has the same lifecycle (runs until signalled) and logs it.
"""

import _support  # noqa: F401
import json
import os
import time
import unittest

from _support import FAKE_CAFFEINATE, TempDirCase
from daemonize import Log
from inhibitor import Inhibitor


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class InhibitorTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.log_path = self.path("daemon.log")
        self.state_path = self.path("inhibitor.json")
        self.caffeinate_log = self.path("caffeinate.log")
        os.environ["FAKE_CAFFEINATE_LOG"] = self.caffeinate_log
        self.addCleanup(os.environ.pop, "FAKE_CAFFEINATE_LOG", None)
        self.log = Log(self.log_path, "debug")

    def make(self, argv=None):
        return Inhibitor(argv or [FAKE_CAFFEINATE], self.state_path, self.log)

    def caffeinate_lines(self):
        try:
            with open(self.caffeinate_log) as fh:
                return [l.strip() for l in fh if l.strip()]
        except IOError:
            return []

    def test_start_spawns_and_stop_reaps(self):
        inh = self.make()
        self.assertFalse(inh.dry)
        self.assertTrue(inh.start(trigger="w1:p1"))
        pid = inh.pid()
        self.assertIsNotNone(pid)
        self.assertTrue(wait_for(lambda: any(l.startswith("START")
                                             for l in self.caffeinate_lines())))
        inh.stop(reason="idle-grace")
        self.assertTrue(wait_for(lambda: not alive(pid)))
        self.assertTrue(any(l.startswith("STOP") for l in self.caffeinate_lines()))
        self.assertIsNone(inh.pid())

    def test_start_is_idempotent(self):
        inh = self.make()
        inh.start()
        pid = inh.pid()
        self.assertTrue(wait_for(lambda: any(l.startswith("START")
                                             for l in self.caffeinate_lines())))
        inh.start()
        self.assertEqual(inh.pid(), pid)
        starts = [l for l in self.caffeinate_lines() if l.startswith("START")]
        self.addCleanup(inh.stop)
        self.assertEqual(len(starts), 1)

    def test_pid_is_recorded_before_anything_else(self):
        inh = self.make()
        inh.start()
        self.addCleanup(inh.stop)
        with open(self.state_path) as fh:
            state = json.load(fh)
        self.assertEqual(state["pid"], inh.pid())
        self.assertEqual(state["argv"], [FAKE_CAFFEINATE])

    def test_state_file_is_cleared_on_stop(self):
        inh = self.make()
        inh.start()
        inh.stop()
        self.assertFalse(os.path.exists(self.state_path))

    def test_adopt_stale_kills_an_orphan_from_a_crashed_daemon(self):
        """A kill -9'd daemon leaves its inhibitor holding the assertion forever."""
        first = self.make()
        first.start()
        orphan = first.pid()
        # Simulate the daemon dying without stopping its inhibitor. Keep a reference to
        # the Popen so Python does not warn about GC'ing a live subprocess; the point is
        # that `first` no longer knows about it.
        self._leaked = first.proc
        first.proc = None
        self.assertTrue(alive(orphan))

        second = self.make()
        reaped = second.adopt_stale()
        self.assertEqual(reaped, orphan)
        self.assertTrue(wait_for(lambda: not alive(orphan)))
        self.assertFalse(os.path.exists(self.state_path))

    def test_adopt_stale_ignores_a_dead_pid(self):
        with open(self.state_path, "w") as fh:
            json.dump({"pid": 999999, "argv": [FAKE_CAFFEINATE], "at": 0}, fh)
        self.assertIsNone(self.make().adopt_stale())
        self.assertFalse(os.path.exists(self.state_path))

    def test_adopt_stale_ignores_a_pid_from_a_different_command(self):
        """Never kill a pid we cannot attribute to our own argv -- it may be reused."""
        inh = self.make()
        inh.start()
        self.addCleanup(inh.stop)
        with open(self.state_path, "w") as fh:
            json.dump({"pid": inh.pid(), "argv": ["/some/other/thing"], "at": 0}, fh)
        other = Inhibitor([FAKE_CAFFEINATE], self.state_path, self.log)
        self.assertIsNone(other.adopt_stale())
        self.assertTrue(alive(inh.pid()))

    def test_dry_mode_when_the_command_is_missing(self):
        inh = self.make(["definitely-not-a-real-binary-xyz"])
        self.assertTrue(inh.dry)
        self.assertTrue(inh.start())
        self.assertIsNone(inh.pid())
        inh.stop(reason="idle-grace")
        with open(self.log_path) as fh:
            text = fh.read()
        self.assertIn("dry-run: would start", text)
        self.assertIn("dry-run: would stop", text)
        self.assertIn("dry mode", text)

    def test_stop_without_start_is_harmless(self):
        self.assertTrue(self.make().stop(reason="shutdown"))

    def test_unlaunchable_command_reports_and_does_not_hold(self):
        path = self.path("not-executable")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(path, 0o644)          # present, but not executable
        inh = self.make([path])
        self.assertTrue(inh.dry)       # not executable -> which() finds nothing
        inh.dry = False                # force the spawn path to exercise the OSError
        self.assertFalse(inh.start())
        self.assertIsNone(inh.pid())

    def test_log_records_start_and_stop_with_pid_and_reason(self):
        inh = self.make()
        inh.start(trigger="w1:p2")
        inh.stop(reason="idle-grace idle_for=60.4s")
        with open(self.log_path) as fh:
            text = fh.read()
        self.assertIn("inhibitor start pid=", text)
        self.assertIn("trigger=w1:p2", text)
        self.assertIn("inhibitor stop pid=", text)
        self.assertIn("reason=idle-grace", text)


if __name__ == "__main__":
    unittest.main()
