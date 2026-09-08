import unittest

from datetime import timezone

from ledger.csvio import read_events


HEADER = "event_id,sku,quantity,unit_price,when"
ROW = "e1,apple,2,1.25,2026-01-01T00:00:00Z"


class CsvIoTests(unittest.TestCase):
    def test_empty_and_header_only_input(self):
        self.assertEqual(read_events(""), [])
        self.assertEqual(read_events("\n\n"), [])
        self.assertEqual(read_events(HEADER + "\n"), [])
        self.assertEqual(read_events(HEADER + "\n\n"), [])

    def test_bom_reordered_quoted_and_multiline_fields(self):
        text = (
            "\ufeff when , sku , event_id , unit_price , quantity \n"
            "2026-01-01T01:00:00+01:00,\" red\napple \",\"e,1\", 1.2 , +2\n"
        )

        events = read_events(text)

        self.assertEqual(events, [{
            "event_id": "e,1",
            "sku": "red\napple",
            "quantity": 2,
            "unit_cents": 120,
            "timestamp": __import__("datetime").datetime(
                2026, 1, 1, tzinfo=timezone.utc
            ),
        }])

    def test_one_leading_bom_only(self):
        with self.assertRaises(ValueError):
            read_events("\ufeff\ufeff" + HEADER + "\n" + ROW + "\n")

    def test_whitespace_and_all_empty_records_are_not_skipped(self):
        with self.assertRaises(ValueError):
            read_events(HEADER + "\n   \n")
        with self.assertRaises(ValueError):
            read_events(HEADER + "\n,,,,\n")
        with self.assertRaises(ValueError):
            read_events(HEADER + "\n\"\"\n")

    def test_header_must_have_each_expected_name_exactly_once(self):
        bad_headers = [
            "event_id,sku,quantity,unit_price",
            "event_id,sku,quantity,unit_price,when,extra",
            "event_id,sku,quantity,unit_price,event_id",
            "event_id,sku,quantity,unit_price,WHEN",
        ]
        for header in bad_headers:
            with self.subTest(header=header):
                with self.assertRaises(ValueError):
                    read_events(header + "\n" + ROW + "\n")

    def test_wrong_width_and_malformed_csv_raise(self):
        with self.assertRaises(ValueError):
            read_events(HEADER + "\ne1,apple,2,1.25\n")
        with self.assertRaises(ValueError):
            read_events(HEADER + "\n\"e1,apple,2,1.25,2026-01-01T00:00:00Z\n")

    def test_bad_later_row_does_not_return_partial_result(self):
        with self.assertRaises(ValueError):
            read_events(HEADER + "\n" + ROW + "\n" +
                        "e2,pear,not-a-number,2.00,2026-01-01T00:00:00Z\n")

    def test_quantity_ascii_grammar_and_range(self):
        valid = {
            "1": 1,
            "+0002": 2,
            "-3": -3,
            "1000000": 1000000,
            "-1000000": -1000000,
        }
        for raw, expected in valid.items():
            with self.subTest(raw=raw):
                event = read_events(
                    HEADER + f"\ne,sku,{raw},1,2026-01-01T00:00:00Z\n"
                )[0]
                self.assertEqual(event["quantity"], expected)

        invalid = ["0", "+0", "-0", "1000001", "-1000001", "1.0", "١"]
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    read_events(
                        HEADER + f"\ne,sku,{raw},1,2026-01-01T00:00:00Z\n"
                    )

    def test_unit_price_uses_exact_cents_and_grammar(self):
        prices = {
            "0": 0,
            "0.5": 50,
            "0.05": 5,
            "1.2": 120,
            "1.25": 125,
            "1000000.00": 100000000,
        }
        for raw, expected in prices.items():
            with self.subTest(raw=raw):
                event = read_events(
                    HEADER + f"\ne,sku,1,{raw},2026-01-01T00:00:00Z\n"
                )[0]
                self.assertEqual(event["unit_cents"], expected)
                self.assertIsInstance(event["unit_cents"], int)

        invalid = [
            "-1", "+1", "1.", ".5", "1.234", "1e2", "nan",
            "1000000.01", "١.٢", "１２",
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    read_events(
                        HEADER + f"\ne,sku,1,{raw},2026-01-01T00:00:00Z\n"
                    )

    def test_ids_and_skus_must_be_nonblank_after_stripping(self):
        for event_id, sku in [("", "sku"), ("   ", "sku"), ("event", ""),
                              ("event", "  ")]:
            with self.subTest(event_id=event_id, sku=sku):
                with self.assertRaises(ValueError):
                    read_events(
                        HEADER + f"\n{event_id},{sku},1,1,2026-01-01T00:00:00Z\n"
                    )

    def test_timestamp_requires_offset_and_is_normalized_to_utc(self):
        events = read_events(
            HEADER + "\ne,sku,1,1,2026-01-01T01:00:00+01:00\n"
        )
        self.assertEqual(events[0]["timestamp"].tzinfo, timezone.utc)
        self.assertEqual(events[0]["timestamp"].isoformat(),
                         "2026-01-01T00:00:00+00:00")

        for raw in [
            "2026-01-01T00:00:00",
            "not-a-timestamp",
            "2026-01-01",
            "2026-01-01T00:00:00+99:00",
        ]:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    read_events(HEADER + f"\ne,sku,1,1,{raw}\n")

    def test_source_order_and_exact_event_shape(self):
        events = read_events(
            HEADER + "\ne2,B,1,1.01,2026-01-01T00:00:00Z\n"
            "e1,a,-1,0.10,2026-01-01T00:00:01Z\n"
        )
        self.assertEqual([event["event_id"] for event in events], ["e2", "e1"])
        self.assertEqual(set(events[0]), {
            "event_id", "sku", "quantity", "unit_cents", "timestamp"
        })
        self.assertIsInstance(events[0]["quantity"], int)
        self.assertIsInstance(events[0]["unit_cents"], int)


if __name__ == "__main__":
    unittest.main()
