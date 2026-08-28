"""The fixed-path shim that makes `track` reachable without hunting for the plugin root.

A GitHub-installed plugin root carries a content hash in its path, so the README cannot
name `bin/track` and a `PATH` symlink cannot point at it. The daemon writes a launcher at
a fixed place in the state directory instead; this pins that path and the repair rules.
"""

import _support  # noqa: F401
import os
import subprocess
import sys
import unittest

import daemonize
import main
from _support import ROOT, TempDirCase

BIN = os.path.join(ROOT, "bin", "track")


class LauncherTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.paths = main.Paths(self.path("state"), "/nowhere/herdr.sock")

    def write(self):
        return daemonize.write_launcher(self.paths.launcher, BIN)

    def test_it_lands_at_the_documented_path_and_is_executable(self):
        # The README hands out `<state dir>/track` verbatim, and tells the user to
        # symlink it onto PATH. If this name moves, every such symlink dangles.
        self.assertEqual(self.paths.launcher,
                         os.path.join(self.paths.state_dir, "track"))
        self.assertEqual(self.write(), self.paths.launcher)
        self.assertTrue(os.access(self.paths.launcher, os.X_OK))
        with open(self.paths.launcher) as fh:
            body = fh.read()
        self.assertTrue(body.startswith("#!/bin/sh\n"))
        self.assertIn('exec %s "$@"' % BIN, body)

    def test_the_launcher_actually_runs_the_command(self):
        self.write()
        result = subprocess.run([self.paths.launcher, "report", "--json"],
                                capture_output=True, text=True,
                                env=dict(os.environ,
                                         HERDR_TRACK_ENTRIES_PATH=self.path("none.jsonl")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().startswith("{"), result.stdout)

    def test_it_repoints_at_a_moved_plugin_root(self):
        # What a reinstall looks like: the shim names yesterday's checkout.
        with open(self.paths.launcher, "w") as fh:
            fh.write('#!/bin/sh\nexec /gone/bin/track "$@"\n')
        self.write()
        with open(self.paths.launcher) as fh:
            self.assertNotIn("/gone/", fh.read())

    def test_it_restores_a_lost_executable_bit(self):
        self.write()
        os.chmod(self.paths.launcher, 0o644)
        self.write()
        self.assertTrue(os.access(self.paths.launcher, os.X_OK))

    def test_rewriting_an_intact_launcher_does_not_touch_the_file(self):
        # `--ensure` runs on every workspace.focused, so the common path must be a read.
        self.write()
        before = os.stat(self.paths.launcher)
        self.write()
        after = os.stat(self.paths.launcher)
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))

    def test_the_daemon_writes_it_before_anything_else_can_fail(self):
        # The call site, not the writer: `daemon` is the only thing that creates the
        # launcher, and it must do so before the checks that can abort the run.
        state = self.path("fresh")
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "src", "main.py"), "daemon"],
            capture_output=True, text=True,
            env=dict(os.environ, HERDR_PLUGIN_STATE_DIR=state,
                     HERDR_PLUGIN_CONFIG_DIR=self.path("cfg"),
                     HERDR_SOCKET_PATH=""))
        self.assertEqual(result.returncode, main.EXIT_SOCKET, result.stderr)
        self.assertTrue(os.access(os.path.join(state, "track"), os.X_OK),
                        "daemon start did not leave a launcher behind")

    def test_an_unwritable_state_dir_is_not_fatal(self):
        # A convenience shim must never be able to take the daemon down.
        os.chmod(self.paths.state_dir, 0o500)
        self.addCleanup(os.chmod, self.paths.state_dir, 0o700)
        self.assertEqual(self.write(), self.paths.launcher)


if __name__ == "__main__":
    unittest.main()
