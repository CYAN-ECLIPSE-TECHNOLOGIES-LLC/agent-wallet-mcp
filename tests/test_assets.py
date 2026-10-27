import copy
import json
import unittest
from pathlib import Path

from assets import (AssetRegistry, RegistryError, validate_registry)
from tests.b0fixtures import *  # noqa: F401,F403

RAW = json.loads((Path(__file__).resolve().parent.parent /
                  "assets.json").read_text())


def mutated(fn):
    data = copy.deepcopy(RAW)
    fn(data)
    return data


class RegistryTests(unittest.TestCase):
    def test_shipped_registry_validates(self):
        reg = AssetRegistry()
        self.assertEqual(len(reg.rows), 14)
        self.assertEqual([r["asset_id"] for r in reg.spend_enabled_rows()],
                         [SANDBOX_ASSET])

    def test_dual_sourced_addresses_exact(self):
        reg = AssetRegistry()
        self.assertEqual(reg.require(USDC_BASE)["contract_or_mint"],
                         "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        self.assertEqual(reg.require(USDC_ETH)["contract_or_mint"],
                         "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
        sol = reg.by_symbol_chain(
            "USDC", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")
        self.assertEqual(sol["contract_or_mint"],
                         "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
        eurc = reg.by_symbol_chain("EURC", "eip155:8453")
        self.assertEqual(eurc["contract_or_mint"],
                         "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42")

    def test_unpopulated_rows_have_no_invented_address(self):
        reg = AssetRegistry()
        for symbol in ("PYUSD", "USDT", "DAI", "USDS"):
            row = reg.by_symbol_chain(symbol, "eip155:1")
            self.assertIsNone(row["contract_or_mint"], symbol)
            self.assertTrue(row["population_required"], symbol)
            self.assertFalse(row["spend_enabled"], symbol)

    def test_tier_and_status_are_separate_fields(self):
        reg = AssetRegistry()
        row = reg.require(USDC_BASE)
        self.assertEqual(row["slate_tier"], "CORE")
        self.assertEqual(row["implementation_status"], "registry-only")
        btc = reg.require(BTC_ASSET)
        self.assertEqual(btc["role"], "reserve")
        self.assertFalse(btc["spend_enabled"])
        # three-field lifecycle: listed != live != used
        self.assertTrue(row["listed"] and not row["live"] and not row["used"])

    def test_duplicate_pair_fails(self):
        def dup(d):
            d["assets"].append(copy.deepcopy(d["assets"][1]))
        with self.assertRaises(RegistryError):
            validate_registry(mutated(dup))

    def test_unknown_family_fails(self):
        def bad(d):
            d["assets"][1]["family"] = "dogechain"
        with self.assertRaises(RegistryError):
            validate_registry(mutated(bad))

    def test_decimals_mismatch_fails(self):
        def bad(d):
            d["assets"][1]["decimals"] = 8
        with self.assertRaises(RegistryError):
            validate_registry(mutated(bad))

    def test_bad_address_format_fails(self):
        def bad(d):
            d["assets"][1]["contract_or_mint"] = "0x123"
        with self.assertRaises(RegistryError):
            validate_registry(mutated(bad))

    def test_non_sandbox_spend_enabled_fails(self):
        def bad(d):
            d["assets"][1]["spend_enabled"] = True
        with self.assertRaises(RegistryError):
            validate_registry(mutated(bad))

    def test_reserve_spend_enabled_fails(self):
        def bad(d):
            for r in d["assets"]:
                if r["symbol"] == "BTC":
                    r["family"] = "sandbox"  # isolate the reserve check
                    r["spend_enabled"] = True
        with self.assertRaises(RegistryError):
            validate_registry(mutated(bad))

    def test_missing_field_fails(self):
        def bad(d):
            del d["assets"][0]["live_gate"]
        with self.assertRaises(RegistryError):
            validate_registry(mutated(bad))


if __name__ == "__main__":
    unittest.main()
