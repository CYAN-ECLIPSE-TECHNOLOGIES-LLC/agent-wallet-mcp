import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

try:
    import desk_envelope
    from fund_signer import create_fund_key, load_fund_signer
    from eth_account import Account
    HAVE_SIGNER_DEPS = True
except ImportError:  # system python: signer deps live in the venv
    HAVE_SIGNER_DEPS = False
    desk_envelope = create_fund_key = load_fund_signer = Account = None
from families.evm import JsonRpcClient
from oracle import OracleError, TwoSourceOracle
from oracle_sources import build_two_source_oracle
from policy import validate_grant
from state_machine import Dispatcher, PaymentIntent, State
from testnet_config import (ETH_TESTNET_ASSET_ID, USDC_TESTNET_ASSET_ID,
                            build_testnet_adapter, load_testnet_registry)
from tests.b0fixtures import *  # noqa: F401,F403
from tests.fake_rpc import FakeRpc
from tests.test_fund_signer import RECIPIENT, make_desk_auth


def fake_coinbase(url):
    if "USDC" in url:
        return {"data": {"amount": "1.0000", "base": "USDC",
                         "currency": "USD"}}
    return {"data": {"amount": "3000.00", "base": "ETH", "currency": "USD"}}


def fake_coingecko(url):
    if "usd-coin" in url:
        return {"usd-coin": {"usd": Decimal("0.9998")}}
    return {"ethereum": {"usd": Decimal("3001.00")}}


class TestnetAdapterTests(unittest.TestCase):
    def setUp(self):
        self.reg = load_testnet_registry()
        self.rpc = FakeRpc()
        self.adapter = build_testnet_adapter(
            self.reg, rpc_client=JsonRpcClient("http://fake",
                                               transport=self.rpc))

    def test_chain_backed_only_84532(self):
        self.assertTrue(self.adapter.submit_supported)
        self.assertEqual(self.adapter.chainId(), "eip155:84532")
        pol = self.adapter.finalityPolicy()
        self.assertEqual(pol["confirmations_required"], 6)
        self.assertEqual(pol["confirmed_confirmations"], 1)

    def test_status_progression(self):
        self.rpc.txs["0xabc"] = 100
        self.assertEqual(self.adapter.getStatus("0xabc"), "CONFIRMED")
        self.rpc.advance(5)
        self.assertEqual(self.adapter.getStatus("0xabc"), "FINAL")
        self.assertEqual(self.adapter.getStatus("0xmissing"), "UNKNOWN")

    def test_chain_balances(self):
        self.rpc.token_balances[RECIPIENT.lower()] = 123_456
        bal = self.adapter.readBalance(RECIPIENT, USDC_TESTNET_ASSET_ID)
        self.assertEqual((bal.amount_base, bal.state), (123_456, "spendable"))
        self.rpc.eth_balances[RECIPIENT.lower()] = 10**17
        bal = self.adapter.readBalance(RECIPIENT, ETH_TESTNET_ASSET_ID)
        self.assertEqual(bal.amount_base, 10**17)

    def test_unsigned_submit_refused(self):
        from families.base import AdapterError
        with self.assertRaises(AdapterError):
            self.adapter.submit({"signed": False, "raw_tx": None,
                                 "tx_hash": "0x0", "chain_id": "eip155:84532"})


class TwoSourceOracleCellTests(unittest.TestCase):
    def setUp(self):
        self.symbols = {USDC_TESTNET_ASSET_ID: "USDC",
                        ETH_TESTNET_ASSET_ID: "ETH"}
        self.clock = Clock(1_000_000)

    def test_two_sources_agree(self):
        oracle = build_two_source_oracle(self.symbols, clock=self.clock,
                                         fetch_a=fake_coinbase,
                                         fetch_b=fake_coingecko)
        snap = oracle.get_price(USDC_TESTNET_ASSET_ID, "USD")
        self.assertIn("coinbase", snap.source)
        self.assertIn("coingecko", snap.source)
        eth = oracle.get_price(ETH_TESTNET_ASSET_ID, "USD")
        self.assertTrue(Decimal("2999") < eth.price_decimal()
                        < Decimal("3002"))

    def test_divergence_refused(self):
        def wild(url):
            return {"usd-coin": {"usd": Decimal("0.50")}}
        oracle = build_two_source_oracle(self.symbols, clock=self.clock,
                                         fetch_a=fake_coinbase, fetch_b=wild)
        with self.assertRaises(OracleError) as ctx:
            oracle.get_price(USDC_TESTNET_ASSET_ID, "USD")
        self.assertEqual(ctx.exception.code, "ORACLE_DIVERGENCE")

    def test_source_failure_is_unavailable(self):
        def dead(url):
            raise RuntimeError("boom")
        oracle = build_two_source_oracle(self.symbols, clock=self.clock,
                                         fetch_a=dead, fetch_b=fake_coingecko)
        with self.assertRaises(OracleError) as ctx:
            oracle.get_price(USDC_TESTNET_ASSET_ID, "USD")
        self.assertEqual(ctx.exception.code, "ORACLE_UNAVAILABLE")
        self.assertIsInstance(oracle, TwoSourceOracle)
        self.assertEqual(oracle.max_staleness_s, 300)  # C-1 stays ON


@unittest.skipUnless(HAVE_SIGNER_DEPS, "signer deps (eth_account/cryptography) live in the Anchor venv")
class DispatcherTestnetTests(unittest.TestCase):
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
        self.adapter = build_testnet_adapter(
            self.reg, rpc_client=JsonRpcClient("http://fake",
                                               transport=self.rpc),
            fund_signer=self.signer)
        self.clock = Clock(1_700_000_000)
        self.oracle = build_two_source_oracle(
            {USDC_TESTNET_ASSET_ID: "USDC", ETH_TESTNET_ASSET_ID: "ETH"},
            clock=self.clock, fetch_a=fake_coinbase, fetch_b=fake_coingecko)
        adapters = {"evm": self.adapter}
        self.policy = validate_grant(
            self.reg, adapters, WALLET, "owner-1",
            {USDC_TESTNET_ASSET_ID: {
                "per_tx_cap_base": 5_000_000, "daily_cap_base": 8_000_000,
                "approval_above_base": 4_000_000, "allowlist": [],
                "max_fee_base": 10**15}},
            aggregate_daily_cap_usd_micro=8_000_000, granted_at=self.clock())
        self.dispatcher = Dispatcher(
            registry=self.reg, adapters=adapters, oracle=self.oracle,
            policy=self.policy, clock=self.clock)
        self.desk = Account.create()

    def tearDown(self):
        self.tmp.cleanup()

    def intent(self, amount=1_000_000, with_auth=True):
        it = PaymentIntent(
            wallet_id=WALLET, from_address=self.signer.address,
            asset_id=USDC_TESTNET_ASSET_ID, chain_id="eip155:84532",
            to=RECIPIENT, amount_base=amount, memo="",
            desk_nonce=0, desk_auth_ok=True, idempotency_key="idem-1",
            created_at=self.clock())
        if with_auth:
            it.desk_public_key = self.desk._key_obj.public_key.to_bytes()
            it.desk_auth_signature = make_desk_auth(
                self.desk, wallet_id=WALLET,
                asset_id=USDC_TESTNET_ASSET_ID, chain_id="eip155:84532",
                to=RECIPIENT, amount_base=amount, memo="", desk_nonce=0,
                idempotency_key="idem-1")
        return it

    def test_end_to_end_confirmed_then_final_one_final_receipt(self):
        it = self.intent()
        res = self.dispatcher.dispatch(it)
        self.assertEqual(res.status, "executed")
        self.assertEqual(it.state, State.CONFIRMED)
        self.assertEqual(len(self.rpc.sent_raw), 1)
        self.assertEqual(len(it.receipts), 1)
        self.assertTrue(it.receipts[0]["finality"]["provisional"])
        self.rpc.advance(5)
        out = self.dispatcher.watch_finality(it, timeout_s=60,
                                             poll_interval_s=1,
                                             sleep=lambda s: None)
        self.assertEqual(out["status"], "FINAL")
        self.assertEqual(it.state, State.FINAL)
        finals = [r for r in it.receipts
                  if r["finality"]["state"] == "FINAL"]
        self.assertEqual(len(finals), 1)          # exactly ONE final receipt
        self.assertEqual(len(it.receipts), 2)    # + 1 provisional
        self.assertEqual(self.dispatcher.tracker.spent_base(
            WALLET, USDC_TESTNET_ASSET_ID), 1_000_000)

    def test_dispatch_without_desk_auth_presentation_refused(self):
        it = self.intent(with_auth=False)
        res = self.dispatcher.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code.value, "DESK_AUTH_INVALID")
        self.assertEqual(len(self.rpc.sent_raw), 0)   # zero broadcasts
        self.assertFalse(self.dispatcher.nonces.is_consumed(WALLET, 0))


if __name__ == "__main__":
    unittest.main()
