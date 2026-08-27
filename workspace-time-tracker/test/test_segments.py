"""The segment state machine: every timing rule, on a frozen clock."""

import _support  # noqa: F401
import datetime
import unittest

import segments as seg_mod
from _support import FakeClock

WS_A = {"workspace_id": "w1", "label": "alpha", "cwd": "/a"}
WS_B = {"workspace_id": "w2", "label": "beta", "cwd": "/b"}


class SegmentTrackerTest(unittest.TestCase):
    def make(self, idle=60.0, min_entry=0.0):
        self.clock = FakeClock()
        return seg_mod.SegmentTracker(idle, min_entry, session="default",
                                      host="testhost", clock=self.clock)

    def work(self, tracker, workspace, seconds, step=10.0):
        """Simulate `seconds` of continuous work as the daemon would see it.

        The daemon polls every couple of seconds and reports activity each time it sees
        any, so a test that jumps minutes forward in one call is modelling an idle gap,
        not work -- feeding the polls is what makes the timing assertions meaningful.
        """
        remaining = float(seconds)
        entries = []
        while remaining > 0:
            advance = min(step, remaining)
            self.clock.advance(advance)
            entries += tracker.update(workspace, True)
            remaining -= advance
        return entries

    def test_activity_opens_an_entry(self):
        t = self.make()
        self.assertEqual(t.update(WS_A, True), [])
        self.assertIsNotNone(t.current)
        self.assertEqual(t.current.workspace_id, "w1")

    def test_focus_without_activity_opens_nothing(self):
        t = self.make()
        self.assertEqual(t.update(WS_A, False), [])
        self.assertIsNone(t.current)

    def test_switching_closes_and_opens(self):
        t = self.make()
        t.update(WS_A, True)
        self.work(t, WS_A, 300)
        entries = t.update(WS_B, True)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["label"], "alpha")
        self.assertEqual(entries[0]["seconds"], 300)
        self.assertEqual(entries[0]["end_reason"], "switch")
        self.assertEqual(t.current.workspace_id, "w2")

    def test_idle_close_is_backdated_and_excludes_the_dead_time(self):
        """The whole point: an idle minute must not be billed as work."""
        t = self.make(idle=60.0)
        t.update(WS_A, True)
        self.work(t, WS_A, 600)          # ten minutes of real work
        self.clock.advance(61)           # then quiet
        entries = t.update(WS_A, False)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["seconds"], 600, "the idle window was counted")
        self.assertEqual(entries[0]["end_reason"], "idle")
        self.assertIsNone(t.current)

    def test_idle_does_not_fire_early(self):
        t = self.make(idle=60.0)
        t.update(WS_A, True)
        self.clock.advance(59)
        self.assertEqual(t.update(WS_A, False), [])
        self.assertIsNotNone(t.current)

    def test_activity_after_idle_opens_a_fresh_entry(self):
        t = self.make(idle=60.0)
        t.update(WS_A, True)
        self.clock.advance(100)
        t.update(WS_A, False)            # closes by idle
        self.assertIsNone(t.current)
        self.clock.advance(3600)
        t.update(WS_A, True)
        self.assertIsNotNone(t.current)
        self.assertEqual(t.current.start, self.clock.now)

    def test_short_entries_are_discarded(self):
        t = self.make(idle=60.0, min_entry=30.0)
        t.update(WS_A, True)
        self.work(t, WS_A, 12, step=2)
        entries = t.update(WS_B, True)
        self.assertEqual(entries, [])
        self.assertEqual(t.discarded, 1)

    def test_entries_at_the_threshold_are_kept(self):
        t = self.make(idle=60.0, min_entry=30.0)
        t.update(WS_A, True)
        self.work(t, WS_A, 30, step=2)
        self.assertEqual(len(t.update(WS_B, True)), 1)

    def test_midnight_splits_into_two_entries(self):
        t = self.make(idle=3600.0)
        self.clock.set(2026, 8, 27, 23, 30, 0)
        t.update(WS_A, True)
        self.clock.set(2026, 8, 28, 0, 20, 0)
        entries = t.update(WS_A, True)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["end_reason"], "rollover")
        self.assertTrue(entries[0]["start"].startswith("2026-08-27T23:30"))
        self.assertTrue(entries[0]["end"].startswith("2026-08-27T23:59:59"))
        self.assertIsNotNone(t.current)
        self.assertTrue(seg_mod.iso(t.current.start).startswith("2026-08-28T00:00:00"))

    def test_a_sleep_across_several_days_produces_one_entry_per_day(self):
        t = self.make(idle=86400.0 * 10)
        self.clock.set(2026, 8, 25, 22, 0, 0)
        t.update(WS_A, True)
        self.clock.set(2026, 8, 28, 1, 0, 0)
        entries = t.update(WS_A, True)
        self.assertEqual([e["end_reason"] for e in entries],
                         ["rollover", "rollover", "rollover"])
        days = [e["start"][:10] for e in entries]
        self.assertEqual(days, ["2026-08-25", "2026-08-26", "2026-08-27"])

    def test_workspace_going_away_closes_the_entry(self):
        t = self.make()
        t.update(WS_A, True)
        self.work(t, WS_A, 120)
        entries = t.update(None, False)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["end_reason"], "closed")

    def test_close_uses_the_last_activity(self):
        t = self.make(idle=600.0)
        t.update(WS_A, True)
        self.work(t, WS_A, 100)
        self.clock.advance(50)           # quiet, but not yet timed out
        entries = t.close()
        self.assertEqual(entries[0]["seconds"], 100)
        self.assertEqual(entries[0]["end_reason"], "shutdown")

    def test_rename_mid_segment_uses_the_new_label(self):
        t = self.make()
        t.update(WS_A, True)
        self.work(t, WS_A, 100)
        renamed = dict(WS_A, label="alpha-renamed")
        self.work(t, renamed, 100)
        entries = t.update(WS_B, True)
        self.assertEqual(entries[0]["label"], "alpha-renamed")

    def test_recovery_of_a_crashed_daemons_segment(self):
        t = self.make()
        state = {"workspace_id": "w1", "label": "alpha", "cwd": "/a",
                 "start": self.clock.now - 500, "last_activity": self.clock.now - 200}
        entries = t.recover(state)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["end_reason"], "recovered")
        self.assertEqual(entries[0]["seconds"], 300)
        self.assertIsNone(t.current)

    def test_recovery_of_a_corrupt_state_is_ignored(self):
        t = self.make()
        self.assertEqual(t.recover({"nonsense": True}), [])

    def test_entry_shape_is_the_documented_contract(self):
        t = self.make()
        t.update(WS_A, True)
        self.work(t, WS_A, 90)
        entry = t.update(WS_B, True)[0]
        self.assertEqual(set(entry), {"v", "workspace_id", "label", "cwd", "start",
                                      "end", "seconds", "end_reason", "session",
                                      "host"})
        self.assertEqual(entry["v"], 1)
        self.assertEqual(entry["session"], "default")
        self.assertEqual(entry["host"], "testhost")

    def test_cwd_is_omitted_never_null(self):
        t = self.make()
        ws = {"workspace_id": "w1", "label": "alpha", "cwd": None}
        t.update(ws, True)
        self.work(t, ws, 90)
        entry = t.update(WS_B, True)[0]
        self.assertNotIn("cwd", entry)

    def test_seconds_matches_the_timestamps(self):
        t = self.make()
        t.update(WS_A, True)
        self.work(t, WS_A, 1792)
        entry = t.update(WS_B, True)[0]
        start = datetime.datetime.fromisoformat(entry["start"])
        end = datetime.datetime.fromisoformat(entry["end"])
        self.assertEqual(entry["seconds"], round((end - start).total_seconds()))

    def test_seconds_until_idle_close_counts_down(self):
        t = self.make(idle=60.0)
        t.update(WS_A, True)
        self.assertAlmostEqual(t.seconds_until_idle_close(), 60.0, places=3)
        self.clock.advance(45)
        self.assertAlmostEqual(t.seconds_until_idle_close(), 15.0, places=3)


if __name__ == "__main__":
    unittest.main()
