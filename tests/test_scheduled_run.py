#!/usr/bin/env python3
import datetime as dt
import importlib.util
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("scheduled_run", ROOT / "scheduled_run.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
BEIJING = ZoneInfo("Asia/Shanghai")
WINDOWS = tuple(MODULE.parse_window(x) for x in MODULE.DEFAULT_WINDOWS)


class ScheduleWindowTests(unittest.TestCase):
    def now(self, hour, minute, day=5):
        return dt.datetime(2026, 8, day, hour, minute, tzinfo=BEIJING)

    def test_midday_boundaries(self):
        self.assertIsNone(MODULE.matching_window(self.now(12, 29), WINDOWS))
        self.assertIsNotNone(MODULE.matching_window(self.now(12, 30), WINDOWS))
        self.assertIsNotNone(MODULE.matching_window(self.now(13, 29), WINDOWS))
        self.assertIsNone(MODULE.matching_window(self.now(13, 30), WINDOWS))

    def test_night_boundaries(self):
        self.assertIsNone(MODULE.matching_window(self.now(21, 59), WINDOWS))
        self.assertIsNotNone(MODULE.matching_window(self.now(22, 0), WINDOWS))
        self.assertIsNotNone(MODULE.matching_window(self.now(7, 59, day=6), WINDOWS))
        self.assertIsNone(MODULE.matching_window(self.now(8, 0, day=6), WINDOWS))

    def test_after_midnight_uses_previous_logical_date(self):
        now = self.now(2, 0, day=6)
        window = MODULE.matching_window(now, WINDOWS)
        self.assertEqual(MODULE.logical_date(now, window), dt.date(2026, 8, 5))

    def test_midday_uses_current_logical_date(self):
        now = self.now(12, 45)
        window = MODULE.matching_window(now, WINDOWS)
        self.assertEqual(MODULE.logical_date(now, window), dt.date(2026, 8, 5))

    def test_remaining_minutes_across_midnight(self):
        now = self.now(23, 0)
        window = MODULE.matching_window(now, WINDOWS)
        self.assertEqual(MODULE.remaining_minutes(now, window), 9 * 60)

    def test_remaining_minutes_at_midday(self):
        now = self.now(12, 45)
        window = MODULE.matching_window(now, WINDOWS)
        self.assertEqual(MODULE.remaining_minutes(now, window), 45)


if __name__ == "__main__":
    unittest.main()
