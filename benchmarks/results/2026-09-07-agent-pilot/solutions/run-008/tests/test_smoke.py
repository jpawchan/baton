import unittest
from datetime import datetime, timezone
from retry_after import retry_delay


class SmokeTests(unittest.TestCase):
    def test_numeric_and_cap(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(retry_delay("12", now), 12.0)
        self.assertEqual(retry_delay("999", now, cap=20), 20.0)

    def test_missing(self):
        self.assertEqual(retry_delay(None, datetime.now(timezone.utc)), 1.0)


if __name__ == "__main__":
    unittest.main()
