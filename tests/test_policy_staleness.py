"""C-1 / defect D-1 evidence: price staleness is enforced at the
POLICY layer, for any injected Oracle implementation - not only
inside TwoSourceOracle. A stale snapshot degrades exactly like an
oracle-side PRICE_STALE (pegged: price_unverified + forced approval
above the threshold; never silently verified)."""
import unittest

from oracle import StaticOracle, TwoSourceOracle
from policy import evaluate_payment
from state_machine import Dispatcher, PaymentIntent
from tests.b0fixtures import *  # noqa: F401,F403


def evaluate(world, amount, **kw):
    registry, adapters, oracle, policy, ledger, clock = world
    return evaluate_payment(
        registry=registry, adapters=adapters, oracle=oracle, policy=policy,
        wallet_id=WALLET, from_address=ALICE, asset_id=SANDBOX_ASSET,
        to=BOB, amount_base=amount, spent_today_base=0,
        spent_today_usd_micro=0, clock=clock, **kw)


class PolicyLayerStalenessTests(unittest.TestCase):
    def test_fresh_static_price_is_verified(self):
        world = make_world()
        v = evaluate(world, 1_000_000)
        self.assertEqual(v.decision, "allow")
        self.assertFalse(v.price_unverified)
        self.assertIsNotNone(v.oracle_ref)

    def test_stale_static_price_is_never_verified(self):
        # Verity D-1 probe shape: a StaticOracle snapshot aged far past
        # the bound, injected at the policy layer.
        world = make_world()
        world[5].advance(10_000)
        v = evaluate(world, 1_000_000)
        self.assertTrue(v.price_unverified)
        self.assertIsNone(v.oracle_ref)

    def test_stale_price_forces_approval_above_threshold(self):
        world = make_world()
        world[5].advance(301)
        v = evaluate(world, 4_500_000)  # approval threshold is 4_000_000
        self.assertEqual(v.decision, "approve")
        self.assertTrue(v.price_unverified)

    def test_future_price_is_stale(self):
        world = make_world()
        world[2].set(SANDBOX_ASSET, "USD", "1.0",
                     observed_at=world[5]() + 10_000, source="fixture")
        v = evaluate(world, 1_000_000)
        self.assertTrue(v.price_unverified)

    def test_max_staleness_is_a_policy_parameter(self):
        world = make_world()
        world[5].advance(11)
        v = evaluate(world, 1_000_000, max_staleness_s=10)
        self.assertTrue(v.price_unverified)
        v = evaluate(world, 1_000_000, max_staleness_s=60)
        self.assertFalse(v.price_unverified)

    def test_two_source_path_still_verified(self):
        clock = Clock()
        registry = AssetRegistry()
        ledger = SandboxLedger()
        sandbox = SandboxAdapter(ledger, clock=clock)
        adapters = {"sandbox": sandbox}
        a = StaticOracle({}, clock=clock)
        b = StaticOracle({}, clock=clock)
        a.set(SANDBOX_ASSET, "USD", "1.0", source="a")
        b.set(SANDBOX_ASSET, "USD", "1.0001", source="b")
        oracle = TwoSourceOracle(a, b, clock=clock)
        policy = validate_grant(
            registry, adapters, WALLET, "owner-1",
            {SANDBOX_ASSET: {"per_tx_cap_base": 5_000_000,
                             "daily_cap_base": 8_000_000,
                             "approval_above_base": 4_000_000,
                             "allowlist": []}},
            aggregate_daily_cap_usd_micro=8_000_000, granted_at=clock())
        ledger.credit(ALICE, SANDBOX_ASSET, 10_000_000)
        v = evaluate_payment(
            registry=registry, adapters=adapters, oracle=oracle,
            policy=policy, wallet_id=WALLET, from_address=ALICE,
            asset_id=SANDBOX_ASSET, to=BOB, amount_base=1_000_000,
            spent_today_base=0, spent_today_usd_micro=0, clock=clock)
        self.assertEqual(v.decision, "allow")
        self.assertFalse(v.price_unverified)

    def test_dispatch_recheck_sees_price_go_stale(self):
        world = make_world()
        registry, adapters, oracle, policy, ledger, clock = world
        d = Dispatcher(registry=registry, adapters=adapters, oracle=oracle,
                       policy=policy, clock=clock)
        it = PaymentIntent(wallet_id=WALLET, from_address=ALICE,
                           asset_id=SANDBOX_ASSET, chain_id="sandbox:local",
                           to=BOB, amount_base=1_000_000, desk_auth_ok=True)
        self.assertEqual(d.authorize(it).status, "authorized")
        clock.advance(301)  # price goes stale between authorize/dispatch
        res = d.dispatch(it)
        self.assertEqual(res.status, "executed")
        # The receipt carries the unverified flag - the staleness is on
        # the record, not silently verified away.
        self.assertTrue(res.receipt["price_unverified"])
        self.assertIsNone(res.receipt["oracle_ref"])
        self.assertEqual(d.nonces.peek(WALLET), 1)  # consumed exactly once


if __name__ == "__main__":
    unittest.main()
