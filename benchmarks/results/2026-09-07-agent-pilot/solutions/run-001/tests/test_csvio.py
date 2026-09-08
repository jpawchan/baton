import csv
from datetime import datetime, timezone
import io
import unittest

from ledger.csvio import read_events


HEADER = "event_id,sku,quantity,unit_price,when\n"
WHEN = "2026-01-01T00:00:00Z"


def document(**changes):
    values = {"event_id": "e1", "sku": "apple", "quantity": "2",
              "unit_price": "1.25", "when": WHEN}
    values.update(changes)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(values)
    writer.writerow(values.values())
    return stream.getvalue()


class CSVTests(unittest.TestCase):
    def test_quoting_multiline_bom_reordered_header_and_whitespace(self):
        stream = io.StringIO(newline="")
        stream.write("\ufeff\r\n")
        writer = csv.writer(stream)
        writer.writerow([" when ", "unit_price", "quantity", " sku ", "event_id"])
        writer.writerow([" 2026-01-01T02:30:00+02:30 ", " 0001.2 ", " +0002 ",
                         '  Fresh,\r\n"apple"  ', ' Event "1" '])
        stream.write("\r\n")
        result = read_events(stream.getvalue())
        self.assertEqual(result, [{
            "event_id": 'Event "1"', "sku": 'Fresh,\r\n"apple"',
            "quantity": 2, "unit_cents": 120,
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }])
        self.assertIs(result[0]["timestamp"].tzinfo, timezone.utc)
        self.assertIs(type(result[0]["quantity"]), int)
        self.assertIs(type(result[0]["unit_cents"]), int)

    def test_empty_and_header_only(self):
        for text in ["", "\ufeff", "\n\r\n\r", "\ufeff\n", HEADER,
                     "\n" + HEADER + "\r\n", HEADER.rstrip("\n")]:
            with self.subTest(text=text):
                self.assertEqual(read_events(text), [])

    def test_invalid_headers(self):
        for header in [
            "event_id,sku,quantity,unit_price", HEADER.rstrip() + ",extra",
            "event_id,sku,quantity,unit_price,event_id",
            "event_id,sku,quantity,unit_price, when ,when",
            "EVENT_ID,sku,quantity,unit_price,when", ",,,,", " ", '""',
            "\ufeff\ufeff" + HEADER, "e1,apple,1,1.00," + WHEN,
        ]:
            with self.subTest(header=header), self.assertRaises(ValueError):
                read_events(header)

    def test_invalid_width_empty_fields_and_csv_syntax(self):
        for row in [
            "e1,apple,1,1.00", "e1,apple,1,1.00," + WHEN + ",extra",
            ",,,,", '"","","","",""', " ", '""',
            'e1,"apple,1,1.00,' + WHEN,
            'e1,"apple"junk,1,1.00,' + WHEN,
        ]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                read_events(HEADER + row)

    def test_nonblank_identifiers(self):
        for name in ["event_id", "sku"]:
            for value in ["", " \t\r\n "]:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    read_events(document(**{name: value}))

    def test_quantity_syntax_range_and_exact_type(self):
        valid = {"1": 1, "+0001": 1, "-0002": -2, "1000000": 1_000_000,
                 "-1000000": -1_000_000, "0" * 5000 + "1": 1}
        for value, expected in valid.items():
            with self.subTest(value=value[:30]):
                result = read_events(document(quantity=value))[0]["quantity"]
                self.assertEqual(result, expected)
                self.assertIs(type(result), int)
        invalid = ["0", "-0", "+000", "1000001", "-1000001", "1.0", "1e2",
                   "NaN", "inf", "1_000", "1 0", "1\n0", "++1", "+-1", "+",
                   "", "١", "１２", "9" * 5000]
        for value in invalid:
            with self.subTest(value=value[:30]), self.assertRaises(ValueError):
                read_events(document(quantity=value))

    def test_price_syntax_range_and_exact_cents(self):
        valid = {"0": 0, "0.0": 0, "000.00": 0, "0.01": 1, "0.29": 29,
                 "1.2": 120, "001.23": 123, "999999.99": 99_999_999,
                 "1000000.00": 100_000_000, "1000000": 100_000_000,
                 "0" * 5000 + "1.01": 101}
        for value, expected in valid.items():
            with self.subTest(value=value[:30]):
                result = read_events(document(unit_price=value))[0]["unit_cents"]
                self.assertEqual(result, expected)
                self.assertIs(type(result), int)
        invalid = ["+1", "-1", "-0", "1e2", "NaN", "nan", "inf", "Infinity",
                   "1.001", "0.000", ".25", "1.", "1000000.01", "1000001",
                   "1_000", "1 2", "١.٢", "１２.３４", "", "9" * 5000]
        for value in invalid:
            with self.subTest(value=value[:30]), self.assertRaises(ValueError):
                read_events(document(unit_price=value))

    def test_datetime_forms_and_utc_normalization(self):
        for value in [WHEN, "2025-12-31T19:00:00-05:00",
                      "2026-01-01 02:30:00+02:30", "20260101T000000+0000",
                      "2026-W01-4T00:00:00Z"]:
            with self.subTest(value=value):
                self.assertEqual(read_events(document(when=value))[0]["timestamp"],
                                 datetime(2026, 1, 1, tzinfo=timezone.utc))
        value = "2026-01-01T02:30:00.123456+02:30"
        self.assertEqual(read_events(document(when=value))[0]["timestamp"].microsecond,
                         123456)

    def test_invalid_or_naive_datetimes(self):
        for value in ["", "2026-01-01", "2026-01-01T00:00:00",
                      "2026-02-30T00:00:00Z", "not-a-date", "2026-01-01T00:00:00+24:00",
                      "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                read_events(document(when=value))

    def test_long_valid_fields_and_restoring_csv_limit(self):
        limit = csv.field_size_limit()
        sku = "x" * (limit + 1)
        self.assertEqual(read_events(document(sku=sku))[0]["sku"], sku)
        self.assertEqual(csv.field_size_limit(), limit)
        with self.assertRaises(ValueError):
            read_events(document(sku=sku, quantity="0"))
        self.assertEqual(csv.field_size_limit(), limit)

    def test_source_order_duplicates_and_no_partial_result(self):
        rows = [f"b,Apple,1,0.29,{WHEN}\n", f"a,apple,-1,0.30,{WHEN}\n"]
        events = read_events(HEADER + rows[0] + "\n" + rows[1] + rows[0])
        self.assertEqual([event["event_id"] for event in events], ["b", "a", "b"])
        self.assertEqual([event["sku"] for event in events], ["Apple", "apple", "Apple"])
        with self.assertRaises(ValueError):
            read_events(HEADER + "".join(rows) + "bad,row\n")


if __name__ == "__main__":
    unittest.main()
