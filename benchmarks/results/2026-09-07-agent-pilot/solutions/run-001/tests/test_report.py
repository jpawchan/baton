from copy import deepcopy
from datetime import datetime, timedelta, timezone, tzinfo
from itertools import permutations
import unittest

from ledger.report import summarize


UTC = timezone.utc
NOW = datetime(2026, 1, 1, tzinfo=UTC)
EMPTY = {"events": 0, "items": [], "total_value_cents": 0}


def event(event_id="e1", sku="apple", quantity=1, unit_cents=125, timestamp=NOW):
    return {"event_id": event_id, "sku": sku, "quantity": quantity,
            "unit_cents": unit_cents, "timestamp": timestamp}


class FoldTimezone(tzinfo):
    def utcoffset(self, dt):
        return timedelta(hours=1 - dt.fold)

    def dst(self, dt):
        return timedelta(0)


class NoOffsetTimezone(tzinfo):
    def utcoffset(self, dt):
        return None


class ReportTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(summarize(iter([])), EMPTY)

    def test_aggregation_sorting_zero_net_and_no_mutation(self):
        events = [event("e1", "apple", 2, 125), event("e2", "Apple", -3, 25),
                  event("e3", "apple", -2, 100), event("e4", "z", 1, 0),
                  event("e5", "Apple", 1, 25)]
        before = deepcopy(events)
        self.assertEqual(summarize(iter(events)), {
            "events": 5,
            "items": [{"sku": "Apple", "quantity": -2, "value_cents": -50},
                      {"sku": "apple", "quantity": 0, "value_cents": 50},
                      {"sku": "z", "quantity": 1, "value_cents": 0}],
            "total_value_cents": 0,
        })
        self.assertEqual(events, before)

    def test_equal_ids_deduplicate_by_absolute_timestamp(self):
        original = event()
        offset = timezone(timedelta(hours=-5))
        duplicate = event(timestamp=NOW.astimezone(offset))
        result = summarize([original, duplicate, event("e2"), event("E1")])
        self.assertEqual(result, {"events": 3,
                                 "items": [{"sku": "apple", "quantity": 3,
                                            "value_cents": 375}],
                                 "total_value_cents": 375})

    def test_every_conflicting_field_is_checked_outside_window(self):
        changes = [{"sku": "Apple"}, {"quantity": -1}, {"unit_cents": 126},
                   {"timestamp": NOW + timedelta(days=1)}]
        windows = [{}, {"since": NOW + timedelta(days=2)}, {"until": NOW},
                   {"since": NOW, "until": NOW},
                   {"since": NOW, "until": NOW + timedelta(hours=1)}]
        for change in changes:
            for window in windows:
                for reverse in [False, True]:
                    with self.subTest(change=change, window=window, reverse=reverse):
                        records = [event(), event(**change)]
                        if reverse:
                            records.reverse()
                        with self.assertRaises(ValueError):
                            summarize(iter(records), **window)

    def test_inclusive_since_exclusive_until(self):
        events = [event("before", timestamp=NOW - timedelta(microseconds=1)),
                  event("start"), event("middle", timestamp=NOW + timedelta(hours=1)),
                  event("end", timestamp=NOW + timedelta(hours=2))]
        since = NOW.astimezone(timezone(timedelta(hours=4)))
        until = (NOW + timedelta(hours=2)).astimezone(timezone(timedelta(hours=-3)))
        self.assertEqual(summarize(events, since, until)["events"], 2)
        self.assertEqual(summarize(events, since=since)["events"], 3)
        self.assertEqual(summarize(events, until=until)["events"], 3)
        self.assertEqual(summarize(events, since=NOW, until=NOW), EMPTY)
        self.assertEqual(summarize([event(), event()], until=NOW), EMPTY)

    def test_invalid_boundaries_even_without_events(self):
        for value in [datetime(2026, 1, 1), "2026-01-01T00:00:00Z", 0,
                      datetime(2026, 1, 1, tzinfo=NoOffsetTimezone())]:
            for name in ["since", "until"]:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    summarize([], **{name: value})
        with self.assertRaises(ValueError):
            summarize([], since=NOW + timedelta(seconds=1), until=NOW)
        # Compare instants, not clock-face values or offset strings.
        earlier = datetime(2026, 1, 1, 2, tzinfo=timezone(timedelta(hours=3)))
        self.assertEqual(summarize([], since=earlier, until=NOW), EMPTY)
        with self.assertRaises(ValueError):
            summarize([], since=NOW, until=earlier)

    def test_aware_extreme_boundaries(self):
        lower = datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))
        upper = datetime.max.replace(tzinfo=timezone(timedelta(hours=-1)))
        self.assertEqual(summarize([event()], lower, upper)["events"], 1)
        self.assertEqual(summarize([event()], since=upper), EMPTY)

    def test_absolute_comparisons_across_folds(self):
        zone = FoldTimezone()
        first = NOW.replace(tzinfo=zone, fold=0)
        second = NOW.replace(tzinfo=zone, fold=1)
        with self.assertRaises(ValueError):
            summarize([event(timestamp=first), event(timestamp=second)])
        events = [event("first", timestamp=first), event("second", timestamp=second)]
        self.assertEqual(summarize(events, since=first, until=second)["events"], 1)
        with self.assertRaises(ValueError):
            summarize([], since=second, until=first)

    def test_determinism_for_input_permutations(self):
        events = [event("b", "b", -1), event("a", "A", 4), event("c", "a", 2),
                  event("b", "b", -1)]
        expected = summarize(events)
        for order in permutations(events):
            self.assertEqual(summarize(iter(order)), expected)

    def test_iterator_reusing_a_mutable_record(self):
        shared = event()

        def records():
            yield shared
            shared["quantity"] = 2
            yield shared

        with self.assertRaises(ValueError):
            summarize(records())


if __name__ == "__main__":
    unittest.main()
