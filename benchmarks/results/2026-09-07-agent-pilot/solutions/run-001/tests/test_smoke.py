import unittest
from ledger.csvio import read_events
from ledger.report import summarize


class SmokeTests(unittest.TestCase):
    def test_pipeline(self):
        events = read_events("event_id,sku,quantity,unit_price,when\n"
                             "e1,apple,2,1.25,2026-01-01T00:00:00Z\n")
        self.assertEqual(summarize(events), {
            "events": 1, "items": [{"sku": "apple", "quantity": 2,
                                     "value_cents": 250}],
            "total_value_cents": 250,
        })

    def test_empty(self):
        self.assertEqual(summarize(read_events("")), {
            "events": 0, "items": [], "total_value_cents": 0,
        })


if __name__ == "__main__":
    unittest.main()
