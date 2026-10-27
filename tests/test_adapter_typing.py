"""C-2 / defect D-2 evidence: every adapter entry point fails TYPED
(AdapterError with a stable code) on missing fields / non-dict input.
The B0 defect was EvmAdapter.buildPayment({}) raising a bare KeyError;
these probes assert AdapterError specifically and never KeyError."""
import unittest

from families.base import AdapterError
from families.evm import EvmAdapter
from tests.b0fixtures import *  # noqa: F401,F403

GOOD_EVM = {"intent_id": "i1", "asset_id": USDC_BASE,
            "to": "0x" + "ab" * 20, "amount_base": 5,
            "from_address": "0x" + "cd" * 20}
GOOD_SBX = {"intent_id": "i1", "asset_id": SANDBOX_ASSET, "to": BOB,
            "amount_base": 5, "from_address": ALICE}


class AdapterTypingTests(unittest.TestCase):
    def setUp(self):
        self.registry = AssetRegistry()
        self.evm = EvmAdapter(self.registry, "eip155:8453", clock=Clock())
        self.sbx = SandboxAdapter(clock=Clock())

    def assert_typed(self, fn, code=None):
        with self.assertRaises(AdapterError) as cm:
            fn()
        self.assertNotIsInstance(cm.exception, KeyError)
        if code is not None:
            self.assertEqual(cm.exception.code, code)

    def test_evm_build_payment_empty(self):
        # The exact Verity D-2 probe.
        self.assert_typed(lambda: self.evm.buildPayment({}),
                          "INTENT_FIELD_MISSING")

    def test_evm_build_payment_each_missing_field(self):
        # The fields buildPayment actually consumes (from_address is a
        # dispatcher-side field; the EVM artifact does not carry it).
        for field in ("asset_id", "amount_base", "to", "intent_id"):
            bad = {k: v for k, v in GOOD_EVM.items() if k != field}
            self.assert_typed(lambda bad=bad: self.evm.buildPayment(bad),
                              "INTENT_FIELD_MISSING")

    def test_evm_build_payment_non_dict(self):
        for bad in (None, [], "x", 42):
            self.assert_typed(lambda bad=bad: self.evm.buildPayment(bad),
                              "INTENT_FIELD_MISSING")

    def test_evm_quote_fee_missing(self):
        self.assert_typed(lambda: self.evm.quoteFee({}),
                          "INTENT_FIELD_MISSING")
        self.assert_typed(lambda: self.evm.quoteFee(None),
                          "INTENT_FIELD_MISSING")

    def test_sandbox_build_payment_missing(self):
        self.assert_typed(lambda: self.sbx.buildPayment({}),
                          "INTENT_FIELD_MISSING")
        for field in GOOD_SBX:
            bad = {k: v for k, v in GOOD_SBX.items() if k != field}
            self.assert_typed(lambda bad=bad: self.sbx.buildPayment(bad),
                              "INTENT_FIELD_MISSING")
        self.assert_typed(lambda: self.sbx.buildPayment(None),
                          "INTENT_FIELD_MISSING")

    def test_sandbox_submit_missing(self):
        self.assert_typed(lambda: self.sbx.submit({}), "ARTIFACT_INVALID")
        self.assert_typed(lambda: self.sbx.submit(None), "ARTIFACT_INVALID")
        self.assert_typed(lambda: self.sbx.submit({"from": ALICE}),
                          "ARTIFACT_INVALID")

    def test_good_intents_still_build(self):
        art = self.evm.buildPayment(dict(GOOD_EVM))
        self.assertEqual(art["kind"], "evm-unsigned-intent")
        art = self.sbx.buildPayment(dict(GOOD_SBX))
        self.assertEqual(art["kind"], "sandbox-payment-intent")


if __name__ == "__main__":
    unittest.main()
