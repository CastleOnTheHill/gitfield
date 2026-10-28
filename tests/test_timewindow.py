import unittest
from datetime import datetime, timezone

from gitfield.errors import GitfieldError
from gitfield.timewindow import parse_since, parse_until, parse_window


class TimeWindowTests(unittest.TestCase):
    def test_calendar_years_and_format(self):
        now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
        start, end = parse_window("2y", "2026-10-28", now=now)
        self.assertEqual(start, datetime(2024, 10, 28, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(
            end, datetime(2026, 10, 28, 23, 59, 59, 999999, tzinfo=timezone.utc)
        )

    def test_month_shift_clamps_leap_day(self):
        now = datetime(2024, 2, 29, 8, 30, tzinfo=timezone.utc)
        self.assertEqual(
            parse_since("1y", now), datetime(2023, 2, 28, 8, 30, tzinfo=timezone.utc)
        )

    def test_relative_units(self):
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)
        self.assertEqual(
            parse_since("730d", now), datetime(2024, 1, 11, tzinfo=timezone.utc)
        )
        self.assertEqual(
            parse_since("18mo", now), datetime(2024, 7, 10, tzinfo=timezone.utc)
        )

    def test_until_now_and_errors(self):
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)
        self.assertEqual(parse_until(None, now), now)
        with self.assertRaises(GitfieldError):
            parse_until("2y", now)
        with self.assertRaises(GitfieldError):
            parse_window("2026-02-01", "2026-01-01", now=now)
        with self.assertRaises(GitfieldError):
            parse_since("not-a-date", now)


if __name__ == "__main__":
    unittest.main()
