"""Integration step (d) evidence: the EURC dual cap (ASSET-SLATE S1 -
EUR cap + USD-equivalent aggregate, first to bind).

EURC rows are not spend-enabled in the shipped registry, so the tests
build a registry VARIANT with a sandbox-family EURC row spend-enabled
(the Verity pattern) and prove: the peg cap binds alone, the USD
aggregate binds alone, accumulation across payments is tracked, the
cap is inside the policy hash, and the dict codec round-trips it."""
import copy
import json
import unittest
from pathlib import Path

from assets import AssetRegistry
from families.base import SandboxLedger
from families.sandbox import SandboxAdapter
from oracle import StaticOracle
from policy import (Refusal, RefusalCode, evaluate_payment,
                    policy_from_dict, policy_to_dict, validate_grant)
from state_machine import Dispatcher, PaymentIntent
from tests.b0fixtures import ALICE, BOB, Clock, WALLET

EURC = "sandbox:local/token:EURC"
EURC_ROW = {
    "asset_id": EURC, "asset_group": "EURC", "symbol": "EURC",
    "display_name": "Sandbox EURC (test variant)", "family": "sandbox",
    "chain_id": "sandbox:local", "network": "sandbox",
    "contract_or_mint": None, "decimals": 6, "subunit": None,
    "transfer_method": "sandbox-ledger",
    "asset_class": "fiat-stable-non-usd", "role": "payment",
    "volatility_class": "pegged-fx",
    "peg": {"target": "EUR", "depeg_warn_bps": 100,
            "depeg_refuse_bps": 200},
    "fee_asset_id": EURC, "slate_tier": "NEXT",
    "implementation_status": "sandbox-live", "listed": True,
    "live": False, "used": True, "live_gate": "sandbox-only",
    "spend_enabled": True, "population_required": False,
    "address_source": "test variant - sandbox row", "reserve_addresses": [],
}


def make_eurc_world(clock=None, peg_cap=5_000_000, aggregate=10**12,
                    usd_price="1.08"):
    clock = clock or Clock()
    data = json.loads(Path(__file__).resolve().parent.parent
                      .joinpath("assets.json").read_text())
    data["assets"].append(copy.deepcopy(EURC_ROW))
    registry = AssetRegistry(data=data)
    ledger = SandboxLedger()
    sandbox = SandboxAdapter(ledger, clock=clock)
    adapters = {"sandbox": sandbox}
    oracle = StaticOracle({}, clock=clock)
    oracle.set(EURC, "EUR", "1.0", source="fixture")
    oracle.set(EURC, "USD", usd_price, source="fixture")
    policy = validate_grant(
        registry, adapters, WALLET, "owner-1",
        {EURC: {"per_tx_cap_base": 100_000_000,
                "daily_cap_base": 100_000_000,
                "approval_above_base": 100_000_000,
                "allowlist": [], "daily_cap_peg_micro": peg_cap}},
        aggregate_daily_cap_usd_micro=aggregate, granted_at=clock())
    ledger.credit(ALICE, EURC, 500_000_000)
    return registry, adapters, oracle, policy, ledger, clock


def evaluate(world, amount, **kw):
    registry, adapters, oracle, policy, ledger, clock = world
    return evaluate_payment(
        registry=registry, adapters=adapters, oracle=oracle, policy=policy,
        wallet_id=WALLET, from_address=ALICE, asset_id=EURC, to=BOB,
        amount_base=amount, spent_today_base=0, spent_today_usd_micro=0,
        clock=clock, **kw)


class DualCapTests(unittest.TestCase):
    def test_peg_cap_binds_alone(self):
        world = make_eurc_world(peg_cap=5_000_000)  # EUR 5/day
        v = evaluate(world, 6_000_000)  # EUR 6; USD 6.48 << aggregate
        self.assertEqual(v.decision, "deny")
        self.assertEqual(v.code, RefusalCode.OVER_PEG_DAILY_CAP)
        self.assertEqual(v.peg_value_micro, 6_000_000)

    def test_usd_aggregate_binds_alone(self):
        world = make_eurc_world(peg_cap=10**12, aggregate=5_000_000)
        v = evaluate(world, 4_900_000)  # EUR 4.9 ok; USD 5.292 > $5 cap
        self.assertEqual(v.decision, "deny")
        self.assertEqual(v.code, RefusalCode.OVER_USD_DAILY_CAP)

    def test_under_both_caps_allows(self):
        world = make_eurc_world(peg_cap=5_000_000, aggregate=50_000_000)
        v = evaluate(world, 4_000_000)
        self.assertEqual(v.decision, "allow")
        self.assertEqual(v.peg_value_micro, 4_000_000)
        self.assertEqual(v.usd_value_micro, 4_320_000)

    def test_peg_accumulation_binds(self):
        world = make_eurc_world(peg_cap=5_000_000)
        v = evaluate(world, 3_000_000, spent_today_peg_micro=3_000_000)
        self.assertEqual(v.code, RefusalCode.OVER_PEG_DAILY_CAP)

    def test_dispatcher_tracks_peg_spend(self):
        world = make_eurc_world(peg_cap=5_000_000)
        registry, adapters, oracle, policy, ledger, clock = world
        d = Dispatcher(registry=registry, adapters=adapters, oracle=oracle,
                       policy=policy, clock=clock)
        kw = dict(wallet_id=WALLET, from_address=ALICE, asset_id=EURC,
                  chain_id="sandbox:local", to=BOB, desk_auth_ok=True)
        r1 = d.dispatch(PaymentIntent(amount_base=3_000_000, **kw))
        self.assertEqual(r1.status, "executed")
        self.assertEqual(d.tracker.spent_peg_micro(WALLET, EURC), 3_000_000)
        it2 = PaymentIntent(amount_base=3_000_000, **kw)
        r2 = d.dispatch(it2)
        self.assertEqual(r2.status, "refused")
        self.assertEqual(r2.refusal_code, RefusalCode.OVER_PEG_DAILY_CAP)
        self.assertEqual(d.nonces.peek(WALLET), 1)  # only r1 consumed

    def test_grant_validation(self):
        registry, adapters, oracle, policy, ledger, clock = \
            make_eurc_world()
        with self.assertRaises(Refusal):
            validate_grant(
                registry, adapters, WALLET, "o",
                {EURC: {"per_tx_cap_base": 1, "daily_cap_base": 1,
                        "approval_above_base": 1,
                        "daily_cap_peg_micro": -1}},
                aggregate_daily_cap_usd_micro=1)
        with self.assertRaises(Refusal):
            validate_grant(
                registry, adapters, WALLET, "o",
                {EURC: {"per_tx_cap_base": 1, "daily_cap_base": 1,
                        "approval_above_base": 1,
                        "daily_cap_peg_micro": 1.5}},
                aggregate_daily_cap_usd_micro=1)

    def test_cap_is_in_policy_hash_and_codec(self):
        w1 = make_eurc_world(peg_cap=5_000_000)
        w2 = make_eurc_world(peg_cap=6_000_000)
        self.assertNotEqual(w1[3].policy_hash(), w2[3].policy_hash())
        doc = policy_to_dict(w1[3])
        back = policy_from_dict(WALLET, doc)
        self.assertEqual(back.policy_hash(), w1[3].policy_hash())
        self.assertEqual(back.assets[EURC].daily_cap_peg_micro, 5_000_000)


if __name__ == "__main__":
    unittest.main()
