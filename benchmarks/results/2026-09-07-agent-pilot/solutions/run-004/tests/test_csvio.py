import csv
from datetime import datetime, timezone
from io import StringIO
import unittest

from ledger.csvio import read_events


HEADER = "event_id,sku,quantity,unit_price,when\n"
WHEN = "2026-01-02T03:04:05Z"


def event_csv(**changes):
    row = dict(event_id="e1", sku="apple", quantity="2", unit_price="1.25", when=WHEN)
    row.update(changes)
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(row)
    writer.writerow(row.values())
    return stream.getvalue()


class CsvTests(unittest.TestCase):
    def test_bom_reordered_header_whitespace_and_multiline_quoting(self):
        text = ('\ufeff\r\n when , unit_price ,sku, event_id , quantity \r\n'
                ' 2026-01-02T08:34:05+05:30 , 0001.2 ,"  a,""b""\r\nc  ", e1 , +02 \r\n'
                '\r\n'
                '2026-01-02T03:04:05Z,0,A,e2,-1\r\n')
        self.assertEqual(read_events(text), [
            {"event_id": "e1", "sku": 'a,"b"\r\nc', "quantity": 2, "unit_cents": 120,
             "timestamp": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)},
            {"event_id": "e2", "sku": "A", "quantity": -1, "unit_cents": 0,
             "timestamp": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)},
        ])

    def test_empty_and_header_only(self):
        for text in ("", "\ufeff", "\n\r\n\r", HEADER, "\n" + HEADER + "\r\n"):
            with self.subTest(text=text):
                self.assertEqual(read_events(text), [])

    def test_invalid_headers(self):
        for text in (
            " ", '\"\"\n', ",,,,\n", "event_id,sku,quantity,unit_price\n",
            "event_id,sku,quantity,unit_price,when,extra\n",
            "event_id,sku,quantity,unit_price,quantity\n",
            "event_id,sku,quantity,unit_price,when, when \n",
            "Event_id,sku,quantity,unit_price,when\n", "\ufeff\ufeff" + HEADER,
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                read_events(text)

    def test_only_physically_empty_records_are_skipped(self):
        valid = f"e1,apple,2,1.25,{WHEN}\n"
        self.assertEqual(len(read_events(HEADER + "\n\r\n" + valid + "\n")), 1)
        for row in (" \n", '\"\"\n', ",,,,\n", '\"\",\"\",\"\",\"\",\"\"\n',
                    f"e1,apple,2,1.25\n", f"e1,apple,2,1.25,{WHEN},extra\n"):
            with self.subTest(row=row), self.assertRaises(ValueError):
                read_events(HEADER + valid + row)

    def test_nonblank_ids_and_skus(self):
        for field in ("event_id", "sku"):
            for value in ("", " \t\r\n"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    read_events(event_csv(**{field: value}))
        result = read_events(event_csv(event_id="  E1  ", sku="  ApPle  "))[0]
        self.assertEqual((result["event_id"], result["sku"]), ("E1", "ApPle"))

    def test_quantities(self):
        for value, expected in (("1", 1), ("+0002", 2), ("-03", -3),
                                ("1000000", 1_000_000), ("-1000000", -1_000_000),
                                (" \t+12 ", 12), ("0" * 5000 + "1", 1)):
            with self.subTest(value=value[:40]):
                actual = read_events(event_csv(quantity=value))[0]["quantity"]
                self.assertIs(type(actual), int)
                self.assertEqual(actual, expected)

    def test_invalid_quantities(self):
        for value in ("", "0", "+0", "-000", "1000001", "-1000001", "1.0", "1e2",
                      "١", "１", "++1", "+-1", "--1", "1_000", "1 2", "- 1", "9" * 5000):
            with self.subTest(value=value[:40]), self.assertRaises(ValueError):
                read_events(event_csv(quantity=value))

    def test_exact_prices(self):
        for value, cents in (("0", 0), ("0.0", 0), ("0.01", 1), ("0.29", 29),
                             ("1.2", 120), ("001.23", 123), ("1000000", 100_000_000),
                             ("1000000.00", 100_000_000), (" 1.09 ", 109),
                             ("0" * 5000 + "1.23", 123)):
            with self.subTest(value=value[:40]):
                actual = read_events(event_csv(unit_price=value))[0]["unit_cents"]
                self.assertIs(type(actual), int)
                self.assertEqual(actual, cents)

    def test_invalid_prices(self):
        for value in ("", "-1", "-0", "+1", ".1", "1.", "1.234", "0.000", "1e2",
                      "NaN", "nan", "Infinity", "inf", "١.٢", "1.２", "1_000", "1 2",
                      "1000000.01", "1000001", "9" * 5000):
            with self.subTest(value=value[:40]), self.assertRaises(ValueError):
                read_events(event_csv(unit_price=value))

    def test_iso_variants_and_utc_normalization(self):
        for value in (WHEN, "2026-01-02 03:04:05+00:00", "20260102T030405Z",
                      "2026-W01-5T03:04:05Z", "2026-01-01T22:04:05-05:00",
                      "2026-01-02T03:04:05.123456+00:00:30.5"):
            with self.subTest(value=value):
                timestamp = read_events(event_csv(when=value))[0]["timestamp"]
                self.assertIs(timestamp.tzinfo, timezone.utc)
                self.assertEqual(timestamp, datetime.fromisoformat(value).astimezone(timezone.utc))
        self.assertEqual(read_events(event_csv(when="2026-01-02T08:34:05+05:30"))[0]["timestamp"],
                         datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))

    def test_invalid_or_naive_timestamps(self):
        for value in ("", "2026-01-02", "2026-01-02T03:04:05", "not a date",
                      "2026-02-30T00:00:00Z", "2026-01-02T03:04:60Z",
                      "2026-01-02T03:04:05+24:00", "0001-01-01T00:00:00+01:00",
                      "9999-12-31T23:59:59-01:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                read_events(event_csv(when=value))

    def test_malformed_quoting_raises_value_error(self):
        for row in ('e1,"unterminated,2,1.25,' + WHEN,
                    'e1,"apple"x,2,1.25,' + WHEN):
            with self.subTest(row=row), self.assertRaises(ValueError):
                read_events(HEADER + row)

    def test_long_fields_and_csv_field_limit_restoration(self):
        previous_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(32)
            sku = "x" * 150_000
            self.assertEqual(read_events(event_csv(sku=sku))[0]["sku"], sku)
            self.assertEqual(csv.field_size_limit(), 32)
            with self.assertRaises(ValueError):
                read_events(event_csv(sku=sku, quantity="invalid"))
            self.assertEqual(csv.field_size_limit(), 32)
        finally:
            csv.field_size_limit(previous_limit)

    def test_duplicates_remain_in_source_order(self):
        rows = [f"e2,Apple,1,1,{WHEN}\n", f"e1,apple,2,1,{WHEN}\n"]
        events = read_events(HEADER + rows[0] + rows[1] + rows[0])
        self.assertEqual([event["event_id"] for event in events], ["e2", "e1", "e2"])
        self.assertEqual(events[0], events[2])
        self.assertIsNot(events[0], events[2])
        self.assertEqual(set(events[0]), {"event_id", "sku", "quantity", "unit_cents", "timestamp"})


if __name__ == "__main__":
    unittest.main()
