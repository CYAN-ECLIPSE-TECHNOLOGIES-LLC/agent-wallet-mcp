import tempfile
import unittest
from pathlib import Path

try:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    import desk_envelope
    import desk_verify
    from fund_signer import (FundSigner, FundSignerError, create_fund_key,
                             load_fund_signer)
    HAVE_SIGNER_DEPS = True
except ImportError:  # system python: signer-side deps live in the venv
    HAVE_SIGNER_DEPS = False
    Account = encode_defunct = desk_envelope = desk_verify = None
    FundSigner = FundSignerError = create_fund_key = load_fund_signer = None

from families.base import AdapterError
from families.evm import EvmAdapter, JsonRpcClient
from testnet_config import (ETH_TESTNET_ASSET_ID, USDC_TESTNET_ASSET_ID,
                            load_testnet_registry)
from tests.b0fixtures import *  # noqa: F401,F403
from tests.fake_rpc import FakeRpc

RECIPIENT = "0x52908400098527886E0F7030069857D2E4169EE7"


def make_desk_auth(desk_acct, **fields):
    msg = desk_verify.spend_message_v2(
        fields["wallet_id"], fields["asset_id"], fields["chain_id"],
        fields["to"], fields["amount_base"], fields["memo"],
        fields["desk_nonce"], fields["idempotency_key"])
    sig = Account.sign_message(encode_defunct(msg), desk_acct.key)
    return sig.signature.hex()


@unittest.skipUnless(HAVE_SIGNER_DEPS, "signer deps (eth_account/cryptography) live in the Anchor venv")
class FundSignerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.vault = base / "funds"
        self.master = base / "funds-master.key"
        desk_envelope.ensure_master_key(self.master)
        self.info = create_fund_key(
            self.vault, self.master, chain_id="eip155:84532",
            asset_scope=[USDC_TESTNET_ASSET_ID, ETH_TESTNET_ASSET_ID],
            purpose="test")
        self.signer = load_fund_signer(self.vault, self.master)
        self.desk = Account.create()
        self.desk_pub = self.desk._key_obj.public_key.to_bytes()

    def tearDown(self):
        self.tmp.cleanup()

    def artifact(self, amount=1_000_000):
        reg = load_testnet_registry()
        adapter = EvmAdapter(reg, "eip155:84532", chain_backed=True,
                             rpc_client=JsonRpcClient("http://fake",
                                                      transport=FakeRpc()),
                             fund_signer=self.signer)
        art = adapter.buildPayment({"intent_id": "i1",
                                    "asset_id": USDC_TESTNET_ASSET_ID,
                                    "to": RECIPIENT, "amount_base": amount,
                                    "from_address": self.signer.address})
        return adapter.finalize_unsigned(art)

    def auth(self, amount=1_000_000, **over):
        fields = {"wallet_id": "wal_t", "asset_id": USDC_TESTNET_ASSET_ID,
                  "chain_id": "eip155:84532", "to": RECIPIENT,
                  "amount_base": amount, "memo": "", "desk_nonce": 0,
                  "idempotency_key": "idem-1"}
        fields.update(over)
        fields["desk_auth_signature"] = make_desk_auth(self.desk, **fields)
        return fields


class MainnetGuardTests(unittest.TestCase):
    def test_mainnet_refused_at_construction(self):
        reg = AssetRegistry()
        for chain in ("eip155:8453", "eip155:1", "eip155:11155111"):
            with self.assertRaises(AdapterError) as ctx:
                EvmAdapter(reg, chain, chain_backed=True,
                           rpc_url="http://example.invalid")
            self.assertEqual(ctx.exception.code, "MAINNET_REFUSED")

    def test_rpc_endpoint_required_from_config(self):
        reg = load_testnet_registry()
        import os
        old = os.environ.pop("AGENT_WALLET_BASE_SEPOLIA_RPC", None)
        try:
            with self.assertRaises(AdapterError) as ctx:
                EvmAdapter(reg, "eip155:84532", chain_backed=True)
            self.assertEqual(ctx.exception.code, "RPC_NOT_CONFIGURED")
        finally:
            if old:
                os.environ["AGENT_WALLET_BASE_SEPOLIA_RPC"] = old

    @unittest.skipUnless(HAVE_SIGNER_DEPS, "signer deps live in the Anchor venv")
    def test_fund_key_creation_refuses_mainnet(self):
        with tempfile.TemporaryDirectory() as td:
            master = Path(td) / "m.key"
            desk_envelope.ensure_master_key(master)
            with self.assertRaises(FundSignerError) as ctx:
                create_fund_key(Path(td) / "v", master,
                                chain_id="eip155:8453", asset_scope=[],
                                purpose="x")
            self.assertEqual(ctx.exception.code, "MAINNET_REFUSED")


class FundSignerInvariantTests(FundSignerFixture):
    def test_signs_valid_erc20(self):
        signed = self.signer.sign_pay(
            self.artifact(), authorized_intent=self.auth(),
            desk_public_key=self.desk_pub, fee_quote_max_base=10**15)
        self.assertTrue(signed["signed"])
        self.assertEqual(signed["value_base"], 0)
        self.assertTrue(signed["raw_tx"].startswith("0x"))
        self.assertEqual(signed["from"], self.signer.address)

    def test_refuses_without_desk_auth(self):
        with self.assertRaises(FundSignerError) as ctx:
            self.signer.sign_pay(self.artifact(), authorized_intent=None,
                                 desk_public_key=self.desk_pub,
                                 fee_quote_max_base=10**15)
        self.assertEqual(ctx.exception.code, "DESK_AUTH_MISSING")

    def test_refuses_tampered_auth(self):
        auth = self.auth()
        auth["desk_auth_signature"] = self.auth(amount=9)[
            "desk_auth_signature"]  # signature for a different amount
        with self.assertRaises(FundSignerError) as ctx:
            self.signer.sign_pay(self.artifact(), authorized_intent=auth,
                                 desk_public_key=self.desk_pub,
                                 fee_quote_max_base=10**15)
        self.assertEqual(ctx.exception.code, "DESK_AUTH_INVALID")

    def test_refuses_amount_mismatch(self):
        with self.assertRaises(FundSignerError) as ctx:
            self.signer.sign_pay(self.artifact(amount=2_000_000),
                                 authorized_intent=self.auth(),
                                 desk_public_key=self.desk_pub,
                                 fee_quote_max_base=10**15)
        self.assertEqual(ctx.exception.code, "AMOUNT_MISMATCH")

    def test_refuses_erc20_value(self):
        art = self.artifact()
        art["value_base"] = 5
        with self.assertRaises(FundSignerError) as ctx:
            self.signer.sign_pay(art, authorized_intent=self.auth(),
                                 desk_public_key=self.desk_pub,
                                 fee_quote_max_base=10**15)
        self.assertEqual(ctx.exception.code, "ERC20_VALUE_REFUSED")

    def test_refuses_fee_over_cap(self):
        with self.assertRaises(FundSignerError) as ctx:
            self.signer.sign_pay(self.artifact(),
                                 authorized_intent=self.auth(),
                                 desk_public_key=self.desk_pub,
                                 fee_quote_max_base=1)
        self.assertEqual(ctx.exception.code, "FEE_OVER_CAP")

    def test_envelope_is_fund_domain_in_own_vault(self):
        env = desk_envelope.read_envelope(
            Path(self.info["envelope"]))
        self.assertEqual(env["domain"], "fund")
        self.assertEqual(env["chain_id"], "eip155:84532")
        self.assertIsInstance(self.signer, FundSigner)


if __name__ == "__main__":
    unittest.main()
