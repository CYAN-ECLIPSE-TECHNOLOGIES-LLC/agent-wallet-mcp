# Copyright (c) 2026 Cyan Eclipse Technologies LLC
# SPDX-License-Identifier: MIT
"""F-T2 + F-T3 fix proofs (B1 testnet broadcast battery findings).

F-T2: a broadcast whose submit response is lost after the tx lands
must be RECONCILED (tx hash / sender nonce read back from chain)
before dispatch may call it refused; a refusal stands only on proven
non-inclusion and carries the reconciliation evidence; a retry of
the same intent - or a twin reusing the idempotency key - never
produces a second broadcast.

F-T3: fee quotes and fee caps bind the ESTIMATED TOTAL fee (L2 +
OP-Stack L1 data fee, the L1 leg fail-closed), and receipts record
the ACTUAL fee (L2 + l1Fee) next to the quote once the chain reports
it - never the quote dressed as the fee.
"""
import tempfile
import unittest
from pathlib import Path

try:
    import desk_envelope
    from fund_signer import create_fund_key, load_fund_signer
    from eth_account import Account
    HAVE_SIGNER_DEPS = True
except ImportError:  # system python: signer deps live in the venv
    HAVE_SIGNER_DEPS = False
    desk_envelope = create_fund_key = load_fund_signer = Account = None

from families.base import AdapterError
from families.evm import GAS_PRICE_ORACLE_ADDRESS, JsonRpcClient
from oracle_sources import build_two_source_oracle
from policy import validate_grant
from receipts import build_receipt
from state_machine import Dispatcher, PaymentIntent, State, TransitionError
from testnet_config import (ETH_TESTNET_ASSET_ID, USDC_TESTNET_ASSET_ID,
                            build_testnet_adapter, load_testnet_registry)
from tests.b0fixtures import *  # noqa: F401,F403
from tests.fake_rpc import FakeRpc
from tests.test_fund_signer import RECIPIENT, make_desk_auth
from tests.test_testnet_evm import fake_coingecko, fake_coinbase

# FakeRpc economics: gas_price 1_000_000 -> L2 quote 65000 * 1e6 * 2;
# GasPriceOracle estimate 100 gwei-ish flat; mined receipt reports
# gasUsed 50000 at the same price + a 900_000_000_000 l1Fee.
L2_QUOTE = 130_000_000_000
L1_ESTIMATE = 100_000_000_000
TOTAL_QUOTE = L2_QUOTE + L1_ESTIMATE
ACTUAL_L2 = 50_000 * 1_000_000
ACTUAL_L1 = 900_000_000_000
ACTUAL_TOTAL = ACTUAL_L2 + ACTUAL_L1


class DroppingSendTransport:
    """Wraps FakeRpc. eth_sendRawTransaction is intercepted: with
    land=True the fake chain records the tx (it LANDED) but the
    response is dropped; with land=False it never reaches the chain.
    `dropping=False` restores a healthy transport."""

    def __init__(self, inner, land=True):
        self.inner = inner
        self.land = land
        self.dropping = True
        self.send_attempts = 0

    def __call__(self, payload):
        if payload["method"] == "eth_sendRawTransaction" and self.dropping:
            self.send_attempts += 1
            if self.land:
                self.inner(payload)
            raise ConnectionError("submit response lost in transit")
        return self.inner(payload)


class NonceEatingTransport:
    """The send consumes the sender's chain nonce (something landed
    in that slot) but the tx itself is nowhere to be found and the
    response is lost: reconciliation can prove neither landing nor
    absence."""

    def __init__(self, inner, sender):
        self.inner = inner
        self.sender = sender.lower()
        self.send_attempts = 0

    def __call__(self, payload):
        if payload["method"] == "eth_sendRawTransaction":
            self.send_attempts += 1
            self.inner.nonces[self.sender] = \
                self.inner.nonces.get(self.sender, 0) + 1
            raise ConnectionError("submit response lost in transit")
        return self.inner(payload)


class DeadOracleTransport:
    """The GasPriceOracle eth_call always fails: no L1 estimate can
    be produced, so no fee quote may be issued (F-T3 fail-closed)."""

    def __init__(self, inner):
        self.inner = inner

    def __call__(self, payload):
        if payload["method"] == "eth_call" and \
                payload["params"][0].get("to") == GAS_PRICE_ORACLE_ADDRESS:
            raise ConnectionError("oracle unreachable")
        return self.inner(payload)


@unittest.skipUnless(HAVE_SIGNER_DEPS, "signer deps live in the Anchor venv")
class FixTestnetBase(unittest.TestCase):
    transport_factory = None  # fn(inner FakeRpc, signer) -> transport

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.master = base / "fm.key"
        desk_envelope.ensure_master_key(self.master)
        create_fund_key(base / "funds", self.master,
                        chain_id="eip155:84532",
                        asset_scope=[USDC_TESTNET_ASSET_ID,
                                     ETH_TESTNET_ASSET_ID], purpose="t")
        self.signer = load_fund_signer(base / "funds", self.master)
        self.reg = load_testnet_registry()
        self.rpc = FakeRpc()
        self.rpc.token_balances[self.signer.address.lower()] = 10_000_000
        self.rpc.eth_balances[self.signer.address.lower()] = 10**18
        transport = self.rpc if self.transport_factory is None else \
            self.transport_factory(self.rpc, self.signer)
        self.transport = transport
        self.adapter = build_testnet_adapter(
            self.reg,
            rpc_client=JsonRpcClient("http://fake", transport=transport,
                                     retries=0),
            fund_signer=self.signer)
        self.clock = Clock(1_700_000_000)
        self.oracle = build_two_source_oracle(
            {USDC_TESTNET_ASSET_ID: "USDC", ETH_TESTNET_ASSET_ID: "ETH"},
            clock=self.clock, fetch_a=fake_coinbase,
            fetch_b=fake_coingecko)
        self.adapters = {"evm": self.adapter}
        self.policy = self.make_policy(10**15)
        self.dispatcher = self.make_dispatcher(self.policy)
        self.desk = Account.create()

    def tearDown(self):
        self.tmp.cleanup()

    def make_policy(self, max_fee_base):
        return validate_grant(
            self.reg, self.adapters, WALLET, "owner-1",
            {USDC_TESTNET_ASSET_ID: {
                "per_tx_cap_base": 5_000_000, "daily_cap_base": 8_000_000,
                "approval_above_base": 4_000_000, "allowlist": [],
                "max_fee_base": max_fee_base}},
            aggregate_daily_cap_usd_micro=8_000_000,
            granted_at=self.clock())

    def make_dispatcher(self, policy):
        return Dispatcher(
            registry=self.reg, adapters=self.adapters, oracle=self.oracle,
            policy=policy, clock=self.clock,
            reconcile_window_s=30, reconcile_poll_s=5,
            sleep=lambda s: self.clock.advance(s))

    def intent(self, amount=1_000_000, desk_nonce=0, idem="idem-1"):
        it = PaymentIntent(
            wallet_id=WALLET, from_address=self.signer.address,
            asset_id=USDC_TESTNET_ASSET_ID, chain_id="eip155:84532",
            to=RECIPIENT, amount_base=amount, memo="",
            desk_nonce=desk_nonce, desk_auth_ok=True,
            idempotency_key=idem, created_at=self.clock())
        it.desk_public_key = self.desk._key_obj.public_key.to_bytes()
        it.desk_auth_signature = make_desk_auth(
            self.desk, wallet_id=WALLET,
            asset_id=USDC_TESTNET_ASSET_ID, chain_id="eip155:84532",
            to=RECIPIENT, amount_base=amount, memo="",
            desk_nonce=desk_nonce, idempotency_key=idem)
        return it

    def finalize(self, it):
        self.rpc.advance(5)
        out = self.dispatcher.watch_finality(it, timeout_s=60,
                                             poll_interval_s=1,
                                             sleep=lambda s: None)
        self.assertEqual(out["status"], "FINAL")
        self.assertEqual(it.state, State.FINAL)


class LostResponseLandedTests(FixTestnetBase):
    transport_factory = staticmethod(
        lambda inner, signer: DroppingSendTransport(inner, land=True))

    def test_lost_response_after_landing_reconciles_not_refuses(self):
        it = self.intent()
        res = self.dispatcher.dispatch(it)
        # The old behavior reported refused / CHAIN_UNHEALTHY here.
        self.assertEqual(res.status, "executed")
        self.assertIsNone(res.refusal_code)
        self.assertIsNotNone(res.evidence)
        self.assertEqual(res.evidence["conclusion"], "landed")
        self.assertEqual(res.evidence["found_by"], "receipt")
        self.assertEqual(it.state, State.CONFIRMED)
        self.assertEqual(it.tx_ref, it.submission["tx_hash"])
        # Exactly ONE chain-level broadcast happened.
        self.assertEqual(len(self.rpc.sent_raw), 1)
        self.assertEqual(self.transport.send_attempts, 1)
        self.assertEqual(len(it.reconciliation), 1)
        self.finalize(it)
        finals = [r for r in it.receipts
                  if r["finality"]["state"] == "FINAL"]
        self.assertEqual(len(finals), 1)      # exactly one FINAL receipt
        self.assertEqual(len(it.receipts), 2)
        # Spend recorded exactly once, at FINAL.
        self.assertEqual(self.dispatcher.tracker.spent_base(
            WALLET, USDC_TESTNET_ASSET_ID), 1_000_000)

    def test_final_receipt_records_actual_fee_next_to_quote(self):
        it = self.intent()
        self.dispatcher.dispatch(it)
        self.finalize(it)
        final = [r for r in it.receipts
                 if r["finality"]["state"] == "FINAL"][0]
        fee = final["fee"]
        self.assertEqual(fee["amount_base"], TOTAL_QUOTE)  # the quote
        self.assertEqual(fee["quoted_amount_base"], TOTAL_QUOTE)
        self.assertEqual(fee["actual_amount_base"], ACTUAL_TOTAL)
        self.assertEqual(fee["actual_l2_amount_base"], ACTUAL_L2)
        self.assertEqual(fee["actual_l1_fee_base"], ACTUAL_L1)
        from receipts import verify_receipt_hash
        self.assertTrue(verify_receipt_hash(final))


class LostResponseAbsentTests(FixTestnetBase):
    transport_factory = staticmethod(
        lambda inner, signer: DroppingSendTransport(inner, land=False))

    def test_proven_absent_refusal_carries_evidence(self):
        it = self.intent()
        res = self.dispatcher.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code.value, "CHAIN_UNHEALTHY")
        self.assertEqual(it.state, State.QUEUED)
        self.assertEqual(len(self.rpc.sent_raw), 0)   # never landed
        self.assertIsNotNone(res.evidence)
        self.assertEqual(res.evidence["conclusion"], "absent")
        self.assertEqual(res.evidence["last_sender_nonce"], 0)
        self.assertIn("reconciliation", res.reason)
        self.assertEqual(len(it.receipts), 0)
        self.assertEqual(self.dispatcher.tracker.spent_base(
            WALLET, USDC_TESTNET_ASSET_ID), 0)

    def test_retry_same_intent_broadcasts_once_after_proven_absence(self):
        it = self.intent()
        res1 = self.dispatcher.dispatch(it)
        self.assertEqual(res1.status, "refused")
        self.transport.dropping = False       # transport heals
        res2 = self.dispatcher.dispatch(it)   # the SAME intent retried
        self.assertEqual(res2.status, "executed")
        # Across both attempts exactly one broadcast ever reached the
        # chain, and the desk nonce was consumed exactly once.
        self.assertEqual(len(self.rpc.sent_raw), 1)
        self.assertTrue(self.dispatcher.nonces.is_consumed(WALLET, 0))
        self.assertEqual(self.dispatcher.nonces.peek(WALLET), 1)
        self.finalize(it)
        self.assertEqual(self.dispatcher.tracker.spent_base(
            WALLET, USDC_TESTNET_ASSET_ID), 1_000_000)


class InconclusiveSubmitTests(FixTestnetBase):
    transport_factory = staticmethod(
        lambda inner, signer: NonceEatingTransport(inner, signer.address))

    def test_inconclusive_parks_unknown_never_refused_never_rebroadcast(self):
        it = self.intent()
        res = self.dispatcher.dispatch(it)
        self.assertNotEqual(res.status, "refused")
        self.assertEqual(it.state, State.UNKNOWN)
        self.assertEqual(it.tx_ref, it.submission["tx_hash"])
        self.assertEqual(it.refusal_codes, [])
        self.assertEqual(res.evidence["conclusion"], "inconclusive")
        self.assertEqual(self.transport.send_attempts, 1)
        # A parked intent cannot be re-dispatched into a 2nd broadcast.
        with self.assertRaises(TransitionError):
            self.dispatcher.dispatch(it)
        self.assertEqual(self.transport.send_attempts, 1)


class TwinIntentIdempotencyTests(FixTestnetBase):
    def test_twin_intent_same_idempotency_key_never_broadcasts(self):
        a = self.intent()
        res_a = self.dispatcher.dispatch(a)
        self.assertEqual(res_a.status, "executed")
        self.finalize(a)
        self.assertEqual(len(self.rpc.sent_raw), 1)
        # A twin: new intent, FRESH desk nonce + valid auth, same
        # wallet + idempotency key (an operator retrying the payment).
        b = self.intent(amount=500_000, desk_nonce=1)
        res_b = self.dispatcher.dispatch(b)
        self.assertEqual(res_b.status, "refused")
        self.assertEqual(res_b.refusal_code.value, "DUPLICATE_SUBMISSION")
        self.assertIsNotNone(res_b.evidence)
        self.assertEqual(len(self.rpc.sent_raw), 1)   # no 2nd broadcast
        self.assertFalse(self.dispatcher.nonces.is_consumed(WALLET, 1))


class FeeQuoteTotalTests(FixTestnetBase):
    def test_quote_is_estimated_total_with_components(self):
        quote = self.adapter.quoteFee(
            {"intent_id": "q1", "asset_id": USDC_TESTNET_ASSET_ID,
             "to": RECIPIENT, "amount_base": 1_000_000,
             "from_address": self.signer.address})
        self.assertEqual(quote.l2_amount_base, L2_QUOTE)
        self.assertEqual(quote.l1_amount_base, L1_ESTIMATE)
        self.assertEqual(quote.amount_base, TOTAL_QUOTE)

    def test_policy_fee_cap_binds_the_total_not_just_l2(self):
        # Cap sits between the L2-only figure and the total: under
        # F-T3's bug this payment sailed through on the L2 leg alone.
        dispatcher = self.make_dispatcher(
            self.make_policy(max_fee_base=200_000_000_000))
        it = self.intent()
        res = dispatcher.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code.value, "FEE_OVER_CAP")
        self.assertEqual(len(self.rpc.sent_raw), 0)

    def test_signer_bounds_estimated_total(self):
        from fund_signer import FundSignerError
        reg = load_testnet_registry()
        adapter = build_testnet_adapter(
            reg, rpc_client=JsonRpcClient("http://fake",
                                          transport=FakeRpc()),
            fund_signer=self.signer)
        art = adapter.buildPayment(
            {"intent_id": "i1", "asset_id": USDC_TESTNET_ASSET_ID,
             "to": RECIPIENT, "amount_base": 1_000_000,
             "from_address": self.signer.address})
        finalized = adapter.finalize_unsigned(art)
        self.assertEqual(finalized["l1_fee_estimate_base"], L1_ESTIMATE)
        self.assertEqual(finalized["fee_estimate_total_base"], TOTAL_QUOTE)
        auth = {"wallet_id": WALLET, "asset_id": USDC_TESTNET_ASSET_ID,
                "chain_id": "eip155:84532", "to": RECIPIENT,
                "amount_base": 1_000_000, "memo": "", "desk_nonce": 0,
                "idempotency_key": "idem-1"}
        auth["desk_auth_signature"] = make_desk_auth(
            self.desk, **auth)
        pub = self.desk._key_obj.public_key.to_bytes()
        # Cap above the L2 leg but below the total -> refused.
        with self.assertRaises(FundSignerError) as ctx:
            self.signer.sign_pay(finalized, authorized_intent=auth,
                                 desk_public_key=pub,
                                 fee_quote_max_base=200_000_000_000)
        self.assertEqual(ctx.exception.code, "FEE_OVER_CAP")
        # Cap at the total -> signs.
        signed = self.signer.sign_pay(finalized, authorized_intent=auth,
                                      desk_public_key=pub,
                                      fee_quote_max_base=TOTAL_QUOTE)
        self.assertTrue(signed["signed"])
        self.assertEqual(signed["fee_estimate_total_base"], TOTAL_QUOTE)
        # No L1 estimate on the artifact -> fail-closed refusal.
        stripped = {k: v for k, v in finalized.items()
                    if k != "l1_fee_estimate_base"}
        with self.assertRaises(FundSignerError) as ctx:
            self.signer.sign_pay(stripped, authorized_intent=auth,
                                 desk_public_key=pub,
                                 fee_quote_max_base=10**15)
        self.assertEqual(ctx.exception.code, "FEE_OVER_CAP")


class L1EstimateFailClosedTests(FixTestnetBase):
    transport_factory = staticmethod(
        lambda inner, signer: DeadOracleTransport(inner))

    def test_quote_fails_closed_without_l1_estimate(self):
        with self.assertRaises(AdapterError) as ctx:
            self.adapter.quoteFee(
                {"intent_id": "q1", "asset_id": USDC_TESTNET_ASSET_ID,
                 "to": RECIPIENT, "amount_base": 1_000_000,
                 "from_address": self.signer.address})
        self.assertEqual(ctx.exception.code, "FEE_ESTIMATE_UNAVAILABLE")

    def test_dispatch_fails_closed_and_broadcasts_nothing(self):
        it = self.intent()
        with self.assertRaises(AdapterError) as ctx:
            self.dispatcher.dispatch(it)
        self.assertEqual(ctx.exception.code, "FEE_ESTIMATE_UNAVAILABLE")
        self.assertEqual(len(self.rpc.sent_raw), 0)


class SandboxInvarianceTests(unittest.TestCase):
    def test_sandbox_receipt_shape_unchanged(self):
        # Non-broadcast paths are untouched: a receipt built without a
        # chain actual carries exactly the pre-fix fee shape.
        registry, adapters, oracle, policy, ledger, clock = make_world()
        row = registry.require(SANDBOX_ASSET)
        r = build_receipt(payment_id="p", intent_id="i", wallet_id=WALLET,
                          asset_row=row, amount_base=10, verdict=None,
                          policy=None, provisional=True)
        self.assertIsNone(r["fee"])
        from families.base import FeeQuote
        q = FeeQuote(amount_base=0, fee_asset_id=SANDBOX_ASSET,
                     quoted_at=1, expires_at=2)
        r2 = build_receipt(payment_id="p", intent_id="i", wallet_id=WALLET,
                           asset_row=row, amount_base=10, fee_quote=q,
                           verdict=None, policy=None, provisional=True)
        self.assertEqual(set(r2["fee"].keys()),
                         {"amount_base", "fee_asset_id", "quoted_at",
                          "expires_at"})
        self.assertIsNone(q.l1_amount_base)
        self.assertIsNone(q.l2_amount_base)


if __name__ == "__main__":
    unittest.main()
