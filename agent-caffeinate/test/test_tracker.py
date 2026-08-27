"""The active/idle state machine. Every timing case uses a fake clock, never a sleep."""

import _support  # noqa: F401  (path setup)
import unittest

from _support import FakeClock
from tracker import START, STOP, Tracker


class TrackerTest(unittest.TestCase):
    def make(self, grace=60.0, active=("working",)):
        self.clock = FakeClock()
        return Tracker(active, grace, clock=self.clock)

    def test_starts_when_a_pane_begins_working(self):
        t = self.make()
        self.assertEqual(t.update({"w1:p1": "working"}), START)
        self.assertTrue(t.holding)

    def test_no_repeat_start_while_already_holding(self):
        t = self.make()
        t.update({"w1:p1": "working"})
        for _ in range(200):
            self.assertIsNone(t.update({"w1:p1": "working"}))

    def test_idle_alone_never_starts(self):
        t = self.make()
        self.assertIsNone(t.update({"w1:p1": "idle", "w1:p2": "unknown"}))
        self.assertFalse(t.holding)

    def test_stops_only_after_the_full_grace(self):
        t = self.make(grace=60.0)
        t.update({"w1:p1": "working"})
        self.assertIsNone(t.update({"w1:p1": "idle"}))
        self.clock.advance(59.9)
        self.assertIsNone(t.update({"w1:p1": "idle"}))
        self.clock.advance(0.2)
        self.assertEqual(t.update({"w1:p1": "idle"}), STOP)
        self.assertFalse(t.holding)

    def test_activity_during_grace_cancels_the_stop(self):
        t = self.make(grace=60.0)
        t.update({"w1:p1": "working"})
        t.update({"w1:p1": "idle"})
        self.clock.advance(59.0)
        self.assertIsNone(t.update({"w1:p1": "working"}))  # back to work
        self.clock.advance(30.0)
        self.assertIsNone(t.update({"w1:p1": "working"}))  # still held
        self.assertTrue(t.holding)
        self.assertIsNone(t.seconds_until_stop())

    def test_second_agent_keeps_it_held(self):
        t = self.make(grace=10.0)
        t.update({"a": "working", "b": "working"})
        self.assertIsNone(t.update({"a": "idle", "b": "working"}))
        self.clock.advance(60.0)
        self.assertIsNone(t.update({"a": "idle", "b": "working"}))
        self.assertTrue(t.holding)

    def test_pane_vanishing_while_working_is_not_stranded(self):
        """The whole point of polling: the map is the truth, so a lost pane just goes."""
        t = self.make(grace=5.0)
        t.update({"w1:p1": "working"})
        self.clock.advance(1.0)
        self.assertIsNone(t.update({}))       # pane gone from the snapshot entirely
        self.clock.advance(5.1)
        self.assertEqual(t.update({}), STOP)

    def test_blocked_is_not_active_by_default(self):
        t = self.make(grace=1.0)
        self.assertIsNone(t.update({"w1:p1": "blocked"}))
        self.assertFalse(t.holding)

    def test_blocked_can_be_opted_in(self):
        t = self.make(grace=1.0, active=("working", "blocked"))
        self.assertEqual(t.update({"w1:p1": "blocked"}), START)

    def test_unknown_status_is_inert(self):
        t = self.make()
        self.assertIsNone(t.update({"w1:p1": "banana"}))

    def test_seconds_until_stop_counts_down(self):
        t = self.make(grace=30.0)
        t.update({"w1:p1": "working"})
        self.assertIsNone(t.seconds_until_stop())
        t.update({"w1:p1": "idle"})
        self.assertAlmostEqual(t.seconds_until_stop(), 30.0, places=3)
        self.clock.advance(10.0)
        self.assertAlmostEqual(t.seconds_until_stop(), 20.0, places=3)

    def test_next_wakeup_never_overshoots_the_deadline(self):
        t = self.make(grace=30.0)
        t.update({"w1:p1": "working"})
        self.assertEqual(t.next_wakeup(2.0), 2.0)
        t.update({"w1:p1": "idle"})
        self.clock.advance(29.5)
        self.assertAlmostEqual(t.next_wakeup(2.0), 0.5, places=3)

    def test_active_panes_is_sorted(self):
        t = self.make()
        t.update({"w2:p9": "working", "w1:p1": "working", "w3:p3": "idle"})
        self.assertEqual(t.active_panes(), ["w1:p1", "w2:p9"])

    def test_idle_since_does_not_reset_while_idle(self):
        t = self.make(grace=10.0)
        t.update({"w1:p1": "working"})
        t.update({"w1:p1": "idle"})
        self.clock.advance(5.0)
        t.update({"w1:p1": "idle"})
        self.clock.advance(5.1)
        self.assertEqual(t.update({"w1:p1": "idle"}), STOP)


if __name__ == "__main__":
    unittest.main()


class TransitionJournalTest(unittest.TestCase):
    """The diagnostic that tells us what idleGraceSec should actually be."""

    def setUp(self):
        from tracker import TransitionJournal
        self.clock = FakeClock()
        self.j = TransitionJournal(clock=self.clock)

    def test_first_sighting_is_announced(self):
        self.assertEqual(self.j.observe({"w1:p1": "idle"}),
                         ["status w1:p1 appeared as idle"])

    def test_no_change_is_silent(self):
        self.j.observe({"w1:p1": "working"})
        for _ in range(10):
            self.clock.advance(2)
            self.assertEqual(self.j.observe({"w1:p1": "working"}), [])

    def test_a_false_idle_gap_is_directly_readable(self):
        self.j.observe({"w1:p1": "working"})
        self.clock.advance(30.0)
        self.j.observe({"w1:p1": "idle"})       # agent still working, rules lost it
        self.clock.advance(8.4)
        lines = self.j.observe({"w1:p1": "working"})
        self.assertEqual(lines, ["status w1:p1 idle -> working (was idle for 8.4s)"])

    def test_vanishing_pane_is_reported_with_its_last_status(self):
        self.j.observe({"w1:p1": "working"})
        self.clock.advance(5.0)
        self.assertEqual(self.j.observe({}),
                         ["status w1:p1 vanished while working (after 5.0s)"])

    def test_tracks_several_panes_independently(self):
        self.j.observe({"a": "working", "b": "idle"})
        self.clock.advance(3.0)
        lines = self.j.observe({"a": "working", "b": "working"})
        self.assertEqual(len(lines), 1)
        self.assertIn("status b idle -> working", lines[0])

    def test_a_reappearing_pane_starts_fresh(self):
        self.j.observe({"w1:p1": "working"})
        self.j.observe({})
        self.clock.advance(100.0)
        self.assertEqual(self.j.observe({"w1:p1": "idle"}),
                         ["status w1:p1 appeared as idle"])
