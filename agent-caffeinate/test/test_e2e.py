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


class DaemonHarness(TempDirCase):
    """Fixtures only -- fake server, fake inhibitor, daemon plumbing."""

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


class DaemonE2ETest(DaemonHarness):
    # -- the core behaviour --------------------------------------------------

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


class WedgedDaemonTakeoverTest(DaemonHarness):
    """A daemon that is alive but not progressing must not block its own replacement.

    On a Herdr restart the startup hook runs, finds the lock held, and would otherwise
    exit silently -- leaving the new server with no daemon at all, forever, because the
    session key is derived from the socket path and so collides with the wedged one.
    """

    def wedge(self, updated_at=None):
        """Start a process that holds the session lock and never reports progress."""
        import daemonize
        key = daemonize.session_key(self.sock_path)
        session_dir = self.path("state", key)
        os.makedirs(session_dir, exist_ok=True)
        script = (
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "os.ftruncate(fd, 0); os.write(fd, str(os.getpid()).encode())\n"
            "sys.stdout.write('locked\\n'); sys.stdout.flush()\n"
            "time.sleep(600)\n")
        proc = subprocess.Popen([sys.executable, "-c", script,
                                 os.path.join(session_dir, "daemon.lock")],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.procs.append(proc)
        self.assertEqual(proc.stdout.readline().strip(), b"locked")
        if updated_at is not None:
            with open(os.path.join(session_dir, "daemon.json"), "w") as fh:
                json.dump({"pid": proc.pid, "updated_at": updated_at,
                           "holding": False}, fh)
        return proc

    def test_a_wedged_daemon_is_displaced(self):
        wedged = self.wedge(updated_at=time.time() - 3600)   # an hour behind
        self.start_daemon()
        self.server.set_statuses({"w1:p1": "working"})
        self.assertTrue(wait_for(lambda: self.count("START") == 1, timeout=25),
                        "the replacement daemon never took over")
        self.assertIsNotNone(wedged.poll(), "the wedged daemon was left running")

    def test_a_healthy_daemon_is_never_displaced(self):
        """The dangerous false positive: killing a daemon that is working fine."""
        self.start_daemon()
        self.server.set_statuses({"w1:p1": "working"})
        self.assertTrue(wait_for(lambda: self.count("START") == 1))
        first = self.procs[0]

        second = subprocess.run(
            [sys.executable, os.path.join(_support.ROOT, "src", "main.py"),
             "daemon", "--foreground"],
            env=self.env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 0)
        self.assertIn("already running", second.stderr)
        self.assertIsNone(first.poll(), "a healthy daemon was killed")
        self.assertEqual(self.count("START"), 1)

    def test_a_holder_that_recovers_during_the_confirm_is_left_alone(self):
        """A machine waking from sleep leaves a stale file and then catches up."""
        import daemonize
        key = daemonize.session_key(self.sock_path)
        session_dir = self.path("state", key)
        wedged = self.wedge(updated_at=time.time() - 3600)

        def revive():
            time.sleep(1.0)
            with open(os.path.join(session_dir, "daemon.json"), "w") as fh:
                json.dump({"pid": wedged.pid, "updated_at": time.time(),
                           "holding": False}, fh)

        import threading
        threading.Thread(target=revive, daemon=True).start()

        result = subprocess.run(
            [sys.executable, os.path.join(_support.ROOT, "src", "main.py"),
             "daemon", "--foreground"],
            env=self.env(HERDR_CAFFEINATE_POLL_INTERVAL_SEC="1"),
            capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(wedged.poll(), "a daemon that caught up was killed anyway")
        self.assertIn("recovered on its own", result.stderr)

    def test_no_status_file_means_no_takeover(self):
        """Never displace a holder we know nothing about."""
        wedged = self.wedge(updated_at=None)      # lock held, no daemon.json at all
        result = subprocess.run(
            [sys.executable, os.path.join(_support.ROOT, "src", "main.py"),
             "daemon", "--foreground"],
            env=self.env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertIn("already running", result.stderr)
        self.assertIsNone(wedged.poll())

    def test_takeover_reaps_the_wedged_daemons_inhibitor(self):
        """A displaced daemon's caffeinate must not survive it."""
        import daemonize
        key = daemonize.session_key(self.sock_path)
        session_dir = self.path("state", key)
        os.makedirs(session_dir, exist_ok=True)

        orphan = subprocess.Popen([FAKE_CAFFEINATE], env=self.env(),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        self.procs.append(orphan)
        with open(os.path.join(session_dir, "inhibitor.json"), "w") as fh:
            json.dump({"pid": orphan.pid, "argv": [FAKE_CAFFEINATE], "at": time.time()},
                      fh)
        self.wedge(updated_at=time.time() - 3600)

        self.start_daemon()
        self.assertTrue(wait_for(lambda: orphan.poll() is not None, timeout=25),
                        "the displaced daemon's inhibitor was left holding")
