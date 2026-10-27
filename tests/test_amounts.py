import random
import unittest
from decimal import Decimal

from amounts import (AmountError, format_amount, parse_amount,
                     parse_base_units, usd_value_micro)
from tests.b0fixtures import *  # noqa: F401,F403 - path bootstrap


class ParseFormatTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_amount("12.50", 6), 12_500_000)
        self.assertEqual(parse_amount("0.000001", 6), 1)
        self.assertEqual(parse_amount(3, 6), 3_000_000)
        self.assertEqual(parse_amount(Decimal("1.25"), 6), 1_250_000)
        self.assertEqual(parse_amount("7", 0), 7)

    def test_wei_scale(self):
        self.assertEqual(parse_amount("1.000000000000000001", 18),
                         10**18 + 1)
        big = "123456789.123456789123456789"
        self.assertEqual(parse_amount(big, 18),
                         123456789123456789123456789)

    def test_rejects(self):
        for bad in (1.5, 0.0, True, False, None, -1, "-1", "1e3", "1,000",
                    "", "abc", "1.", ".5", [1], {"a": 1}):
            with self.assertRaises(AmountError, msg=repr(bad)):
                parse_amount(bad, 6)

    def test_over_precision_rejected_not_rounded(self):
        with self.assertRaises(AmountError) as ctx:
            parse_amount("0.0000001", 6)
        self.assertEqual(ctx.exception.code, "AMOUNT_OVER_PRECISION")
        with self.assertRaises(AmountError):
            parse_amount(Decimal("0.0000001"), 6)

    def test_overflow(self):
        with self.assertRaises(AmountError) as ctx:
            parse_amount(str(2**256), 0)
        self.assertEqual(ctx.exception.code, "AMOUNT_OVERFLOW")

    def test_format(self):
        self.assertEqual(format_amount(12_500_000, 6), "12.5")
        self.assertEqual(format_amount(1, 6), "0.000001")
        self.assertEqual(format_amount(0, 18), "0")
        self.assertEqual(format_amount(10**18 + 1, 18),
                         "1.000000000000000001")
        for bad in (1.5, True, -1, "12"):
            with self.assertRaises(AmountError, msg=repr(bad)):
                format_amount(bad, 6)

    def test_base_units(self):
        self.assertEqual(parse_base_units(42), 42)
        self.assertEqual(parse_base_units("42"), 42)
        for bad in (1.5, True, -3, "4.2", None):
            with self.assertRaises(AmountError, msg=repr(bad)):
                parse_base_units(bad)

    def test_usd_value(self):
        self.assertEqual(usd_value_micro(2_000_000, 6, "1.0"), 2_000_000)
        self.assertEqual(usd_value_micro(1_500_000, 6, Decimal("0.5")),
                         750_000)
        with self.assertRaises(AmountError):
            usd_value_micro(1, 6, 1.0)  # float price rejected

    def test_seeded_fuzz_roundtrip(self):
        rng = random.Random(20261027)
        for decimals in (0, 6, 8, 9, 18):
            for _ in range(300):
                whole = rng.randrange(0, 10**rng.randrange(1, 12))
                frac = rng.randrange(0, 10**decimals) if decimals else 0
                base = whole * 10**decimals + frac
                text = format_amount(base, decimals)
                self.assertEqual(parse_amount(text, decimals), base,
                                 f"roundtrip failed: {base} dec={decimals}")
                self.assertEqual(parse_amount(Decimal(text), decimals), base)


if __name__ == "__main__":
    unittest.main()
