import unittest
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from fractions import Fraction

from retry_after import retry_delay


class NoOffset(tzinfo):
    def utcoffset(self, dt):
        return None


class BadOffset(tzinfo):
    def utcoffset(self, dt):
        return 1


class FoldOffset(tzinfo):
    def utcoffset(self, dt):
        return timedelta(hours=-4 if dt.fold == 0 else -5)


class RetryDelayTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, microsecond=250000, tzinfo=timezone.utc)

    def assertDelay(self, expected, value, **kwargs):
        result = retry_delay(value, kwargs.pop("now", self.now), **kwargs)
        self.assertIs(type(result), float)
        self.assertEqual(result, expected)

    def test_missing_header_still_validates_now(self):
        with self.assertRaises(ValueError):
            retry_delay(None, None)

    def test_invalid_now_for_every_header_kind(self):
        invalid_values = [
            None, True, 0, "2026-01-01", date(2026, 1, 1),
            datetime(2026, 1, 1),
            datetime(2026, 1, 1, tzinfo=NoOffset()),
            datetime(2026, 1, 1, tzinfo=BadOffset()),
            datetime(2026, 1, 1, tzinfo=tzinfo()),
        ]
        for index, now in enumerate(invalid_values):
            for header in (None, "", "bad", "12", "01 Jan 2026 00:00:10 GMT"):
                with self.subTest(index=index, header=header):
                    with self.assertRaises(ValueError):
                        retry_delay(header, now, cap=0)

    def test_invalid_default_and_cap_for_every_header_kind(self):
        invalid_values = [
            True, False, None, "1", b"1", -1, -0.01,
            float("nan"), float("inf"), float("-inf"), 10**5000,
            Decimal("1"), Fraction(1, 2), 1 + 0j, object(),
        ]
        for name in ("default", "cap"):
            for index, value in enumerate(invalid_values):
                for header in (None, "", "bad", "12", "01 Jan 2026 00:00:10 GMT"):
                    with self.subTest(name=name, index=index, header=header):
                        kwargs = {"default": 2, "cap": 0}
                        kwargs[name] = value
                        with self.assertRaises(ValueError):
                            retry_delay(header, self.now, **kwargs)

    def test_fallback_for_missing_blank_and_non_string_headers(self):
        for header in (None, "", " \t\r\n", "\u2003", 12, 1.5, True, b"12", [], {}):
            with self.subTest(header=header):
                self.assertDelay(1.75, header, default=1.75, cap=10)
                self.assertDelay(2.5, header, default=8, cap=2.5)
                self.assertDelay(0.0, header, default=0, cap=10)

    def test_malformed_numeric_forms_use_fallback(self):
        for header in (
            "+12", "-12", "-0", "12.0", ".5", "1e2", "1E+2", "1_000", "0x10",
            "１２", "١٢", "१२", "¹²", "1２", "12 seconds", "1 2", "1\n2",
            "NaN", "inf", "-Infinity", "garbage",
        ):
            with self.subTest(header=header):
                self.assertDelay(2.5, header, default=8, cap=2.5)
                self.assertDelay(1.75, header, default=1.75, cap=10)

    def test_ascii_digits_and_whitespace(self):
        for header, expected in (
            ("0", 0.0), ("00012", 12.0), ("  \t12\r\n", 12.0),
            ("\u200312\u2003", 12.0), ("300", 300.0), ("301", 300.0),
        ):
            with self.subTest(header=header):
                self.assertDelay(expected, header)
        self.assertDelay(0.5, "1", cap=0.5)

    def test_long_digit_headers(self):
        for label, header, cap, expected in (
            ("overflow", "9" * 5000, 300, 300.0),
            ("large cap", "9" * 5000, 1e308, 1e308),
            ("leading zeroes", "0" * 4999 + "7", 300, 7.0),
            ("all zeroes", "0" * 5000, 300, 0.0),
            ("long finite", "0" * 4996 + "1234", 2000, 1234.0),
        ):
            with self.subTest(label=label):
                self.assertDelay(expected, header, cap=cap)

    def test_zero_cap_for_all_header_forms(self):
        for header in (None, "", "bad", "0", "12", "9" * 5000,
                       "01 Jan 2026 00:00:10 GMT", "01 Jan 2026 00:00:00 GMT"):
            with self.subTest(header=header[:40] if isinstance(header, str) else header):
                self.assertDelay(0.0, header, default=10, cap=0)

    def test_date_fractional_seconds(self):
        self.assertDelay(9.75, "Thu, 01 Jan 2026 00:00:10 GMT")

    def test_aware_dates_and_supported_formats(self):
        for header in (
            "Thu, 01 Jan 2026 02:00:10 +0200",
            "Wed, 31 Dec 2025 19:00:10 -0500",
            "Thursday, 01-Jan-26 00:00:10 GMT",
            "01 Jan 2026 00:00:10 +0000",
            " \tThu, 01 Jan 2026 00:00:10 UTC\r\n",
        ):
            with self.subTest(header=header):
                self.assertDelay(9.75, header)
        local_now = self.now.astimezone(timezone(timedelta(hours=5, minutes=30)))
        self.assertDelay(9.75, "01 Jan 2026 00:00:10 GMT", now=local_now)
        fractional_offset_now = datetime(
            2026, 1, 1, 5, 30, 0, 250000,
            tzinfo=timezone(timedelta(hours=5, minutes=30, microseconds=125000)),
        )
        self.assertDelay(9.875, "01 Jan 2026 00:00:10 GMT", now=fractional_offset_now)

    def test_past_equal_and_capped_dates(self):
        self.assertDelay(0.0, "01 Jan 2026 00:00:00 GMT")
        self.assertDelay(0.0, "31 Dec 2025 23:59:59 GMT")
        self.assertDelay(0.0, "01 Jan 2026 00:00:00 GMT", now=self.now.replace(microsecond=0))
        self.assertDelay(0.5, "01 Jan 2026 00:00:10 GMT", cap=0.5)
        self.assertDelay(300.0, "01 Jan 2027 00:00:00 GMT")

    def test_invalid_and_naive_dates_use_fallback(self):
        for header in (
            "Thu, 32 Jan 2026 00:00:10 GMT",
            "Thu, 01 Jan 2026 25:00:10 GMT",
            "Thu, 01 Jan 2026 00:00:60 GMT",
            "Thu, 01 Jan 2026 00:00:10 +2400",
            "Thu, 01 Jan 2026 00:00:10 -2400",
            "Thu, 01 Jan 10000 00:00:10 GMT",
            "Thu, 01 Jan 2026 00:00:10",
            "Thu, 01 Jan 2026 00:00:10 -0000",
            "Thu Jan  1 00:00:10 2026",
            "Thu, 01 Jan 2026 00:00:10 UNKNOWN",
            "01 Jan " + "9" * 5000 + " 00:00:10 GMT",
        ):
            with self.subTest(header=header[:70]):
                self.assertDelay(2.5, header, default=8, cap=2.5)

    def test_fold_uses_absolute_instant(self):
        for fold, expected in ((0, 3609.75), (1, 9.75)):
            now = datetime(2026, 11, 1, 1, 30, 0, 250000, tzinfo=FoldOffset(), fold=fold)
            with self.subTest(fold=fold):
                self.assertDelay(expected, "01 Nov 2026 06:30:10 GMT", now=now, cap=4000)

    def test_datetime_limits_do_not_require_utc_normalization(self):
        near_max = datetime(9999, 12, 31, 23, 59, 58, 999999,
                            tzinfo=timezone(timedelta(hours=-1)))
        self.assertDelay(0.000001, "31 Dec 9999 23:59:59 -0100", now=near_max)
        earliest = datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))
        latest = datetime.max.replace(tzinfo=timezone(timedelta(hours=-1)))
        self.assertDelay(300.0, "01 Jan 0100 00:00:00 GMT", now=earliest)
        self.assertDelay(0.0, "01 Jan 1970 00:00:00 GMT", now=latest)

    def test_inputs_are_not_mutated(self):
        header = " \t00012\n"
        default, cap = 3, 20
        before = (header, self.now.isoformat(), self.now.fold, default, cap)
        self.assertDelay(12.0, header, default=default, cap=cap)
        self.assertEqual(before, (header, self.now.isoformat(), self.now.fold, default, cap))
        mutable_header = ["12"]
        self.assertDelay(1.0, mutable_header)
        self.assertEqual(mutable_header, ["12"])


if __name__ == "__main__":
    unittest.main()
