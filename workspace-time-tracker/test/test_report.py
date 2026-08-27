"""Aggregation and the --json contract."""

import _support  # noqa: F401
import datetime
import unittest

import report as report_mod


def entry(label, start, seconds, workspace_id="w1"):
    started = datetime.datetime.fromisoformat(start)
    ended = started + datetime.timedelta(seconds=seconds)
    return {"v": 1, "workspace_id": workspace_id, "label": label,
            "start": started.astimezone().isoformat(timespec="seconds"),
            "end": ended.astimezone().isoformat(timespec="seconds"),
            "seconds": seconds, "end_reason": "switch", "session": "default",
            "host": "h"}


DAY = "2026-08-27"
ENTRIES = [
    entry("alpha", DAY + "T09:00:00", 3600),
    entry("alpha", DAY + "T11:00:00", 1800),
    entry("beta", DAY + "T13:00:00", 900, workspace_id="w2"),
    entry("alpha", "2026-08-26T09:00:00", 600),
]


class ReportTest(unittest.TestCase):
    def day(self, value):
        return datetime.date.fromisoformat(value)

    def test_json_envelope_is_the_documented_shape(self):
        summary = report_mod.summarise(ENTRIES, since=self.day(DAY),
                                       until=self.day(DAY))
        self.assertEqual(set(summary), {"v", "range", "by", "groups", "total_seconds",
                                        "overlapping"})
        self.assertEqual(set(summary["groups"][0]), {"key", "seconds", "entries"})
        self.assertEqual(summary["v"], 1)

    def test_groups_by_label_biggest_first(self):
        summary = report_mod.summarise(ENTRIES, since=self.day(DAY), until=self.day(DAY))
        self.assertEqual([g["key"] for g in summary["groups"]], ["alpha", "beta"])
        self.assertEqual(summary["groups"][0]["seconds"], 5400)
        self.assertEqual(summary["groups"][0]["entries"], 2)
        self.assertEqual(summary["total_seconds"], 6300)

    def test_the_range_filter_excludes_other_days(self):
        summary = report_mod.summarise(ENTRIES, since=self.day(DAY), until=self.day(DAY))
        self.assertEqual(summary["total_seconds"], 6300)   # excludes the 26th
        wide = report_mod.summarise(ENTRIES, since=self.day("2026-08-26"),
                                    until=self.day(DAY))
        self.assertEqual(wide["total_seconds"], 6900)

    def test_group_by_workspace(self):
        summary = report_mod.summarise(ENTRIES, by="workspace", since=self.day(DAY),
                                       until=self.day(DAY))
        self.assertEqual([g["key"] for g in summary["groups"]], ["w1", "w2"])

    def test_group_by_day_is_chronological(self):
        summary = report_mod.summarise(ENTRIES, by="day", since=self.day("2026-08-26"),
                                       until=self.day(DAY))
        self.assertEqual([g["key"] for g in summary["groups"]],
                         ["2026-08-26", "2026-08-27"])

    def test_empty_input(self):
        summary = report_mod.summarise([], since=self.day(DAY), until=self.day(DAY))
        self.assertEqual(summary["groups"], [])
        self.assertEqual(summary["total_seconds"], 0)
        self.assertIn("no entries", report_mod.render(summary))

    def test_overlap_is_detected_and_flagged(self):
        overlapping = [entry("alpha", DAY + "T09:00:00", 3600),
                       entry("beta", DAY + "T09:30:00", 3600)]
        summary = report_mod.summarise(overlapping, since=self.day(DAY),
                                       until=self.day(DAY))
        self.assertTrue(summary["overlapping"])
        self.assertIn("overlap", report_mod.render(summary))

    def test_adjacent_entries_do_not_count_as_overlapping(self):
        adjacent = [entry("alpha", DAY + "T09:00:00", 3600),
                    entry("beta", DAY + "T10:00:00", 3600)]
        summary = report_mod.summarise(adjacent, since=self.day(DAY),
                                       until=self.day(DAY))
        self.assertFalse(summary["overlapping"])

    def test_seconds_are_recomputed_when_the_field_is_missing(self):
        broken = dict(ENTRIES[0])
        del broken["seconds"]
        summary = report_mod.summarise([broken], since=self.day(DAY),
                                       until=self.day(DAY))
        self.assertEqual(summary["total_seconds"], 3600)

    def test_parse_day_keywords(self):
        today = datetime.date(2026, 8, 27)
        self.assertEqual(report_mod.parse_day("today", today), today)
        self.assertEqual(report_mod.parse_day("yesterday", today),
                         datetime.date(2026, 8, 26))
        self.assertEqual(report_mod.parse_day("2026-01-05", today),
                         datetime.date(2026, 1, 5))
        with self.assertRaises(ValueError):
            report_mod.parse_day("last tuesday", today)

    def test_duration_formatting(self):
        self.assertEqual(report_mod.format_duration(0), "0s")
        self.assertEqual(report_mod.format_duration(45), "45s")
        self.assertEqual(report_mod.format_duration(90), "1m")
        self.assertEqual(report_mod.format_duration(3600), "1h 00m")
        self.assertEqual(report_mod.format_duration(11520), "3h 12m")

    def test_render_includes_every_group_and_a_total(self):
        summary = report_mod.summarise(ENTRIES, since=self.day(DAY), until=self.day(DAY))
        text = report_mod.render(summary, DAY)
        self.assertIn("alpha", text)
        self.assertIn("beta", text)
        self.assertIn("total", text)
        self.assertIn("1h 45m", text)


if __name__ == "__main__":
    unittest.main()
