import unittest

from oracle import StaticOracle, TwoSourceOracle, OracleError
from policy import (AllowlistEntry, Refusal, RefusalCode, evaluate_payment,
                    validate_grant)
from tests.b0fixtures import *  # noqa: F401,F403


def ev(world, **over):
    registry, adapters, oracle, policy, ledger, clock = world
    kw = dict(registry=registry, adapters=adapters, oracle=oracle,
              policy=policy, wallet_id=WALLET, from_address=ALICE,
              asset_id=SANDBOX_ASSET, to=BOB, amount_base=1_000_000,
              spent_today_base=0, spent_today_usd_micro=0, clock=clock)
    kw.update(over)
    return evaluate_payment(**kw)


class GrantTests(unittest.TestCase):
    def test_grant_ok_and_hash_stable(self):
        world = make_world()
        self.assertEqual(world[3].policy_hash(), world[3].policy_hash())
        self.assertEqual(len(world[3].policy_hash()), 64)

    def test_grant_rejects_reserve_asset(self):
        registry, adapters, *_ = make_world()
        with self.assertRaises(Refusal) as ctx:
            validate_grant(registry, adapters, WALLET, "o",
                           {BTC_ASSET: {"per_tx_cap_base": 1,
                                        "daily_cap_base": 1,
                                        "approval_above_base": 1}},
                           aggregate_daily_cap_usd_micro=1)
        self.assertEqual(ctx.exception.code, RefusalCode.ASSET_RESERVE_ONLY)

    def test_grant_rejects_next_and_gas_assets(self):
        registry, adapters, *_ = make_world()
        for asset in (USDC_BASE, "eip155:8453/slip44:60"):
            with self.assertRaises(Refusal) as ctx:
                validate_grant(registry, adapters, WALLET, "o",
                               {asset: {"per_tx_cap_base": 1,
                                        "daily_cap_base": 1,
                                        "approval_above_base": 1}},
                               aggregate_daily_cap_usd_micro=1)
            self.assertEqual(ctx.exception.code,
                             RefusalCode.ASSET_NOT_ENABLED, asset)

    def test_grant_validates_allowlist_at_grant_time(self):
        registry, adapters, *_ = make_world()
        with self.assertRaises(Refusal) as ctx:
            validate_grant(
                registry, adapters, WALLET, "o",
                {SANDBOX_ASSET: {"per_tx_cap_base": 1, "daily_cap_base": 1,
                                 "approval_above_base": 1,
                                 "allowlist": [{"family": "sandbox",
                                                "chain_id": "sandbox:local",
                                                "address": "bogus!!"}]}},
                aggregate_daily_cap_usd_micro=1)
        self.assertEqual(ctx.exception.code,
                         RefusalCode.ADDRESS_INVALID_FOR_FAMILY)

    def test_grant_rejects_reserve_address_in_allowlist(self):
        import copy
        import json
        from pathlib import Path
        from assets import AssetRegistry
        raw = json.loads((Path(__file__).parent.parent /
                          "assets.json").read_text())
        data = copy.deepcopy(raw)
        for r in data["assets"]:
            if r["asset_id"] == SANDBOX_ASSET:
                r["reserve_addresses"] = ["sandbox:reserve1"]
        registry = AssetRegistry(data)
        _, adapters, *_ = make_world()
        with self.assertRaises(Refusal) as ctx:
            validate_grant(
                registry, adapters, WALLET, "o",
                {SANDBOX_ASSET: {"per_tx_cap_base": 1, "daily_cap_base": 1,
                                 "approval_above_base": 1,
                                 "allowlist": [{"family": "sandbox",
                                                "chain_id": "sandbox:local",
                                                "address": "sandbox:reserve1"}]}},
                aggregate_daily_cap_usd_micro=1)
        self.assertEqual(ctx.exception.code, RefusalCode.ASSET_RESERVE_ONLY)

    def test_grant_rejects_float_and_bool_caps(self):
        registry, adapters, *_ = make_world()
        for bad in (1.5, True, -1, "10"):
            with self.assertRaises(Refusal, msg=repr(bad)):
                validate_grant(
                    registry, adapters, WALLET, "o",
                    {SANDBOX_ASSET: {"per_tx_cap_base": bad,
                                     "daily_cap_base": 1,
                                     "approval_above_base": 1}},
                    aggregate_daily_cap_usd_micro=1)


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.world = make_world()

    def test_allow(self):
        v = ev(self.world)
        self.assertEqual((v.decision, v.code), ("allow", None))
        self.assertEqual(v.usd_value_micro, 1_000_000)

    def test_frozen_global_and_asset(self):
        registry, adapters, oracle, policy, ledger, clock = self.world
        policy.frozen_global = True
        self.assertEqual(ev(self.world).code, RefusalCode.POLICY_FROZEN)
        policy.frozen_global = False
        policy.frozen_assets.add(SANDBOX_ASSET)
        self.assertEqual(ev(self.world).code,
                         RefusalCode.POLICY_FROZEN_ASSET)

    def test_no_policy_and_expired(self):
        self.assertEqual(ev(self.world, policy=None).code,
                         RefusalCode.NO_POLICY)
        registry, adapters, oracle, policy, ledger, clock = self.world
        policy.expires_at = clock() - 1
        self.assertEqual(ev(self.world).code, RefusalCode.POLICY_EXPIRED)

    def test_caps(self):
        self.assertEqual(ev(self.world, amount_base=5_000_001).code,
                         RefusalCode.OVER_PER_TX_CAP)
        self.assertEqual(ev(self.world, amount_base=4_000_000,
                            spent_today_base=4_500_000).code,
                         RefusalCode.OVER_DAILY_CAP)

    def test_aggregate_usd_cap_is_anti_structuring(self):
        # Each payment is under every per-asset cap; the aggregate binds.
        v = ev(self.world, amount_base=3_000_000,
               spent_today_usd_micro=6_000_000)
        self.assertEqual(v.code, RefusalCode.OVER_USD_DAILY_CAP)

    def test_allowlist_and_address(self):
        registry, adapters, oracle, policy, ledger, clock = self.world
        policy.assets[SANDBOX_ASSET].allowlist = [
            AllowlistEntry("sandbox", "sandbox:local", "sandbox:carol")]
        self.assertEqual(ev(self.world).code, RefusalCode.NOT_ALLOWLISTED)
        self.assertEqual(ev(self.world, to="bogus!!").code,
                         RefusalCode.ADDRESS_INVALID_FOR_FAMILY)

    def test_balance_states(self):
        registry, adapters, oracle, policy, ledger, clock = self.world
        self.assertEqual(ev(self.world, amount_base=4_500_000,
                            ).decision, "approve")  # funded 10, threshold 4
        ledger.set_frozen(ALICE, SANDBOX_ASSET, True)
        v = ev(self.world)
        self.assertEqual(v.code, RefusalCode.BALANCE_ISSUER_FROZEN)
        world2 = make_world(fund=0)
        self.assertEqual(ev(world2).code, RefusalCode.INSUFFICIENT_BALANCE)

    def test_approval_threshold(self):
        v = ev(self.world, amount_base=4_500_000)
        self.assertEqual(v.decision, "approve")

    def test_depeg_warn_forces_approval_refuse_band_refuses(self):
        world = make_world(price="0.995")  # 50 bps off - inside warn? no: 50<100
        self.assertEqual(ev(world).decision, "allow")
        world = make_world(price="0.989")  # 110 bps - warn band
        v = ev(world)
        self.assertEqual(v.decision, "approve")
        self.assertEqual(v.depeg_bps, 110)
        world = make_world(price="0.97")   # 300 bps - refuse band
        v = ev(world)
        self.assertEqual((v.decision, v.code),
                         ("deny", RefusalCode.DEPEG_BAND_EXCEEDED))

    def test_oracle_missing_for_pegged_degrades_not_silent(self):
        registry, adapters, oracle, policy, ledger, clock = self.world
        v = ev(self.world, oracle=StaticOracle({}, clock=clock),
               amount_base=4_500_000)
        self.assertEqual(v.decision, "approve")  # unverified + above threshold
        self.assertTrue(v.price_unverified)


class OracleTests(unittest.TestCase):
    def test_two_source_ok(self):
        clock = Clock(10_000)
        a = StaticOracle({}, clock=clock)
        b = StaticOracle({}, clock=clock)
        a.set(SANDBOX_ASSET, "USD", "1.0000", source="a")
        b.set(SANDBOX_ASSET, "USD", "1.0001", source="b")
        snap = TwoSourceOracle(a, b, clock=clock).get_price(SANDBOX_ASSET)
        self.assertIn("a+b", snap.source)

    def test_divergence_refused_loudly(self):
        clock = Clock(10_000)
        a = StaticOracle({}, clock=clock)
        b = StaticOracle({}, clock=clock)
        a.set(SANDBOX_ASSET, "USD", "1.00", source="a")
        b.set(SANDBOX_ASSET, "USD", "0.90", source="b")
        with self.assertRaises(OracleError) as ctx:
            TwoSourceOracle(a, b, clock=clock).get_price(SANDBOX_ASSET)
        self.assertEqual(ctx.exception.code, "ORACLE_DIVERGENCE")
        # ...and the policy engine surfaces it as a typed verdict code
        world = make_world(clock=clock)
        v = ev(world, oracle=TwoSourceOracle(a, b, clock=clock),
               amount_base=4_500_000)
        self.assertIn(v.decision, ("approve", "deny"))
        self.assertTrue(v.price_unverified or v.code ==
                        RefusalCode.ORACLE_DIVERGENCE)

    def test_staleness_refused(self):
        clock = Clock(10_000)
        a = StaticOracle({}, clock=clock)
        b = StaticOracle({}, clock=clock)
        a.set(SANDBOX_ASSET, "USD", "1.0", observed_at=10_000 - 10_000,
              source="a")
        b.set(SANDBOX_ASSET, "USD", "1.0", observed_at=10_000, source="b")
        with self.assertRaises(OracleError) as ctx:
            TwoSourceOracle(a, b, max_staleness_s=300,
                            clock=clock).get_price(SANDBOX_ASSET)
        self.assertEqual(ctx.exception.code, "PRICE_STALE")


if __name__ == "__main__":
    unittest.main()
