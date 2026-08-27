"""Activity detection: the screen hash, and the agent-pane carve-out."""

import _support  # noqa: F401
import unittest

import activity


class PaneHasAgentTest(unittest.TestCase):
    def test_plain_shell_has_no_agent(self):
        self.assertFalse(activity.pane_has_agent(
            {"pane_id": "w1:p1", "agent_status": "unknown"}))

    def test_missing_status_is_no_agent(self):
        self.assertFalse(activity.pane_has_agent({"pane_id": "w1:p1"}))

    def test_a_real_status_means_an_agent(self):
        for status in ("working", "idle", "blocked", "done"):
            self.assertTrue(activity.pane_has_agent({"agent_status": status}), status)

    def test_an_agent_name_alone_is_enough(self):
        self.assertTrue(activity.pane_has_agent(
            {"agent": "claude", "agent_status": "unknown"}))


class ActivityProbeTest(unittest.TestCase):
    def setUp(self):
        self.screens = {"w1:p1": "hello"}
        self.calls = []

        def read(pane_id):
            self.calls.append(pane_id)
            return self.screens.get(pane_id)

        self.probe = activity.ActivityProbe(read)

    def test_first_sample_is_a_baseline_not_activity(self):
        """A newly focused pane must not count as activity merely for existing."""
        self.assertFalse(self.probe.changed("w1:p1"))

    def test_unchanged_screen_is_not_activity(self):
        self.probe.changed("w1:p1")
        for _ in range(5):
            self.assertFalse(self.probe.changed("w1:p1"))

    def test_changed_screen_is_activity(self):
        self.probe.changed("w1:p1")
        self.screens["w1:p1"] = "hello\n$ ls"
        self.assertTrue(self.probe.changed("w1:p1"))

    def test_typing_without_enter_is_activity(self):
        self.probe.changed("w1:p1")
        self.screens["w1:p1"] = "hello$ half-typed"
        self.assertTrue(self.probe.changed("w1:p1"))

    def test_a_vanished_pane_is_not_activity(self):
        self.probe.changed("w1:p1")
        del self.screens["w1:p1"]
        self.assertFalse(self.probe.changed("w1:p1"))
        self.assertNotIn("w1:p1", self.probe.tokens)

    def test_a_read_error_is_not_activity(self):
        def boom(_pane_id):
            raise RuntimeError("socket exploded")
        self.assertFalse(activity.ActivityProbe(boom).changed("w1:p1"))

    def test_forget_drops_panes_that_no_longer_exist(self):
        self.screens["w2:p1"] = "other"
        self.probe.changed("w1:p1")
        self.probe.changed("w2:p1")
        self.probe.forget({"w1:p1"})
        self.assertEqual(set(self.probe.tokens), {"w1:p1"})


class AgentActivityTest(unittest.TestCase):
    PANES = [
        {"pane_id": "w1:p1", "workspace_id": "w1", "agent_status": "working"},
        {"pane_id": "w1:p2", "workspace_id": "w1", "agent_status": "idle"},
        {"pane_id": "w2:p1", "workspace_id": "w2", "agent_status": "working"},
    ]

    def test_only_counts_panes_in_the_named_workspace(self):
        self.assertEqual(activity.agent_activity(self.PANES, "w1", ["working"]),
                         ["w1:p1"])

    def test_respects_the_configured_statuses(self):
        self.assertEqual(
            sorted(activity.agent_activity(self.PANES, "w1", ["working", "idle"])),
            ["w1:p1", "w1:p2"])

    def test_no_matches_is_empty(self):
        self.assertEqual(activity.agent_activity(self.PANES, "w3", ["working"]), [])


if __name__ == "__main__":
    unittest.main()
