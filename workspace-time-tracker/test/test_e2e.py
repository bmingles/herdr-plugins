"""End to end: the real daemon against a fake Herdr server.

The daemon runs as a subprocess exactly as Herdr would run it and speaks the real socket
protocol. Only the server is faked.
"""

import _support  # noqa: F401
import json
import os
import signal
import subprocess
import sys
import time
import unittest

import daemonize

from _support import ENTRYPOINT, TempDirCase
from fake_server import FakeHerdrServer

IDLE = 2.0
POLL = 0.2
SNAP = 0.2


def wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def panes(*specs):
    """('w1:p1', 'w1', 'unknown', True) -> a pane record."""
    return [{"pane_id": p, "workspace_id": w, "agent_status": s, "focused": f}
            for (p, w, s, f) in specs]


class TrackerE2ETest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.sock_path = self.path("herdr.sock")
        self.server = FakeHerdrServer(self.sock_path, self.path("scenario.json"))
        self.server.set()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.entries_path = self.path("entries.jsonl")
        self.procs = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
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
            "HERDR_TRACK_ENTRIES_PATH": self.entries_path,
            "HERDR_TRACK_IDLE_TIMEOUT_SEC": str(IDLE),
            "HERDR_TRACK_POLL_INTERVAL_SEC": str(POLL),
            "HERDR_TRACK_SNAPSHOT_INTERVAL_SEC": str(SNAP),
            "HERDR_TRACK_MIN_ENTRY_SEC": "0",
            "HERDR_TRACK_LOG_LEVEL": "debug",
        })
        env.update(extra)
        return env

    def start_daemon(self, **kwargs):
        proc = subprocess.Popen([sys.executable, ENTRYPOINT, "daemon", "--foreground"],
                                env=self.env(**kwargs),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.procs.append(proc)
        return proc

    def cli(self, *args, **kwargs):
        return subprocess.run([sys.executable, ENTRYPOINT] + list(args),
                              env=self.env(**kwargs), capture_output=True, text=True,
                              timeout=30)

    def status_file(self):
        """Read the daemon's status directly.

        Shelling out to `status` inside a polling loop spawns an interpreter per
        iteration, which dominates the runtime of the whole suite.
        """
        key = daemonize.session_key(self.sock_path)
        try:
            with open(self.path("state", key, "daemon.json")) as fh:
                return json.load(fh)
        except (IOError, ValueError):
            return None

    def open_label(self):
        state = self.status_file() or {}
        return ((state.get("open") or {}).get("label"))

    def entries(self):
        try:
            with open(self.entries_path) as fh:
                return [json.loads(l) for l in fh if l.strip()]
        except IOError:
            return []

    def labels(self):
        return [e.get("label") for e in self.entries()]

    # -- core behaviour ----------------------------------------------------------

    def test_switching_spaces_writes_an_entry_for_the_first(self):
        self.start_daemon()
        self.server.set(focused_workspace="w1",
                        workspaces=[{"workspace_id": "w1", "label": "alpha"},
                                    {"workspace_id": "w2", "label": "beta"}],
                        panes=panes(("w1:p1", "w1", "working", True)))
        self.assertTrue(wait_for(lambda: self.open_label() == "alpha"),
                        "never started tracking alpha")
        time.sleep(0.6)
        self.server.set(focused_workspace="w2",
                        workspaces=[{"workspace_id": "w1", "label": "alpha"},
                                    {"workspace_id": "w2", "label": "beta"}],
                        panes=panes(("w2:p1", "w2", "working", True)))
        got = wait_for(lambda: self.entries() or None)
        self.assertIsNotNone(got, "no entry written on switch")
        self.assertEqual(got[0]["label"], "alpha")
        self.assertEqual(got[0]["end_reason"], "switch")

    def test_a_working_agent_keeps_the_entry_open_past_the_idle_timeout(self):
        self.start_daemon()
        self.server.set(panes=panes(("w1:p1", "w1", "working", True)))
        self.assertTrue(wait_for(lambda: self.open_label() == "alpha"))
        time.sleep(IDLE * 2.5)
        self.assertEqual(self.entries(), [],
                         "closed an entry while an agent was working")

    def test_quiet_closes_the_entry_backdated(self):
        self.start_daemon()
        self.server.set(panes=panes(("w1:p1", "w1", "working", True)))
        self.assertTrue(wait_for(lambda: self.open_label() == "alpha"))
        time.sleep(0.8)
        self.server.set(panes=panes(("w1:p1", "w1", "idle", True)))
        got = wait_for(lambda: self.entries() or None)
        self.assertIsNotNone(got, "quiet never closed the entry")
        entry = got[0]
        self.assertEqual(entry["end_reason"], "idle")
        self.assertLess(entry["seconds"], IDLE + 2,
                        "the idle window was billed as work")

    def test_a_plain_shell_typing_counts_as_activity(self):
        """The probe-18 case: a plain pane emits no events, only its screen changes."""
        self.start_daemon()
        screens = {"w1:p1": "prompt$ "}
        self.server.set(panes=panes(("w1:p1", "w1", "unknown", True)), screens=screens)
        self.assertFalse(wait_for(lambda: self.open_label() == "alpha",
                                  timeout=1.0),
                         "focus alone should not open an entry before any activity")
        for i in range(12):
            screens = {"w1:p1": "prompt$ " + "x" * i}
            self.server.set(panes=panes(("w1:p1", "w1", "unknown", True)),
                            screens=screens)
            time.sleep(0.25)
        self.assertEqual(self.open_label(), "alpha",
                         "typing in a plain shell was not detected as activity")
        self.assertEqual(self.entries(), [], "entry closed while typing continued")

    def test_an_agent_pane_is_never_screen_hashed(self):
        """An animating agent UI must not be able to keep an entry alive by itself."""
        self.start_daemon()
        for i in range(14):
            # The screen churns every sample, exactly like a spinner -- but the pane has
            # a detected agent that is idle, so it must not count as activity.
            self.server.set(panes=panes(("w1:p1", "w1", "idle", True)),
                            screens={"w1:p1": "spinner frame %d" % i})
            time.sleep(0.25)
        self.assertEqual(self.entries(), [])
        self.assertIsNone(self.open_label(),
                          "an animating agent pane was treated as activity")

    def test_server_death_closes_the_open_entry(self):
        proc = self.start_daemon()
        self.server.set(panes=panes(("w1:p1", "w1", "working", True)))
        self.assertTrue(wait_for(lambda: self.open_label() == "alpha"))
        time.sleep(0.5)
        self.server.stop()
        self.assertEqual(proc.wait(timeout=15), 0)
        got = self.entries()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["end_reason"], "shutdown")

    def test_sigterm_closes_the_open_entry(self):
        proc = self.start_daemon()
        self.server.set(panes=panes(("w1:p1", "w1", "working", True)))
        self.assertTrue(wait_for(lambda: self.open_label() == "alpha"))
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=15), 0)
        self.assertEqual(len(self.entries()), 1)

    def test_a_crashed_daemon_is_recovered_on_next_start(self):
        proc = self.start_daemon()
        self.server.set(panes=panes(("w1:p1", "w1", "working", True)))
        self.assertTrue(wait_for(lambda: self.open_label() == "alpha"))
        time.sleep(0.6)
        proc.kill()                       # SIGKILL: no chance to write the entry
        proc.wait(timeout=10)
        self.assertEqual(self.entries(), [], "entry written despite SIGKILL?")

        self.start_daemon()
        got = wait_for(lambda: self.entries() or None)
        self.assertIsNotNone(got, "the stranded segment was never recovered")
        self.assertEqual(got[0]["end_reason"], "recovered")
        self.assertEqual(got[0]["label"], "alpha")

    def test_short_entries_are_discarded_when_configured(self):
        self.start_daemon(HERDR_TRACK_MIN_ENTRY_SEC="30")
        self.server.set(focused_workspace="w1",
                        workspaces=[{"workspace_id": "w1", "label": "alpha"},
                                    {"workspace_id": "w2", "label": "beta"}],
                        panes=panes(("w1:p1", "w1", "working", True)))
        self.assertTrue(wait_for(lambda: self.open_label() == "alpha"))
        self.server.set(focused_workspace="w2",
                        workspaces=[{"workspace_id": "w1", "label": "alpha"},
                                    {"workspace_id": "w2", "label": "beta"}],
                        panes=panes(("w2:p1", "w2", "working", True)))
        time.sleep(1.5)
        self.assertEqual(self.entries(), [], "a sub-threshold entry was written")

    # -- CLI ---------------------------------------------------------------------

    def test_report_on_an_empty_file(self):
        result = self.cli("report")
        self.assertEqual(result.returncode, 0)
        self.assertIn("no entries", result.stdout)

    def test_report_json_shape(self):
        self.start_daemon()
        self.server.set(panes=panes(("w1:p1", "w1", "working", True)))
        self.assertTrue(wait_for(lambda: self.open_label() == "alpha"))
        time.sleep(0.5)
        self.cli("stop")
        self.assertTrue(wait_for(lambda: self.entries() or None))
        payload = json.loads(self.cli("report", "--json").stdout)
        self.assertEqual(set(payload), {"v", "range", "by", "groups", "total_seconds",
                                        "overlapping"})
        self.assertEqual(payload["groups"][0]["key"], "alpha")

    def test_report_rejects_a_bad_date(self):
        result = self.cli("report", "--day", "last tuesday")
        self.assertEqual(result.returncode, 64)
        self.assertIn("YYYY-MM-DD", result.stderr)

    def test_status_without_a_daemon(self):
        result = self.cli("status")
        self.assertEqual(result.returncode, 0)
        self.assertIn("not running", result.stdout)

    def test_doctor_reports_the_focused_space(self):
        self.server.set(panes=panes(("w1:p1", "w1", "unknown", True)))
        result = self.cli("doctor")
        self.assertEqual(result.returncode, 0)
        self.assertIn("reachable", result.stdout)
        self.assertIn("alpha", result.stdout)

    def test_second_daemon_does_not_double_start(self):
        self.start_daemon()
        self.server.set(panes=panes(("w1:p1", "w1", "working", True)))
        self.assertTrue(wait_for(lambda: self.open_label() == "alpha"))
        second = subprocess.run([sys.executable, ENTRYPOINT, "daemon", "--foreground",
                                 "--ensure"], env=self.env(), capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stderr.strip(), "")

    def test_daemon_without_a_socket_exits_two(self):
        env = self.env()
        env.pop("HERDR_SOCKET_PATH")
        result = subprocess.run([sys.executable, ENTRYPOINT, "daemon", "--foreground"],
                                env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
