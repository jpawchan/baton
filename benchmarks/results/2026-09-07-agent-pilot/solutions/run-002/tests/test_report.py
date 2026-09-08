import unittest

from copy import deepcopy
from datetime import datetime, timezone, timedelta

from ledger.report import summarize


UTC = timezone.utc


def event(event_id, sku, quantity, unit_cents, timestamp):
    return {
        "event_id": event_id,
        "sku": sku,
        "quantity": quantity,
        "unit_cents": unit_cents,
        "timestamp": timestamp,
    }


class ReportTests(unittest.TestCase):
    def test_generator_input_is_consumed_without_mutating_input(self):
        source = [
            event("a", "apple", 2, 125,
                  datetime(2026, 1, 1, tzinfo=UTC)),
        ]
        original = deepcopy(source)

        result = summarize((item for item in source))

        self.assertEqual(result, {
            "events": 1,
            "items": [{"sku": "apple", "quantity": 2, "value_cents": 250}],
            "total_value_cents": 250,
        })
        self.assertEqual(source, original)

    def test_equivalent_offset_timestamps_deduplicate(self):
        events = [
            event("same", "apple", 2, 125,
                  datetime(2026, 1, 1, 0, 0, tzinfo=UTC)),
            event("same", "apple", 2, 125,
                  datetime(2025, 12, 31, 19, 0,
                            tzinfo=timezone(timedelta(hours=-5)))),
            event("other", "apple", 1, 100,
                  datetime(2026, 1, 1, 1, 0, tzinfo=UTC)),
        ]

        self.assertEqual(summarize(events), {
            "events": 2,
            "items": [{"sku": "apple", "quantity": 3, "value_cents": 350}],
            "total_value_cents": 350,
        })

    def test_conflicting_duplicate_is_rejected_even_out_of_window(self):
        events = [
            event("same", "apple", 1, 100,
                  datetime(2025, 1, 1, tzinfo=UTC)),
            event("same", "pear", 1, 100,
                  datetime(2025, 1, 1, tzinfo=UTC)),
        ]
        with self.assertRaises(ValueError):
            summarize(events, since=datetime(2026, 1, 1, tzinfo=UTC))

    def test_filter_bounds_are_absolute_inclusive_exclusive_and_equal(self):
        events = [
            event("before", "a", 1, 1,
                  datetime(2026, 1, 1, 0, 0, tzinfo=UTC)),
            event("start", "b", 1, 2,
                  datetime(2026, 1, 2, 0, 0,
                            tzinfo=timezone(timedelta(hours=2)))),
            event("end", "c", 1, 4,
                  datetime(2026, 1, 3, 0, 0, tzinfo=UTC)),
        ]
        since = datetime(2026, 1, 2, 0, 0,
                         tzinfo=timezone(timedelta(hours=2)))
        until = datetime(2026, 1, 3, 1, 0,
                         tzinfo=timezone(timedelta(hours=1)))

        self.assertEqual(summarize(events, since=since, until=until), {
            "events": 1,
            "items": [{"sku": "b", "quantity": 1, "value_cents": 2}],
            "total_value_cents": 2,
        })
        self.assertEqual(summarize(events, since=since, until=since), {
            "events": 0, "items": [], "total_value_cents": 0,
        })

    def test_reversed_or_naive_bounds_are_rejected(self):
        aware = datetime(2026, 1, 2, tzinfo=UTC)
        with self.assertRaises(ValueError):
            summarize([], since=aware, until=datetime(2026, 1, 1, tzinfo=UTC))
        with self.assertRaises(ValueError):
            summarize([], since=datetime(2026, 1, 1))
        with self.assertRaises(ValueError):
            summarize([], until=datetime(2026, 1, 1))

    def test_items_sort_case_sensitively_and_retain_zero_net_items(self):
        events = [
            event("z", "b", 1, 7, datetime(2026, 1, 1, tzinfo=UTC)),
            event("a", "A", 1, 3, datetime(2026, 1, 1, tzinfo=UTC)),
            event("c", "a", 1, 5, datetime(2026, 1, 1, tzinfo=UTC)),
            event("d", "b", -1, 7, datetime(2026, 1, 1, tzinfo=UTC)),
        ]

        self.assertEqual(summarize(events), {
            "events": 4,
            "items": [
                {"sku": "A", "quantity": 1, "value_cents": 3},
                {"sku": "a", "quantity": 1, "value_cents": 5},
                {"sku": "b", "quantity": 0, "value_cents": 0},
            ],
            "total_value_cents": 8,
        })

    def test_distinct_event_ids_count_separately(self):
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        self.assertEqual(summarize([
            event("x", "sku", 1, 10, ts),
            event("y", "sku", 1, 10, ts),
        ]), {
            "events": 2,
            "items": [{"sku": "sku", "quantity": 2, "value_cents": 20}],
            "total_value_cents": 20,
        })


if __name__ == "__main__":
    unittest.main()
