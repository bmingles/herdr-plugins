"""The fixed-path shims that make `sync --doctor` and `adopt` reachable.

A GitHub-installed plugin root carries a content hash in its path, so the README cannot
name `bin/sync`. Every Herdr-invoked run refreshes a shim in the state directory instead;
this pins those paths, the repair rules, and the rule that a run outside Herdr writes
nothing at all.
"""

import _support  # noqa: F401
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import launchers
from _support import PLUGIN_ROOT


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {"HERDR_PLUGIN_STATE_DIR": self.tmp}

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_both_entrypoints_land_at_the_documented_paths(self):
        # The README hands out `<state dir>/sync` and `<state dir>/adopt` verbatim, and
        # tells the user to symlink them onto PATH. If a name moves, those dangle.
        found = launchers.refresh(self.env)
        self.assertEqual(sorted(found), ["adopt", "sync"])
        for name in ("sync", "adopt"):
            self.assertEqual(found[name], self.path(name))
            self.assertTrue(os.access(self.path(name), os.X_OK))
            with open(self.path(name)) as fh:
                body = fh.read()
            self.assertTrue(body.startswith("#!/bin/sh\n"))
            self.assertIn('exec %s "$@"'
                          % os.path.join(PLUGIN_ROOT, "bin", name), body)

    def test_the_launcher_actually_runs_the_command(self):
        launchers.refresh(self.env)
        result = subprocess.run([self.path("adopt"), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--dry-run", result.stdout)

    def test_no_state_dir_means_no_shims_anywhere(self):
        # A hand-run from a checkout, or a test, must not scribble into the real
        # ~/.local/state directory just because Herdr was not the caller.
        self.assertEqual(launchers.refresh({}), {})
        self.assertEqual(os.listdir(self.tmp), [])

    def test_it_repoints_at_a_moved_plugin_root(self):
        # What a reinstall looks like: the shim names yesterday's checkout.
        with open(self.path("sync"), "w") as fh:
            fh.write('#!/bin/sh\nexec /gone/bin/sync "$@"\n')
        launchers.refresh(self.env)
        with open(self.path("sync")) as fh:
            self.assertNotIn("/gone/", fh.read())

    def test_it_restores_a_lost_executable_bit(self):
        launchers.refresh(self.env)
        os.chmod(self.path("sync"), 0o644)
        launchers.refresh(self.env)
        self.assertTrue(os.access(self.path("sync"), os.X_OK))

    def test_refreshing_intact_shims_does_not_touch_them(self):
        # Twelve event hooks call this, so the common path must be a read.
        launchers.refresh(self.env)
        before = [os.stat(self.path(n)) for n in ("sync", "adopt")]
        launchers.refresh(self.env)
        after = [os.stat(self.path(n)) for n in ("sync", "adopt")]
        self.assertEqual([(s.st_ino, s.st_mtime_ns) for s in before],
                         [(s.st_ino, s.st_mtime_ns) for s in after])

    def test_an_unwritable_state_dir_is_not_fatal(self):
        os.chmod(self.tmp, 0o500)
        self.addCleanup(os.chmod, self.tmp, 0o700)
        self.assertEqual(sorted(launchers.refresh(self.env)), ["adopt", "sync"])

    def test_a_hook_run_installs_them_even_when_the_config_is_broken(self):
        # The call site, not the writer. `--doctor` is what you reach for when config is
        # wrong, so the shims have to exist before the config load can abort the run.
        cfg_dir = os.path.join(self.tmp, "cfg")
        os.makedirs(cfg_dir)
        with open(os.path.join(cfg_dir, "config.json"), "w") as fh:
            fh.write("{ not json")
        result = subprocess.run(
            [sys.executable, os.path.join(PLUGIN_ROOT, "src", "main.py"),
             "--reason", "event"],
            capture_output=True, text=True,
            env=dict(os.environ, HERDR_PLUGIN_STATE_DIR=self.tmp,
                     HERDR_PLUGIN_CONFIG_DIR=cfg_dir))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(os.access(self.path("sync"), os.X_OK),
                        "a failed hook run left no launcher behind")


if __name__ == "__main__":
    unittest.main()
