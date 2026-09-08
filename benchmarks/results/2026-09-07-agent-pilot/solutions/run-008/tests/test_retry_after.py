import sys
import unittest
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from fractions import Fraction

from retry_after import retry_delay


class StubOffset(tzinfo):
    def __init__(self, offset):
        self.offset = offset

    def utcoffset(self, dt):
        return self.offset


class RetryDelayTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.date_header = "Thu, 01 Jan 2026 00:00:02 GMT"

    def assert_delay(self, expected, value, now=None, **kwargs):
        result = retry_delay(value, self.now if now is None else now, **kwargs)
        self.assertIs(type(result), float)
        self.assertEqual(result, expected)

    def test_missing_blank_and_non_string_headers(self):
        headers = [None, "", " \t\r\n", 12, 2.5, True, b"12", [], {}, object()]
        for header in headers:
            for default, cap, expected in [(7, 20, 7.0), (7, 3, 3.0)]:
                with self.subTest(header=header, default=default, cap=cap):
                    self.assert_delay(expected, header, default=default, cap=cap)

    def test_ascii_digits_and_surrounding_whitespace(self):
        for header, expected in [
            ("0", 0.0),
            ("00000", 0.0),
            ("00012", 12.0),
            ("\t 12\r\n", 12.0),
            ("\u200312\u2003", 12.0),
            ("300", 300.0),
            ("301", 300.0),
        ]:
            with self.subTest(header=header):
                self.assert_delay(expected, header)
        self.assert_delay(2.5, "3", cap=2.5)
        self.assert_delay(2.0, "2", cap=2.5)

    def test_huge_digit_headers_saturate_without_integer_conversion(self):
        for cap in [0, 0.25, 300, sys.float_info.max]:
            with self.subTest(cap=cap):
                self.assert_delay(float(cap), "9" * 5000, cap=cap)
        self.assert_delay(12.0, "0" * 5000 + "12")
        self.assert_delay(0.0, "0" * 5000)

    def test_non_delay_numeric_forms_are_malformed(self):
        for header in [
            "+3", "-3", "-0", "3.0", ".3", "3.", "1e2", "0x10",
            "1_000", "1,000", "1 2", "1\n2", "NaN", "inf", "Infinity",
            "٣", "１２", "²", "1٢", "3\x00", "garbage",
        ]:
            with self.subTest(header=header):
                self.assert_delay(7.0, header, default=7, cap=19)
                self.assert_delay(2.0, header, default=7, cap=2)

    def test_aware_date_formats(self):
        for header in [
            self.date_header,
            "Thursday, 01-Jan-26 00:00:02 GMT",
            "1 Jan 2026 00:00:02 +0000",
            "Thu, 01 Jan 2026 02:00:02 +0200",
            "Wed, 31 Dec 2025 19:00:02 -0500",
            "Wed, 31 Dec 2025 19:00:02 EST",
            "\t " + self.date_header + " \r\n",
        ]:
            with self.subTest(header=header):
                self.assert_delay(2.0, header, default=7)

    def test_dates_use_absolute_instants_and_fractional_seconds(self):
        for offset in [timedelta(hours=2), timedelta(hours=-7)]:
            now = (self.now + timedelta(microseconds=250000)).astimezone(
                timezone(offset)
            )
            with self.subTest(offset=offset):
                self.assert_delay(1.75, self.date_header, now=now)
        now = datetime(
            2026, 1, 1, 2, 0, 0,
            tzinfo=timezone(timedelta(hours=2, microseconds=250000)),
        )
        self.assert_delay(2.25, self.date_header, now=now)

    def test_past_equal_and_capped_dates(self):
        self.assert_delay(0.0, "Wed, 31 Dec 2025 23:59:59 GMT")
        self.assert_delay(0.0, "Thu, 01 Jan 2026 00:00:00 GMT")
        self.assert_delay(0.5, self.date_header, cap=0.5)
        self.assert_delay(300.0, "Fri, 01 Jan 2027 00:00:00 GMT")
        self.assert_delay(
            0.0, "Thu, 01 Jan 2026 00:00:00 GMT",
            now=self.now + timedelta(microseconds=1),
        )
        self.assert_delay(
            0.000001, self.date_header,
            now=self.now + timedelta(seconds=2, microseconds=-1),
        )

    def test_invalid_and_naive_dates_use_capped_fallback(self):
        for header in [
            "Thu, 01 Jan 2026 00:00:02",
            "Thu, 01 Jan 2026 00:00:02 -0000",
            "Thu, 01 Jan 2026 00:00:02 UNKNOWN",
            "Thu Jan  1 00:00:02 2026",
            "Thu, 32 Jan 2026 00:00:02 GMT",
            "Thu, 01 Jan 2026 25:00:02 GMT",
            "Thu, 01 Jan 2026 00:00:02 +2500",
            "Thu, 01 Jan 10000 00:00:02 GMT",
            "Thu,",
        ]:
            with self.subTest(header=header):
                self.assert_delay(7.0, header, default=7, cap=19)
                self.assert_delay(2.0, header, default=7, cap=2)

    def test_zero_cap_on_every_header_path(self):
        for header in [None, "", "garbage", "12", self.date_header]:
            with self.subTest(header=header):
                self.assert_delay(0.0, header, default=7, cap=0)

    def test_now_must_be_an_aware_datetime_on_every_call(self):
        invalid_now = [
            None, "2026-01-01", 0, date(2026, 1, 1),
            datetime(2026, 1, 1),
            datetime(2026, 1, 1, tzinfo=StubOffset(None)),
            datetime(2026, 1, 1, tzinfo=StubOffset(0)),
            datetime(2026, 1, 1, tzinfo=StubOffset(timedelta(days=1))),
            datetime(2026, 1, 1, tzinfo=tzinfo()),
        ]
        for now in invalid_now:
            for header in [None, "", "garbage", "12", self.date_header]:
                for cap in [0, 300]:
                    with self.subTest(now=now, header=header, cap=cap):
                        with self.assertRaises(ValueError):
                            retry_delay(header, now, cap=cap)

    def test_default_and_cap_are_validated_on_every_call(self):
        invalid_numbers = [
            ("true", True), ("false", False), ("none", None),
            ("string", "1"), ("bytes", b"1"), ("complex", 1 + 0j),
            ("decimal", Decimal("1")), ("fraction", Fraction(1, 1)),
            ("negative int", -1), ("negative float", -0.25),
            ("nan", float("nan")), ("infinity", float("inf")),
            ("negative infinity", -float("inf")),
            ("huge int", 10 ** 5000), ("huge negative int", -(10 ** 5000)),
        ]
        for parameter in ["default", "cap"]:
            for label, number in invalid_numbers:
                for header in [None, "", "garbage", "12", self.date_header]:
                    with self.subTest(parameter=parameter, kind=label, header=header):
                        with self.assertRaises(ValueError):
                            retry_delay(header, self.now, **{parameter: number})
                if parameter == "default":
                    with self.subTest(kind=label, cap=0):
                        with self.assertRaises(ValueError):
                            retry_delay(None, self.now, default=number, cap=0)

    def test_valid_numeric_arguments_are_converted_to_float(self):
        for number in [0, -0.0, 1, 0.125, 10 ** 308, sys.float_info.max]:
            with self.subTest(number=number):
                self.assert_delay(float(number), None, default=number, cap=number)
                self.assert_delay(float(number), "9" * 5000, cap=number)

    def test_datetime_boundaries_do_not_require_representable_utc_datetime(self):
        earliest = datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))
        self.assert_delay(300.0, self.date_header, now=earliest, default=7)
        latest = datetime.max.replace(tzinfo=timezone(timedelta(hours=-1)))
        self.assert_delay(
            0.0, "Fri, 31 Dec 9999 23:59:59 GMT", now=latest, default=7,
        )
        now = datetime(9999, 12, 31, 23, 59, 58, 750000, tzinfo=timezone.utc)
        self.assert_delay(
            3600.25, "Fri, 31 Dec 9999 23:59:59 -0100", now=now, cap=5000,
        )

    def test_inputs_are_unchanged(self):
        header = " 00012 "
        now = self.now.replace(fold=1)
        before = (header, now.isoformat(), now.fold, now.tzinfo)
        self.assert_delay(12.0, header, now=now)
        self.assertEqual((header, now.isoformat(), now.fold, now.tzinfo), before)
        for mutable_header in [["12"], {"Retry-After": "12"}, bytearray(b"12")]:
            before = mutable_header.copy()
            self.assert_delay(1.0, mutable_header)
            self.assertEqual(mutable_header, before)


if __name__ == "__main__":
    unittest.main()
