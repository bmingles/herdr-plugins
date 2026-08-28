"""The `indicator` subcommand: what the tab bar renders, and when it renders nothing.

The state logic is pure over two files the daemon already writes, so most of this is a
unit test over hand-built state directories. One end-to-end case drives the real daemon
against the fake server, because the thing most worth proving is that the indicator
tracks a real hold rather than a hand-written `daemon.json`.
"""

import _support  # noqa: F401
import json
import os
import subprocess
import sys
import time
import unittest

import config as config_mod
import daemonize
import main
from _support import ENTRYPOINT, TempDirCase
from test_e2e import DaemonHarness, wait_for


def dead_pid():
    """A pid that has certainly exited. Reaped, so nothing recycles it mid-test."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


class IndicatorStateTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.cfg = config_mod.Config()          # pollIntervalSec 2.0 -> stale after 15s
        self.paths = main.Paths(self.path("state"), "/nowhere/herdr.sock")

    # -- fixtures ------------------------------------------------------------------

    def hold_lock(self, pid=None):
        with open(self.paths.lock, "w") as fh:
            fh.write("%d\n" % (os.getpid() if pid is None else pid))

    def write_status(self, **fields):
        payload = {"updated_at": time.time(), "holding": False, "dry": False,
                   "active_panes": [], "seconds_until_stop": None}
        payload.update(fields)
        with open(self.paths.status, "w") as fh:
            json.dump(payload, fh)

    def state(self):
        return main.indicator_state(self.cfg, self.paths)

    def text(self, label="caffeinate", icon=main.ICON_HOLDING, show_idle=False):
        return main.indicator_text(self.state(), label, icon, show_idle)

    # -- nothing to say ------------------------------------------------------------

    def test_no_daemon_at_all_renders_nothing(self):
        self.assertEqual(self.state()["state"], "absent")
        self.assertEqual(self.text(), "")

    def test_a_dead_lock_holder_renders_nothing(self):
        self.hold_lock(dead_pid())
        self.write_status(holding=True)
        self.assertEqual(self.state()["state"], "absent")
        self.assertEqual(self.text(), "")

    def test_a_holder_that_has_not_reported_yet_renders_nothing(self):
        """A daemon one poll into its life. Not a fault; do not draw a warning."""
        self.hold_lock()
        self.assertEqual(self.state()["state"], "starting")
        self.assertEqual(self.text(), "")

    def test_unparseable_status_renders_nothing(self):
        self.hold_lock()
        with open(self.paths.status, "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.state()["state"], "starting")
        self.assertEqual(self.text(), "")

    def test_idle_renders_nothing_by_default(self):
        self.hold_lock()
        self.write_status(holding=False)
        self.assertEqual(self.state()["state"], "idle")
        self.assertEqual(self.text(), "")

    # -- something to say ----------------------------------------------------------

    def test_holding(self):
        self.hold_lock()
        self.write_status(holding=True, active_panes=["w1:p1"])
        info = self.state()
        self.assertEqual(info["state"], "holding")
        self.assertEqual(info["active_panes"], ["w1:p1"])
        self.assertEqual(self.text(), "%s caffeinate" % main.ICON_HOLDING)

    def test_idle_can_be_shown_on_request(self):
        self.hold_lock()
        self.write_status(holding=False)
        self.assertEqual(self.text(show_idle=True), "%s caffeinate" % main.ICON_IDLE)

    def test_dry_mode_is_marked(self):
        self.hold_lock()
        self.write_status(holding=True, dry=True)
        self.assertTrue(self.state()["dry"])
        self.assertEqual(self.text(), "%s caffeinate (dry)" % main.ICON_HOLDING)

    def test_label_and_icon_are_overridable(self):
        self.hold_lock()
        self.write_status(holding=True)
        self.assertEqual(self.text(label="awake", icon="*"), "* awake")

    def test_an_empty_label_leaves_the_icon_alone(self):
        self.hold_lock()
        self.write_status(holding=True)
        self.assertEqual(self.text(label=""), main.ICON_HOLDING)

    # -- faults --------------------------------------------------------------------

    def test_a_wedged_daemon_warns_rather_than_reporting_a_stale_hold(self):
        self.hold_lock()
        self.write_status(holding=True, updated_at=time.time() - 3600)
        info = self.state()
        self.assertEqual(info["state"], "wedged")
        self.assertGreater(info["age"], 3000)
        self.assertEqual(self.text(), "%s caffeinate" % main.ICON_FAULT)

    def test_the_staleness_bound_follows_the_poll_interval(self):
        """A long pollIntervalSec must not make a healthy daemon look wedged."""
        self.cfg.poll_interval_sec = 60.0       # stale after 300s, not 15
        self.hold_lock()
        self.write_status(holding=True, updated_at=time.time() - 100)
        self.assertEqual(self.state()["state"], "holding")

    def test_a_broken_config_file_does_not_blind_the_indicator(self):
        env = {"HERDR_PLUGIN_CONFIG_DIR": self.path("cfg"),
               "HERDR_PLUGIN_STATE_DIR": self.path("state2"),
               "HERDR_SOCKET_PATH": "/nowhere/herdr.sock"}
        os.makedirs(env["HERDR_PLUGIN_CONFIG_DIR"], exist_ok=True)
        with open(os.path.join(env["HERDR_PLUGIN_CONFIG_DIR"], "config.json"), "w") as fh:
            fh.write("{ this is not json")
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(env)

        paths = main.Paths(env["HERDR_PLUGIN_STATE_DIR"], env["HERDR_SOCKET_PATH"])
        with open(paths.lock, "w") as fh:
            fh.write("%d\n" % os.getpid())
        with open(paths.status, "w") as fh:
            json.dump({"updated_at": time.time(), "holding": True}, fh)

        out = subprocess.run(
            [sys.executable, os.path.join(_support.ROOT, "src", "main.py"), "indicator"],
            env=dict(os.environ, **env), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "%s caffeinate" % main.ICON_HOLDING)


class LauncherTest(TempDirCase):
    """The fixed-path shim that lets `config.toml` name the indicator without the user
    hunting down a content-hashed plugin root."""

    def setUp(self):
        super().setUp()
        self.paths = main.Paths(self.path("state"), "/nowhere/herdr.sock")

    def write(self):
        return daemonize.write_launcher(self.paths.launcher, ENTRYPOINT)

    def test_it_lands_at_the_documented_path_and_is_executable(self):
        # The README hands out `<state dir>/agent-caffeinate` verbatim; if this moves,
        # every config.toml and PATH symlink built from that README breaks silently.
        self.assertEqual(self.paths.launcher,
                         os.path.join(self.paths.root, "agent-caffeinate"))
        self.assertEqual(self.write(), self.paths.launcher)
        self.assertTrue(os.access(self.paths.launcher, os.X_OK))
        with open(self.paths.launcher) as fh:
            body = fh.read()
        self.assertTrue(body.startswith("#!/bin/sh\n"))
        self.assertIn('exec %s "$@"' % ENTRYPOINT, body)

    def test_it_repoints_at_a_moved_plugin_root(self):
        # What a reinstall looks like: the shim names yesterday's checkout.
        with open(self.paths.launcher, "w") as fh:
            fh.write("#!/bin/sh\nexec /gone/bin/agent-caffeinate \"$@\"\n")
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

    def test_an_unwritable_state_dir_is_not_fatal(self):
        # An optional tab bar entry must never be able to take the daemon down.
        os.chmod(self.paths.root, 0o500)
        self.addCleanup(os.chmod, self.paths.root, 0o700)
        self.assertEqual(self.write(), self.paths.launcher)


class IndicatorE2ETest(DaemonHarness):
    def test_the_indicator_tracks_a_real_hold(self):
        self.start_daemon()
        self.assertEqual(self.run_cli("indicator").stdout, "",
                         "an idle daemon should render nothing")

        self.server.set_statuses({"w1:p1": "working"})
        self.assertTrue(
            wait_for(lambda: self.run_cli("indicator").stdout != "", interval=0.15),
            "the indicator never showed the hold")

        result = self.run_cli("indicator")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "%s caffeinate" % main.ICON_HOLDING)

        payload = json.loads(self.run_cli("indicator", "--json").stdout)
        self.assertEqual(payload["state"], "holding")
        self.assertFalse(payload["dry"])
        self.assertEqual(payload["active_panes"], ["w1:p1"])
        self.assertEqual(payload["text"], "%s caffeinate" % main.ICON_HOLDING)
        self.assertLess(payload["age"], 5.0)

        self.server.set_statuses({"w1:p1": "idle"})
        self.assertTrue(
            wait_for(lambda: self.run_cli("indicator").stdout == "", interval=0.15),
            "the indicator never cleared after the release")

    def test_the_daemon_writes_a_launcher_that_renders_the_same_line(self):
        self.start_daemon()
        launcher = os.path.join(self.path("state"), "agent-caffeinate")
        self.assertTrue(wait_for(lambda: os.access(launcher, os.X_OK), interval=0.1),
                        "the daemon never wrote the indicator launcher")

        self.server.set_statuses({"w1:p1": "working"})
        self.assertTrue(
            wait_for(lambda: self.run_cli("indicator").stdout != "", interval=0.15),
            "the indicator never showed the hold")

        # This is exactly what Herdr runs for a ui.tab_bar_right command entry.
        result = subprocess.run([launcher, "indicator"], env=self.env(),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "%s caffeinate" % main.ICON_HOLDING)

        flagged = subprocess.run([launcher, "indicator", "--label", "AWAKE"],
                                 env=self.env(), capture_output=True, text=True)
        self.assertEqual(flagged.stdout.strip(), "%s AWAKE" % main.ICON_HOLDING)

    def test_the_indicator_renders_nothing_with_no_daemon_running(self):
        result = self.run_cli("indicator")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(self.run_cli("indicator", "--json").stdout)["state"],
                         "absent")


if __name__ == "__main__":
    unittest.main()
