"""F-2 (B1 testnet bench finding): the policy layer must preserve the
oracle failure CAUSE — ORACLE_DIVERGENCE vs PRICE_STALE vs
ORACLE_UNAVAILABLE — on the verdict and through to the receipt,
instead of collapsing all three into price_unverified with code=None.
"""
import unittest

from oracle import StaticOracle, TwoSourceOracle
from policy import evaluate_payment
from state_machine import Dispatcher, PaymentIntent
from tests.b0fixtures import *  # noqa: F401,F403


def _intent(amount=1_000_000):
    return PaymentIntent(wallet_id=WALLET, from_address=ALICE,
                         asset_id=SANDBOX_ASSET, chain_id="sandbox:local",
                         to=BOB, amount_base=amount, desk_auth_ok=True)


def _dispatch(world, oracle):
    registry, adapters, _o, policy, ledger, clock = world
    d = Dispatcher(registry=registry, adapters=adapters, oracle=oracle,
                   policy=policy, clock=clock)
    return d, d.dispatch(_intent())


class OracleCauseReceiptTests(unittest.TestCase):
    def test_healthy_price_has_no_failure_code(self):
        world = make_world()
        _d, res = _dispatch(world, world[2])
        self.assertEqual(res.status, "executed")
        self.assertFalse(res.receipt["price_unverified"])
        self.assertIsNone(res.receipt["oracle_failure_code"])

    def test_divergence_survives_to_receipt(self):
        world = make_world()
        clock = world[5]
        a = StaticOracle({}, clock=clock)
        a.set(SANDBOX_ASSET, "USD", "1.0000", source="src-a")
        b = StaticOracle({}, clock=clock)
        b.set(SANDBOX_ASSET, "USD", "1.0100", source="src-b")  # ~100 bps
        two = TwoSourceOracle(a, b, clock=clock)
        _d, res = _dispatch(world, two)
        self.assertEqual(res.status, "executed")  # pegged degrade, not deny
        self.assertTrue(res.receipt["price_unverified"])
        self.assertEqual(res.receipt["oracle_failure_code"],
                         "ORACLE_DIVERGENCE")

    def test_stale_survives_to_receipt(self):
        world = make_world()
        clock = world[5]
        stale = StaticOracle({}, clock=clock)
        stale.set(SANDBOX_ASSET, "USD", "1.0",
                  observed_at=clock() - 10_000, source="old")
        _d, res = _dispatch(world, stale)
        self.assertEqual(res.status, "executed")
        self.assertTrue(res.receipt["price_unverified"])
        self.assertEqual(res.receipt["oracle_failure_code"], "PRICE_STALE")

    def test_unavailable_survives_to_receipt(self):
        world = make_world()
        empty = StaticOracle({}, clock=world[5])
        _d, res = _dispatch(world, empty)
        self.assertEqual(res.status, "executed")
        self.assertTrue(res.receipt["price_unverified"])
        self.assertEqual(res.receipt["oracle_failure_code"],
                         "ORACLE_UNAVAILABLE")


class OracleCauseVerdictTests(unittest.TestCase):
    def _evaluate(self, oracle):
        registry, adapters, _o, policy, ledger, clock = make_world()
        return evaluate_payment(
            registry=registry, adapters=adapters, oracle=oracle,
            policy=policy, wallet_id=WALLET, from_address=ALICE,
            asset_id=SANDBOX_ASSET, to=BOB, amount_base=1_000_000,
            spent_today_base=0, spent_today_usd_micro=0, clock=clock)

    def test_causes_are_distinguishable_on_the_verdict(self):
        clock = Clock()
        a = StaticOracle({}, clock=clock)
        a.set(SANDBOX_ASSET, "USD", "1.0000", source="src-a")
        b = StaticOracle({}, clock=clock)
        b.set(SANDBOX_ASSET, "USD", "1.0100", source="src-b")
        cases = {
            "ORACLE_DIVERGENCE": TwoSourceOracle(a, b, clock=clock),
            "ORACLE_UNAVAILABLE": StaticOracle({}, clock=clock),
        }
        stale = StaticOracle({}, clock=clock)
        stale.set(SANDBOX_ASSET, "USD", "1.0",
                  observed_at=clock() - 10_000, source="old")
        cases["PRICE_STALE"] = stale
        for expected, oracle in cases.items():
            with self.subTest(cause=expected):
                v = self._evaluate(oracle)
                # Pegged degrade: allowed, unverified, code stays None —
                # the CAUSE is what distinguishes the three.
                self.assertEqual(v.decision, "allow")
                self.assertIsNone(v.code)
                self.assertTrue(v.price_unverified)
                self.assertEqual(v.oracle_failure_code, expected)

    def test_no_oracle_at_all_records_unavailable(self):
        v = self._evaluate(None)
        self.assertTrue(v.price_unverified)
        self.assertEqual(v.oracle_failure_code, "ORACLE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
