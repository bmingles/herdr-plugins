"""End to end: the real daemon, a fake Herdr server, a fake inhibitor.

Nothing here is mocked inside the process -- `bin/agent-caffeinate` runs as a
subprocess exactly as Herdr would run it, talks the real socket protocol to
`fake_server`, and spawns `test/fake-caffeinate` as its inhibitor. The only things
faked are the two ends the devcontainer cannot provide: a Herdr server and macOS.
"""

import _support  # noqa: F401
import json
import os
import signal
import subprocess
import sys
import time
import unittest

from _support import ENTRYPOINT, FAKE_CAFFEINATE, TempDirCase
from fake_server import FakeHerdrServer

GRACE = 1.0
POLL = 0.1


def wait_for(predicate, timeout=10.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


class DaemonE2ETest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.sock_path = self.path("herdr.sock")
        self.server = FakeHerdrServer(self.sock_path, self.path("statuses.json"))
        self.server.set_statuses({})
        self.server.start()
        self.addCleanup(self.server.stop)
        self.caffeinate_log = self.path("caffeinate.log")
        self.procs = []
        self.addCleanup(self._kill_all)

    def _kill_all(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def env(self, **extra):
        env = dict(os.environ)
        env.update({
            "HERDR_SOCKET_PATH": self.sock_path,
            "HERDR_PLUGIN_STATE_DIR": self.path("state"),
            "HERDR_PLUGIN_CONFIG_DIR": self.path("cfg"),
            "HERDR_CAFFEINATE_INHIBITOR_COMMAND": json.dumps([FAKE_CAFFEINATE]),
            "HERDR_CAFFEINATE_IDLE_GRACE_SEC": str(GRACE),
            "HERDR_CAFFEINATE_POLL_INTERVAL_SEC": str(POLL),
            "HERDR_CAFFEINATE_LOG_LEVEL": "debug",
            "FAKE_CAFFEINATE_LOG": self.caffeinate_log,
        })
        env.update(extra)
        return env

    def start_daemon(self, *args, **kwargs):
        proc = subprocess.Popen(
            [sys.executable, os.path.join(_support.ROOT, "src", "main.py"),
             "daemon", "--foreground"] + list(args),
            env=self.env(**kwargs), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.procs.append(proc)
        return proc

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(_support.ROOT, "src", "main.py")] + list(args),
            env=self.env(), capture_output=True, text=True)

    def lines(self):
        try:
            with open(self.caffeinate_log) as fh:
                return [l.strip() for l in fh if l.strip()]
        except IOError:
            return []

    def count(self, prefix):
        return len([l for l in self.lines() if l.startswith(prefix)])

    # -- the core behaviour ------------------------------------------------------

    def test_holds_while_working_and_releases_after_the_grace(self):
        self.start_daemon()
        self.server.set_statuses({"w1:p1": "working"})
        self.assertTrue(wait_for(lambda: self.count("START") == 1),
                        "inhibitor never started")

        started_at = time.time()
        self.server.set_statuses({"w1:p1": "idle"})
        self.assertTrue(wait_for(lambda: self.count("STOP") == 1),
                        "inhibitor never stopped")
        held_for = time.time() - started_at
        self.assertGreaterEqual(held_for, GRACE * 0.8,
                                "released before the grace period elapsed")
        self.assertLess(held_for, GRACE + 3.0, "released far too late")

    def test_repeated_identical_statuses_start_it_once(self):
        self.start_daemon()
        for _ in range(30):
            self.server.set_statuses({"w1:p1": "working"})
            time.sleep(0.02)
        self.assertTrue(wait_for(lambda: self.count("START") == 1))
        time.sleep(0.5)
        self.assertEqual(self.count("START"), 1)
        self.assertEqual(self.count("STOP"), 0)

    def test_a_second_agent_keeps_it_held(self):
        self.start_daemon()
        self.server.set_statuses({"a:p1": "working", "b:p1": "working"})
        self.assertTrue(wait_for(lambda: self.count("START") == 1))
        self.server.set_statuses({"a:p1": "idle", "b:p1": "working"})
        time.sleep(GRACE * 2)
        self.assertEqual(self.count("STOP"), 0, "released while an agent was still working")

    def test_activity_during_the_grace_cancels_the_release(self):
        self.start_daemon()
        self.server.set_statuses({"w1:p1": "working"})
        self.assertTrue(wait_for(lambda: self.count("START") == 1))
        self.server.set_statuses({"w1:p1": "idle"})
        time.sleep(GRACE * 0.4)
        self.server.set_statuses({"w1:p1": "working"})
        time.sleep(GRACE * 1.5)
        self.assertEqual(self.count("STOP"), 0)
        self.assertEqual(self.count("START"), 1)

    def test_pane_vanishing_while_working_still_releases(self):
        """No event says 'that agent is gone'; the poll notices the pane is absent."""
        self.start_daemon()
        self.server.set_statuses({"w1:p1": "working"})
        self.assertTrue(wait_for(lambda: self.count("START") == 1))
        self.server.set_statuses({})
        self.assertTrue(wait_for(lambda: self.count("STOP") == 1),
                        "a vanished pane stranded the inhibitor")

    # -- lifecycle ---------------------------------------------------------------

    def test_server_death_releases_and_exits_cleanly(self):
        proc = self.start_daemon()
        self.server.set_statuses({"w1:p1": "working"})
        self.assertTrue(wait_for(lambda: self.count("START") == 1))

        self.server.stop()
        self.assertEqual(proc.wait(timeout=10), 0, "daemon did not exit cleanly")
        self.assertEqual(self.count("STOP"), 1,
                         "daemon exited without releasing the inhibitor")

    def test_sigterm_releases_the_inhibitor(self):
        proc = self.start_daemon()
        self.server.set_statuses({"w1:p1": "working"})
        self.assertTrue(wait_for(lambda: self.count("START") == 1))
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual(self.count("STOP"), 1)

    def test_second_daemon_refuses_to_double_start(self):
        self.start_daemon()
        self.server.set_statuses({"w1:p1": "working"})
        self.assertTrue(wait_for(lambda: self.count("START") == 1))

        second = subprocess.run(
            [sys.executable, os.path.join(_support.ROOT, "src", "main.py"),
             "daemon", "--foreground", "--ensure"],
            env=self.env(), capture_output=True, text=True, timeout=15)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stderr.strip(), "", "--ensure should be silent")
        self.assertEqual(self.count("START"), 1, "a second inhibitor was spawned")

    def test_ensure_reports_nothing_but_plain_daemon_does(self):
        self.start_daemon()
        self.server.set_statuses({"w1:p1": "working"})
        self.assertTrue(wait_for(lambda: self.count("START") == 1))
        plain = subprocess.run(
            [sys.executable, os.path.join(_support.ROOT, "src", "main.py"),
             "daemon", "--foreground"],
            env=self.env(), capture_output=True, text=True, timeout=15)
        self.assertEqual(plain.returncode, 0)
        self.assertIn("already running", plain.stderr)

    # -- CLI ---------------------------------------------------------------------

    def test_status_reports_holding_then_idle(self):
        self.start_daemon()
        self.server.set_statuses({"w1:p1": "working"})
        self.assertTrue(wait_for(lambda: self.count("START") == 1))

        out = wait_for(lambda: (lambda r: r if "holding" in r.stdout else None)(
            self.run_cli("status")))
        self.assertIsNotNone(out, "status never reported holding")
        self.assertIn("w1:p1", out.stdout)

        payload = json.loads(self.run_cli("status", "--json").stdout)
        self.assertTrue(payload["running"])
        self.assertEqual(payload["state"]["active_panes"], ["w1:p1"])

    def test_status_without_a_daemon(self):
        result = self.run_cli("status")
        self.assertEqual(result.returncode, 0)
        self.assertIn("not running", result.stdout)

    def test_stop_command_releases_the_inhibitor(self):
        self.start_daemon()
        self.server.set_statuses({"w1:p1": "working"})
        self.assertTrue(wait_for(lambda: self.count("START") == 1))
        result = self.run_cli("stop")
        self.assertEqual(result.returncode, 0)
        self.assertIn("stopped", result.stdout)
        self.assertTrue(wait_for(lambda: self.count("STOP") == 1))

    def test_doctor_reports_a_reachable_server(self):
        self.server.set_statuses({"w1:p1": "working"})
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 0)
        self.assertIn("reachable, 1 pane", result.stdout)
        self.assertIn("w1:p1", result.stdout)

    def test_daemon_without_a_socket_exits_two(self):
        env = self.env()
        env.pop("HERDR_SOCKET_PATH")
        result = subprocess.run(
            [sys.executable, os.path.join(_support.ROOT, "src", "main.py"),
             "daemon", "--foreground"],
            env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn("HERDR_SOCKET_PATH", result.stderr)

    def test_detached_daemon_returns_immediately_and_keeps_running(self):
        """The startup hook must not be held open by the daemon it spawns."""
        began = time.time()
        result = subprocess.run(
            [sys.executable, os.path.join(_support.ROOT, "src", "main.py"), "daemon"],
            env=self.env(), capture_output=True, text=True, timeout=15)
        elapsed = time.time() - began
        self.assertEqual(result.returncode, 0)
        self.assertLess(elapsed, 5.0, "the hook was held open by its daemon")

        self.server.set_statuses({"w1:p1": "working"})
        self.assertTrue(wait_for(lambda: self.count("START") == 1),
                        "the detached daemon is not running")
        self.run_cli("stop")
        self.assertTrue(wait_for(lambda: self.count("STOP") == 1))


if __name__ == "__main__":
    unittest.main()
