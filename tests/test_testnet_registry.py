import json
import unittest
from pathlib import Path

from assets import AssetRegistry, RegistryError, validate_registry
from testnet_config import (ETH_TESTNET_ASSET_ID, TESTNET_CHAIN_ID,
                            USDC_TESTNET_ASSET_ID, load_testnet_registry)
from tests.b0fixtures import *  # noqa: F401,F403

EXPECTED_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


class TestnetRegistryTests(unittest.TestCase):
    def test_usdc_row_dual_sourced(self):
        row = AssetRegistry().require(USDC_TESTNET_ASSET_ID)
        self.assertEqual(row["contract_or_mint"], EXPECTED_USDC)
        self.assertEqual(row["chain_id"], "eip155:84532")
        self.assertEqual(row["decimals"], 6)
        self.assertEqual(row["network"], "testnet")
        # dual-source rule recorded in the row itself
        self.assertIn("Circle", row["address_source"])
        self.assertIn("BaseScan Sepolia", row["address_source"])
        self.assertIn(EXPECTED_USDC, row["address_source"])

    def test_eth_gas_row(self):
        row = AssetRegistry().require(ETH_TESTNET_ASSET_ID)
        self.assertEqual(row["role"], "gas")
        self.assertIsNone(row["contract_or_mint"])
        self.assertEqual(row["decimals"], 18)
        usdc = AssetRegistry().require(USDC_TESTNET_ASSET_ID)
        self.assertEqual(usdc["fee_asset_id"], ETH_TESTNET_ASSET_ID)

    def test_shipped_defaults_not_spend_enabled(self):
        reg = AssetRegistry()
        for aid in (USDC_TESTNET_ASSET_ID, ETH_TESTNET_ASSET_ID):
            row = reg.require(aid)
            self.assertTrue(row["listed"])
            self.assertFalse(row["live"])
            self.assertFalse(row["used"])
            self.assertFalse(row["spend_enabled"], aid)
        shipped = [r["asset_id"] for r in reg.spend_enabled_rows()]
        self.assertEqual(shipped, [SANDBOX_ASSET])

    def test_testnet_config_flips_only_in_memory(self):
        reg = load_testnet_registry()
        self.assertTrue(reg.require(USDC_TESTNET_ASSET_ID)["spend_enabled"])
        # gas row is not a payment row: policy grants name USDC; the ETH
        # row stays spend_enabled in config so balance/fee reads resolve,
        # but the shipped file on disk is unchanged.
        on_disk = json.loads(Path(__file__).resolve().parent.parent.joinpath(
            "assets.json").read_text())
        for row in on_disk["assets"]:
            if row["chain_id"] == TESTNET_CHAIN_ID:
                self.assertFalse(row["spend_enabled"], row["asset_id"])

    def test_mainnet_spend_still_refused_by_validator(self):
        data = json.loads(Path(__file__).resolve().parent.parent.joinpath(
            "assets.json").read_text())
        for row in data["assets"]:
            if row["asset_id"].startswith("eip155:8453/"):
                row["spend_enabled"] = True
        with self.assertRaises(RegistryError):
            validate_registry(data)
        # even an explicit testnet opt-in cannot enable a mainnet row
        with self.assertRaises(RegistryError):
            validate_registry(data, testnet_spend_chains={"eip155:84532"})


if __name__ == "__main__":
    unittest.main()
