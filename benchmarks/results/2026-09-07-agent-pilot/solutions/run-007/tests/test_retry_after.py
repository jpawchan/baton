import unittest
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from fractions import Fraction

from retry_after import retry_delay


class NoOffset(tzinfo):
    def utcoffset(self, dt):
        return None


class InvalidOffset(tzinfo):
    def utcoffset(self, dt):
        return 60


class RetryDelayTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.headers = (None, "", "bad", "3", "Thu, 01 Jan 2026 00:00:05 GMT")

    def assert_delay(self, expected, value, now=None, **kwargs):
        result = retry_delay(value, self.now if now is None else now, **kwargs)
        self.assertIs(type(result), float)
        self.assertEqual(result, expected)

    def test_invalid_now_on_every_header_path(self):
        cases = (
            None,
            False,
            0,
            "2026-01-01T00:00:00Z",
            date(2026, 1, 1),
            datetime(2026, 1, 1),
            datetime(2026, 1, 1, tzinfo=NoOffset()),
            datetime(2026, 1, 1, tzinfo=InvalidOffset()),
        )
        for index, now in enumerate(cases):
            for header in self.headers:
                with self.subTest(case=index, header=header):
                    with self.assertRaises(ValueError):
                        retry_delay(header, now, cap=0)

    def test_invalid_default_and_cap_on_every_header_path(self):
        cases = (
            ("none", None),
            ("true", True),
            ("false", False),
            ("string", "2"),
            ("complex", 2 + 0j),
            ("decimal", Decimal("2")),
            ("fraction", Fraction(2, 1)),
            ("negative int", -1),
            ("negative float", -0.01),
            ("nan", float("nan")),
            ("infinity", float("inf")),
            ("negative infinity", float("-inf")),
            ("huge int", 10**5000),
            ("huge negative int", -(10**5000)),
        )
        for name in ("default", "cap"):
            for label, value in cases:
                for header in self.headers:
                    with self.subTest(argument=name, case=label, header=header):
                        kwargs = {"default": 0, "cap": 0, name: value}
                        with self.assertRaises(ValueError):
                            retry_delay(header, self.now, **kwargs)

    def test_fallback_is_clamped_and_float(self):
        for default, cap, expected in (
            (1, 300, 1.0),
            (7, 3, 3.0),
            (2.5, 4, 2.5),
            (0, 4, 0.0),
            (-0.0, 4, 0.0),
            (4, 0, 0.0),
            (10**308, 10**308, float(10**308)),
        ):
            for header in (None, "", " \t\r\n ", "bad", 12, b"12"):
                with self.subTest(default=default, cap=cap, header=header):
                    self.assert_delay(expected, header, default=default, cap=cap)

    def test_non_string_headers_are_not_coerced(self):
        for header in (12, 3.5, True, False, b"12", ["12"], {"delay": 12}, self.now):
            with self.subTest(header=header):
                self.assert_delay(2.0, header, default=2, cap=10)

    def test_ascii_delays_and_long_headers(self):
        cases = (
            ("0", 20, 0.0),
            ("00012", 20, 12.0),
            (" \t12\r\n ", 20, 12.0),
            ("\u200312\u2003", 20, 12.0),
            ("999", 20, 20.0),
            ("1", 0.25, 0.25),
            ("9" * 5000, 20, 20.0),
            ("9" * 5000, 10**308, float(10**308)),
            ("0" * 5000, 20, 0.0),
            ("0" * 4999 + "7", 20, 7.0),
        )
        for index, (header, cap, expected) in enumerate(cases):
            with self.subTest(case=index):
                self.assert_delay(expected, header, cap=cap)

    def test_malformed_numeric_forms_use_fallback(self):
        for header in (
            "+12", "-12", "-0", "1.5", "1.", ".5", "1e2", "1E+2",
            "1_000", "0x10", "1 2", "12\n34", "NaN", "inf", "Infinity",
            "\u0661\u0662", "\uff11\uff12", "\u00b2", "1\u0662", "12\x00",
        ):
            with self.subTest(header=header):
                self.assert_delay(2.0, header, default=2, cap=10)

    def test_aware_date_forms_preserve_fractional_delay(self):
        now = self.now.replace(microsecond=250000)
        for header in (
            "Thu, 01 Jan 2026 00:00:02 GMT",
            "Thursday, 01-Jan-26 00:00:02 GMT",
            "01 Jan 2026 00:00:02 +0000",
            " \tThu, 01 Jan 2026 00:00:02 GMT\r\n",
            "Thu, 01 Jan 2026 01:00:02 +0100",
            "Wed, 31 Dec 2025 19:00:02 -0500",
        ):
            with self.subTest(header=header):
                self.assert_delay(1.75, header, now=now)

    def test_date_delay_uses_absolute_instants(self):
        now = datetime(2026, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1)))
        self.assert_delay(
            5400.0, "Thu, 01 Jan 2026 00:00:00 -0100", now=now, cap=6000
        )
        self.assert_delay(0.0, "Thu, 01 Jan 2026 01:00:00 +0200")
        fractional_offset = timezone(timedelta(hours=1, microseconds=500000))
        now = datetime(2026, 1, 1, 1, tzinfo=fractional_offset)
        self.assert_delay(0.5, "Thu, 01 Jan 2026 00:00:00 GMT", now=now)

    def test_date_delay_is_clamped(self):
        self.assert_delay(0.0, "Wed, 31 Dec 2025 23:59:59 GMT")
        self.assert_delay(0.0, "Thu, 01 Jan 2026 00:00:00 GMT")
        self.assert_delay(300.0, "Fri, 02 Jan 2026 00:00:00 GMT")
        self.assert_delay(0.5, "Thu, 01 Jan 2026 00:00:01 GMT", cap=0.5)

    def test_invalid_and_naive_dates_use_fallback(self):
        for header in (
            "Thu, 01 Jan 2026 00:00:05",
            "Thu Jan  1 00:00:05 2026",
            "Thu, 01 Jan 2026 00:00:05 -0000",
            "Thu, 01 Jan 2026 00:00:05 UNKNOWN",
            "Thu, 32 Jan 2026 00:00:05 GMT",
            "Thu, 01 Jan 2026 25:00:05 GMT",
            "Thu, 01 Jan 2026 00:00:60 GMT",
            "Thu, 01 Jan 2026 00:00:05 +2500",
            "Thu, 01 Jan 10000 00:00:05 GMT",
        ):
            with self.subTest(header=header):
                self.assert_delay(2.0, header, default=8, cap=2)

    def test_zero_cap_on_every_header_path(self):
        for header in self.headers + ("9" * 5000,):
            with self.subTest(header_length=len(header) if header else 0):
                self.assert_delay(0.0, header, default=8, cap=0)

    def test_datetime_boundaries_do_not_overflow_utc_conversion(self):
        earliest = datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))
        self.assert_delay(
            300.0, "Mon, 01 Jan 1900 00:00:00 GMT", now=earliest
        )
        latest = datetime(9999, 12, 31, 23, 59, 58, tzinfo=timezone.utc)
        self.assert_delay(
            3601.0, "Fri, 31 Dec 9999 23:59:59 -0100", now=latest, cap=5000
        )

    def test_inputs_are_unchanged(self):
        header = " \t00012\r\n"
        now = self.now.replace(microsecond=250000)
        self.assert_delay(12.0, header, now=now)
        self.assertEqual(header, " \t00012\r\n")
        self.assertEqual(now, self.now.replace(microsecond=250000))
        self.assertIs(now.tzinfo, timezone.utc)
        mutable_header = ["12"]
        self.assert_delay(1.0, mutable_header)
        self.assertEqual(mutable_header, ["12"])


if __name__ == "__main__":
    unittest.main()
