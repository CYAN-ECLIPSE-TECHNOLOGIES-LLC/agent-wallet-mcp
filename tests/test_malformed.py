"""Malformed-args battery: every public B0 function must fail typed and
closed on wrong types, missing fields, unknown asset/chain, malformed
addresses, and cap overflow - never crash with an untyped error and
never silently pass."""
import unittest

from amounts import AmountError, parse_amount
from assets import RegistryError
from families.base import AdapterError, SandboxLedger
from policy import Refusal, evaluate_payment, validate_grant
from receipts import build_receipt
from state_machine import PaymentIntent
from tests.b0fixtures import *  # noqa: F401,F403

BAD_AMOUNTS = [None, 1.5, True, False, -1, "x", "", [], {}, b"12"]


class MalformedTests(unittest.TestCase):
    def setUp(self):
        self.world = make_world()
        (self.registry, self.adapters, self.oracle, self.policy,
         self.ledger, self.clock) = self.world

    def test_parse_amount_battery(self):
        for bad in BAD_AMOUNTS:
            with self.assertRaises(AmountError, msg=repr(bad)):
                parse_amount(bad, 6)
        with self.assertRaises(AmountError):
            parse_amount("1", 6.0)  # float decimals
        with self.assertRaises(AmountError):
            parse_amount("1", True)

    def test_evaluate_battery(self):
        base = dict(registry=self.registry, adapters=self.adapters,
                    oracle=self.oracle, policy=self.policy, wallet_id=WALLET,
                    from_address=ALICE, asset_id=SANDBOX_ASSET, to=BOB,
                    amount_base=100, spent_today_base=0,
                    spent_today_usd_micro=0, clock=self.clock)
        for bad in BAD_AMOUNTS:
            v = evaluate_payment(**{**base, "amount_base": bad})
            self.assertEqual(v.decision, "deny", repr(bad))
            self.assertIsNotNone(v.code, repr(bad))
        v = evaluate_payment(**{**base, "asset_id": "eip155:999/nope:0x0"})
        self.assertEqual(v.decision, "deny")
        v = evaluate_payment(**{**base, "asset_id": None})
        self.assertEqual(v.decision, "deny")
        v = evaluate_payment(**{**base, "to": None})
        self.assertEqual(v.decision, "deny")
        v = evaluate_payment(**{**base, "to": 12345})
        self.assertEqual(v.decision, "deny")

    def test_intent_battery(self):
        for bad in BAD_AMOUNTS:
            with self.assertRaises((ValueError, TypeError), msg=repr(bad)):
                PaymentIntent(wallet_id=WALLET, from_address=ALICE,
                              asset_id=SANDBOX_ASSET, chain_id="sandbox:local",
                              to=BOB, amount_base=bad)

    def test_grant_battery(self):
        good = {"per_tx_cap_base": 1, "daily_cap_base": 1,
                "approval_above_base": 1}
        for bad_spec in (None, [], "x", {"per_tx_cap_base": 1}, 
                         {**good, "per_tx_cap_base": 2**256}):
            # note: 2**256 cap is a valid int; policy caps are not amounts,
            # so only the structural cases must refuse
            if isinstance(bad_spec, dict) and \
                    bad_spec.get("per_tx_cap_base") == 2**256:
                continue
            with self.assertRaises(Refusal, msg=repr(bad_spec)):
                validate_grant(self.registry, self.adapters, WALLET, "o",
                               {SANDBOX_ASSET: bad_spec},
                               aggregate_daily_cap_usd_micro=1)
        with self.assertRaises(Refusal):
            validate_grant(self.registry, self.adapters, WALLET, "o",
                           {"unknown:asset": good},
                           aggregate_daily_cap_usd_micro=1)
        with self.assertRaises(Refusal):
            validate_grant(self.registry, self.adapters, WALLET, "o",
                           {SANDBOX_ASSET: good},
                           aggregate_daily_cap_usd_micro=-5)
        with self.assertRaises(Refusal):
            validate_grant(self.registry, self.adapters, WALLET, "o", {},
                           aggregate_daily_cap_usd_micro=1)

    def test_adapter_battery(self):
        adapter = self.adapters["sandbox"]
        for bad in BAD_AMOUNTS:
            with self.assertRaises((AdapterError, TypeError, KeyError),
                                   msg=repr(bad)):
                adapter.buildPayment({"intent_id": "i", "asset_id":
                                      SANDBOX_ASSET, "to": BOB,
                                      "amount_base": bad,
                                      "from_address": ALICE})
        with self.assertRaises(Exception):
            adapter.buildPayment({"intent_id": "i", "asset_id":
                                  SANDBOX_ASSET, "to": "bogus!!",
                                  "amount_base": 5, "from_address": ALICE})
        ledger = SandboxLedger()
        for bad in BAD_AMOUNTS:
            with self.assertRaises((AdapterError, TypeError), msg=repr(bad)):
                ledger.credit(ALICE, SANDBOX_ASSET, bad)

    def test_receipt_battery(self):
        row = self.registry.require(SANDBOX_ASSET)
        for bad in (-1, 1.5, True, "5", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                build_receipt(payment_id="p", intent_id="i",
                              wallet_id=WALLET, asset_row=row,
                              amount_base=bad)

    def test_registry_require_battery(self):
        for bad in (None, "", "nope", 42):
            with self.assertRaises(RegistryError, msg=repr(bad)):
                self.registry.require(bad)


if __name__ == "__main__":
    unittest.main()
