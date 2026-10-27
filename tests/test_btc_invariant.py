"""Invariant R/P-1: BTC is reserve-only. No bitcoin family module exists,
no BTC buildPayment path exists, grant-time and pay-time are typed
ASSET_RESERVE_ONLY, and a reserve address can never be a recipient."""
import re
import unittest
from pathlib import Path

from policy import Refusal, RefusalCode, evaluate_payment, validate_grant
from tests.b0fixtures import *  # noqa: F401,F403

ROOT = Path(__file__).resolve().parent.parent


class BtcInvariantTests(unittest.TestCase):
    def test_no_bitcoin_family_module(self):
        fams = sorted(p.name for p in (ROOT / "families").glob("*.py"))
        self.assertEqual(fams, ["__init__.py", "base.py", "evm.py",
                                "sandbox.py"])

    def test_no_btc_pay_path_in_code(self):
        offenders = []
        for p in list(ROOT.glob("*.py")) + list((ROOT / "families").glob("*.py")):
            text = p.read_text()
            for m in re.finditer(r"class\s+(\w+)", text):
                if "bitcoin" in m.group(1).lower():
                    offenders.append((p.name, m.group(1)))
            if re.search(r"def\s+buildPayment", text) and \
                    "bip122" in text:
                offenders.append((p.name, "buildPayment+bip122"))
        self.assertEqual(offenders, [])

    def test_btc_payment_attempt_is_reserve_only(self):
        registry, adapters, oracle, policy, ledger, clock = make_world()
        v = evaluate_payment(
            registry=registry, adapters=adapters, oracle=oracle,
            policy=policy, wallet_id=WALLET, from_address=ALICE,
            asset_id=BTC_ASSET, to="sandbox:bob", amount_base=1000,
            spent_today_base=0, spent_today_usd_micro=0, clock=clock)
        self.assertEqual((v.decision, v.code),
                         ("deny", RefusalCode.ASSET_RESERVE_ONLY))

    def test_btc_grant_is_invalid(self):
        registry, adapters, *_ = make_world()
        with self.assertRaises(Refusal) as ctx:
            validate_grant(registry, adapters, WALLET, "o",
                           {BTC_ASSET: {"per_tx_cap_base": 1,
                                        "daily_cap_base": 1,
                                        "approval_above_base": 1}},
                           aggregate_daily_cap_usd_micro=1)
        self.assertEqual(ctx.exception.code, RefusalCode.ASSET_RESERVE_ONLY)

    def test_slate_tiers_cannot_be_spent(self):
        """NEXT / WATCH / gas-role assets all refuse at evaluate time with
        typed codes, never a silent pass."""
        registry, adapters, oracle, policy, ledger, clock = make_world()
        for asset in (USDC_BASE, USDC_ETH, "eip155:8453/slip44:60",
                      "eip155:1/erc20:UNPOPULATED",
                      "eip155:1/erc20:UNPOPULATED-DAI"):
            v = evaluate_payment(
                registry=registry, adapters=adapters, oracle=oracle,
                policy=policy, wallet_id=WALLET, from_address=ALICE,
                asset_id=asset, to=BOB, amount_base=100,
                spent_today_base=0, spent_today_usd_micro=0, clock=clock)
            self.assertEqual(v.decision, "deny", asset)
            self.assertIn(v.code, (RefusalCode.ASSET_NOT_ENABLED,
                                   RefusalCode.NO_POLICY), asset)


if __name__ == "__main__":
    unittest.main()
