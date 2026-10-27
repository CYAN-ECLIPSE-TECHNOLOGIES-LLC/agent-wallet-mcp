import unittest

from families.base import AdapterNotImplemented, SandboxLedger
from families.evm import is_valid_evm_address, keccak256, to_checksum_address
from families.sandbox import SandboxAdapter
from tests.b0fixtures import *  # noqa: F401,F403

EIP55_VECTORS = [
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
    "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
    "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
]
RECIPIENT = "0x52908400098527886E0F7030069857D2E4169EE7"


class KeccakTests(unittest.TestCase):
    def test_empty_vector(self):
        self.assertEqual(
            keccak256(b"").hex(),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")

    def test_eip55_vectors(self):
        for addr in EIP55_VECTORS:
            self.assertEqual(to_checksum_address(addr.lower()), addr)
            self.assertTrue(is_valid_evm_address(addr))

    def test_bad_checksum_rejected(self):
        bad = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD"  # last char flipped case
        self.assertFalse(is_valid_evm_address(bad))
        self.assertFalse(is_valid_evm_address("0x123"))
        self.assertFalse(is_valid_evm_address("not-an-address"))
        self.assertFalse(is_valid_evm_address(None))
        self.assertFalse(is_valid_evm_address(42))


class EvmAdapterTests(unittest.TestCase):
    def setUp(self):
        self.reg = AssetRegistry()
        self.adapter = make_evm_adapter(self.reg)

    def test_build_erc20_intent_unsigned(self):
        art = self.adapter.buildPayment({
            "intent_id": "int_1", "asset_id": USDC_BASE, "to": RECIPIENT,
            "amount_base": 2_500_000, "from_address": RECIPIENT})
        self.assertFalse(art["signed"])
        self.assertEqual(art["to"],
                         "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        self.assertEqual(art["value_base"], 0)
        self.assertTrue(art["data"].startswith("0xa9059cbb"))
        self.assertTrue(art["data"].endswith(format(2_500_000, "x").rjust(64, "0")))
        self.assertEqual(art["payment_to"], to_checksum_address(RECIPIENT))

    def test_build_native_intent(self):
        art = self.adapter.buildPayment({
            "intent_id": "int_2", "asset_id": "eip155:8453/slip44:60",
            "to": RECIPIENT, "amount_base": 10**15,
            "from_address": RECIPIENT})
        self.assertEqual(art["value_base"], 10**15)
        self.assertEqual(art["data"], "0x")

    def test_unpopulated_token_cannot_build(self):
        pyusd = "eip155:1/erc20:UNPOPULATED"
        l1 = make_evm_adapter(self.reg, chain_id="eip155:1")
        with self.assertRaises(Exception) as ctx:
            l1.buildPayment({"intent_id": "i", "asset_id": pyusd,
                             "to": RECIPIENT, "amount_base": 1,
                             "from_address": RECIPIENT})
        self.assertEqual(ctx.exception.code, "ASSET_NOT_ENABLED")

    def test_submit_is_typed_not_implemented(self):
        with self.assertRaises(AdapterNotImplemented):
            self.adapter.submit({})
        with self.assertRaises(AdapterNotImplemented):
            self.adapter.submitAnchor(b"x")

    def test_fee_quote_and_balance_states(self):
        ledger = SandboxLedger()
        adapter = EvmAdapter(self.reg, "eip155:8453", ledger=ledger,
                             clock=Clock(1000))
        q = adapter.quoteFee({"asset_id": USDC_BASE})
        self.assertEqual(q.fee_asset_id, "eip155:8453/slip44:60")
        self.assertFalse(q.is_expired(1000))
        self.assertTrue(q.is_expired(1031))
        self.assertEqual(adapter.readBalance(RECIPIENT, USDC_BASE).state,
                         "absent")
        ledger.credit(RECIPIENT, USDC_BASE, 500, frozen=True)
        bal = adapter.readBalance(RECIPIENT, USDC_BASE)
        self.assertEqual((bal.state, bal.amount_base), ("frozen", 500))


class SandboxAdapterTests(unittest.TestCase):
    def test_roundtrip(self):
        ledger = SandboxLedger()
        adapter = SandboxAdapter(ledger, clock=Clock(1000))
        ledger.credit(ALICE, SANDBOX_ASSET, 1000)
        art = adapter.buildPayment({"intent_id": "int_9", "asset_id":
                                     SANDBOX_ASSET, "to": BOB,
                                     "amount_base": 400, "from_address": ALICE})
        tx = adapter.submit(art)
        self.assertEqual(adapter.getStatus(tx), "FINAL")
        self.assertEqual(ledger.read(ALICE, SANDBOX_ASSET).amount_base, 600)
        self.assertEqual(ledger.read(BOB, SANDBOX_ASSET).amount_base, 400)
        self.assertEqual(adapter.finalityPolicy()["final_at"], "instant")
        self.assertTrue(adapter.health()["ok"])


if __name__ == "__main__":
    unittest.main()
