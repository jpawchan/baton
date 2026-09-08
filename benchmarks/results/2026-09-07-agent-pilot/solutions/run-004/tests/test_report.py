from copy import deepcopy
from datetime import datetime, timedelta, timezone, tzinfo
from itertools import permutations
import unittest

from ledger.report import summarize


NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
EMPTY = {"events": 0, "items": [], "total_value_cents": 0}


def event(event_id="e1", sku="apple", quantity=2, unit_cents=125, timestamp=NOW):
    return dict(event_id=event_id, sku=sku, quantity=quantity,
                unit_cents=unit_cents, timestamp=timestamp)


class FoldTimezone(tzinfo):
    """A local repeated hour, without relying on a system timezone database."""

    def utcoffset(self, dt):
        return timedelta(hours=1 - dt.fold)

    def dst(self, dt):
        return timedelta(0)


class NoOffsetTimezone(tzinfo):
    def utcoffset(self, dt):
        return None


class ReportTests(unittest.TestCase):
    def test_empty_iterable(self):
        self.assertEqual(summarize(iter(())), EMPTY)

    def test_signed_values_sorting_and_zero_net_items(self):
        events = [event("e1", "apple", 2, 125), event("e2", "apple", -2, 150),
                  event("e3", "Apple", 3, 50), event("e4", "banana", -1, 200)]
        expected = {
            "events": 4,
            "items": [{"sku": "Apple", "quantity": 3, "value_cents": 150},
                      {"sku": "apple", "quantity": 0, "value_cents": -50},
                      {"sku": "banana", "quantity": -1, "value_cents": -200}],
            "total_value_cents": -100,
        }
        for ordered in permutations(events):
            with self.subTest(order=[row["event_id"] for row in ordered]):
                self.assertEqual(summarize(iter(ordered)), expected)

    def test_identical_duplicates_and_distinct_case_sensitive_ids(self):
        rows = [event(), event(), event("E1"), event("e2")]
        self.assertEqual(summarize(rows), {
            "events": 3, "items": [{"sku": "apple", "quantity": 6, "value_cents": 750}],
            "total_value_cents": 750,
        })

    def test_duplicate_timestamp_equality_is_absolute(self):
        local = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
        self.assertEqual(summarize([event(), event(timestamp=local)]), summarize([event()]))

    def test_each_conflicting_field_is_rejected(self):
        for change in ({"sku": "Apple"}, {"quantity": -2}, {"unit_cents": 126},
                       {"timestamp": NOW + timedelta(microseconds=1)}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                summarize([event(), event(**change)])

    def test_conflicts_are_rejected_even_outside_or_in_empty_windows(self):
        conflicts = [event(), event(quantity=3)]
        for bounds in ({"since": NOW + timedelta(days=1)}, {"until": NOW},
                       {"since": NOW, "until": NOW}):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                summarize(iter(conflicts), **bounds)
        for rows in ([event(), event(timestamp=NOW + timedelta(days=1))],
                     [event(timestamp=NOW + timedelta(days=1)), event()]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                summarize(rows, since=NOW, until=NOW + timedelta(hours=1))

    def test_half_open_window_and_optional_endpoints(self):
        rows = [event("before", timestamp=NOW - timedelta(microseconds=1)),
                event("start"), event("middle", timestamp=NOW + timedelta(hours=1)),
                event("end", timestamp=NOW + timedelta(days=1))]
        since = NOW.astimezone(timezone(timedelta(hours=-4)))
        until = (NOW + timedelta(days=1)).astimezone(timezone(timedelta(hours=3)))
        self.assertEqual(summarize(rows, since=since, until=until), summarize(rows[1:3]))
        self.assertEqual(summarize(rows, since=since), summarize(rows[1:]))
        self.assertEqual(summarize(rows, until=until), summarize(rows[:3]))
        self.assertEqual(summarize(rows, since=since, until=NOW), EMPTY)

    def test_bounds_must_be_aware_datetimes(self):
        for name in ("since", "until"):
            for value in ("2026-01-02T00:00:00Z", 0, NOW.date(),
                          NOW.replace(tzinfo=None), NOW.replace(tzinfo=NoOffsetTimezone())):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    summarize([], **{name: value})
        with self.assertRaises(ValueError):
            summarize([], since=NOW + timedelta(microseconds=1), until=NOW)

    def test_folded_local_times_use_absolute_order_and_equality(self):
        zone = FoldTimezone()
        early = datetime(2026, 1, 2, 1, tzinfo=zone, fold=0)  # 00:00 UTC
        late = early.replace(fold=1)  # 01:00 UTC, despite matching wall time
        with self.assertRaises(ValueError):
            summarize([], since=late, until=early)
        with self.assertRaises(ValueError):
            summarize([event(timestamp=early), event(timestamp=late)])
        self.assertEqual(summarize([event(), event(timestamp=early)]), summarize([event()]))
        self.assertEqual(summarize([event()], since=early, until=late), summarize([event()]))

    def test_aware_bounds_at_datetime_extremes(self):
        since = datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))
        until = datetime.max.replace(tzinfo=timezone(timedelta(hours=-1)))
        self.assertEqual(summarize([event()], since=since, until=until), summarize([event()]))

    def test_generator_and_input_records_are_not_mutated(self):
        rows = [event(), event(), event("e2", quantity=-2)]
        before = deepcopy(rows)
        result = summarize(row for row in rows)
        self.assertEqual(rows, before)
        result["items"][0]["quantity"] = 999
        self.assertEqual(rows, before)


if __name__ == "__main__":
    unittest.main()
