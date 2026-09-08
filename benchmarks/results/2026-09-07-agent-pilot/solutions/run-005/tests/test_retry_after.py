import sys
import unittest
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from fractions import Fraction

from retry_after import retry_delay


DATE = "Thu, 01 Jan 2026 00:00:05 GMT"


class NoOffset(tzinfo):
    def utcoffset(self, dt):
        return None


class InvalidOffset(tzinfo):
    def utcoffset(self, dt):
        return timedelta(days=1)


class WrongTypeOffset(tzinfo):
    def utcoffset(self, dt):
        return 0


class FailingOffset(tzinfo):
    def utcoffset(self, dt):
        raise RuntimeError("offset unavailable")


class FailingFloat(float):
    def __float__(self):
        raise RuntimeError("conversion unavailable")


class FoldOffset(tzinfo):
    def utcoffset(self, dt):
        return timedelta(hours=-5 if dt.fold else -4)


class RetryDelayTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, microsecond=250000, tzinfo=timezone.utc)

    def assertFloatEqual(self, actual, expected):
        self.assertIs(type(actual), float)
        self.assertEqual(actual, expected)

    def test_ascii_delays_and_whitespace(self):
        for header, expected in (
            ("0", 0.0),
            ("00000", 0.0),
            ("12", 12.0),
            ("00012", 12.0),
            (" \t12\r\n", 12.0),
            ("\u200312\u2003", 12.0),
            ("21", 20.0),
        ):
            with self.subTest(header=header):
                self.assertFloatEqual(
                    retry_delay(header, self.now, default=3, cap=20), expected
                )
        self.assertFloatEqual(retry_delay("12", self.now, cap=2.5), 2.5)

    def test_very_long_digits_saturate_without_integer_conversion(self):
        digit_limit = sys.get_int_max_str_digits()
        for label, header, expected in (
            ("large", "9" * 5000, 20.0),
            ("leading zeroes", "0" * 4999 + "7", 7.0),
            ("zero", "0" * 5000, 0.0),
        ):
            with self.subTest(label=label):
                self.assertFloatEqual(retry_delay(header, self.now, cap=20), expected)
        self.assertFloatEqual(
            retry_delay("9" * 5000, self.now, cap=sys.float_info.max),
            sys.float_info.max,
        )
        self.assertEqual(sys.get_int_max_str_digits(), digit_limit)

    def test_missing_blank_and_nonstring_headers_use_capped_fallback(self):
        for header in (None, "", " \t\r\n", "\u2003", 12, 1.5, True, b"12", [], {}):
            for default, cap, expected in ((7, 3, 3.0), (2, 3, 2.0), (0, 3, 0.0)):
                with self.subTest(header=header, default=default, cap=cap):
                    self.assertFloatEqual(
                        retry_delay(header, self.now, default=default, cap=cap),
                        expected,
                    )

    def test_malformed_numeric_forms_use_fallback(self):
        for header in (
            "+12", "-12", "-0", "1.5", ".5", "1e2", "1E+2", "NaN", "Infinity",
            "inf", "1_000", "0x10", "1,000", "1 2", "12\n34", "12 seconds",
            "\u0661\u0662", "\uff11\uff12", "\u00b2", "1\u0662", "12\x00",
        ):
            with self.subTest(header=header):
                self.assertFloatEqual(
                    retry_delay(header, self.now, default=7, cap=3), 3.0
                )

    def test_aware_dates_preserve_fractional_seconds(self):
        for header in (
            DATE,
            " \t" + DATE + "\r\n",
            "Thursday, 01-Jan-26 00:00:05 GMT",
            "01 Jan 2026 00:00:05 +0000",
            "Thu, 01 Jan 2026 00:00:05 UTC",
            "Thu, 01 Jan 2026 02:00:05 +0200",
            "Wed, 31 Dec 2025 17:00:05 -0700",
        ):
            with self.subTest(header=header):
                self.assertFloatEqual(retry_delay(header, self.now), 4.75)

    def test_now_offsets_are_compared_as_absolute_instants(self):
        for now in (
            datetime(2026, 1, 1, 5, 30, 0, 250000,
                     tzinfo=timezone(timedelta(hours=5, minutes=30))),
            datetime(2025, 12, 31, 17, 0, 0, 250000,
                     tzinfo=timezone(timedelta(hours=-7))),
            datetime(2026, 1, 1, 1, 0, 0, 750000,
                     tzinfo=timezone(timedelta(hours=1, microseconds=500000))),
        ):
            with self.subTest(now=now):
                self.assertFloatEqual(retry_delay(DATE, now), 4.75)

    def test_now_fold_is_respected(self):
        for fold, expected in ((0, 3604.75), (1, 4.75)):
            now = datetime(2026, 11, 1, 1, 30, 0, 250000,
                           tzinfo=FoldOffset(), fold=fold)
            original = (now.isoformat(), now.tzinfo, now.fold)
            with self.subTest(fold=fold):
                self.assertFloatEqual(
                    retry_delay("Sun, 01 Nov 2026 06:30:05 GMT", now, cap=10000),
                    expected,
                )
                self.assertEqual((now.isoformat(), now.tzinfo, now.fold), original)

    def test_past_equal_and_capped_future_dates(self):
        self.assertFloatEqual(
            retry_delay("Thu, 01 Jan 2026 00:00:00 GMT", self.now), 0.0
        )
        self.assertFloatEqual(
            retry_delay(DATE, datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc)),
            0.0,
        )
        self.assertFloatEqual(retry_delay(DATE, self.now, cap=2.5), 2.5)
        self.assertFloatEqual(
            retry_delay("Fri, 31 Dec 9999 23:59:59 -1400", self.now), 300.0
        )

    def test_datetime_bounds_do_not_overflow_when_offsets_cross_utc_bounds(self):
        epoch_header = "Thu, 01 Jan 1970 00:00:00 GMT"
        earliest = datetime.min.replace(tzinfo=timezone(timedelta(hours=14)))
        expected = (
            datetime(1970, 1, 1) - datetime.min + timedelta(hours=14)
        ).total_seconds()
        self.assertFloatEqual(
            retry_delay(epoch_header, earliest, cap=1e20), expected
        )
        latest = datetime.max.replace(tzinfo=timezone(timedelta(hours=-14)))
        self.assertFloatEqual(retry_delay(epoch_header, latest), 0.0)

    def test_naive_and_invalid_dates_use_fallback(self):
        for header in (
            "Thu, 01 Jan 2026 00:00:05",
            "Thu, 01 Jan 2026 00:00:05 -0000",
            "Thu Jan  1 00:00:05 2026",
            "Thu, 01 Jan 2026 00:00:05 UNKNOWN",
            "not a date",
            "Thu, 32 Jan 2026 00:00:05 GMT",
            "Thu, 30 Feb 2026 00:00:05 GMT",
            "Thu, 01 Jan 2026 24:00:05 GMT",
            "Thu, 01 Jan 2026 00:00:60 GMT",
            "Thu, 01 Jan 2026 00:00:05 +2500",
            "Thu, 01 Jan 999999999999 00:00:05 GMT",
            "Sunday, -Nov-94 08:49:37 GMT",
        ):
            with self.subTest(header=header):
                self.assertFloatEqual(
                    retry_delay(header, self.now, default=7, cap=3), 3.0
                )

    def test_invalid_now_is_rejected_on_every_path(self):
        for now in (
            None, True, 1, "2026-01-01", date(2026, 1, 1), datetime(2026, 1, 1),
            datetime(2026, 1, 1, tzinfo=NoOffset()),
            datetime(2026, 1, 1, tzinfo=InvalidOffset()),
            datetime(2026, 1, 1, tzinfo=WrongTypeOffset()),
            datetime(2026, 1, 1, tzinfo=tzinfo()),
            datetime(2026, 1, 1, tzinfo=FailingOffset()),
        ):
            for header in (None, "", "0", DATE, "malformed"):
                for cap in (0, 20):
                    with self.subTest(now=now, header=header, cap=cap):
                        with self.assertRaises(ValueError):
                            retry_delay(header, now, cap=cap)

    def test_invalid_default_and_cap_are_rejected_on_every_path(self):
        for name in ("default", "cap"):
            for invalid in (
                True, False, None, "1", b"1", [], {}, 1 + 0j,
                Decimal("1"), Fraction(1, 1), FailingFloat(1.0), -1, -0.1,
                float("nan"), float("inf"), float("-inf"), 10**400, -(10**400),
            ):
                for header in (None, "", "0", DATE, "malformed"):
                    # A zero cap/default must not bypass validation either.
                    options = {"default": 0, "cap": 0, name: invalid}
                    with self.subTest(name=name, invalid=invalid, header=header):
                        with self.assertRaises(ValueError):
                            retry_delay(header, self.now, **options)

    def test_valid_numeric_argument_bounds_and_zero_cap(self):
        for number in (0, -0.0, 2, 2.5, 5e-324, 10**308, sys.float_info.max):
            with self.subTest(number=number):
                self.assertFloatEqual(
                    retry_delay(None, self.now, default=number, cap=number),
                    float(number),
                )
        for header in (None, "", "malformed", "12", "9" * 5000, DATE):
            with self.subTest(header=header[:20] if isinstance(header, str) else header):
                self.assertFloatEqual(retry_delay(header, self.now, cap=0), 0.0)

    def test_nonstring_input_is_not_mutated(self):
        header = ["12"]
        self.assertFloatEqual(retry_delay(header, self.now, default=7, cap=3), 3.0)
        self.assertEqual(header, ["12"])


if __name__ == "__main__":
    unittest.main()
