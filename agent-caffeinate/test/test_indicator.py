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
import main
from _support import TempDirCase
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

    def test_the_indicator_renders_nothing_with_no_daemon_running(self):
        result = self.run_cli("indicator")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(self.run_cli("indicator", "--json").stdout)["state"],
                         "absent")


if __name__ == "__main__":
    unittest.main()
