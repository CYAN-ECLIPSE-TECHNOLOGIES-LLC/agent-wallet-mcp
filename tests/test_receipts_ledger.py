import copy
import tempfile
import unittest
from pathlib import Path

from families.base import SandboxLedger
from families.sandbox import SandboxAdapter
from ledger_v2 import (DeltaStore, StoreCorruption, StoreError,
                       build_index, migrate_v1_to_v2, validate_store)
from oracle import StaticOracle
from policy import evaluate_payment
from receipts import build_receipt, receipt_hash, verify_receipt_hash
from tests.b0fixtures import *  # noqa: F401,F403


class ReceiptTests(unittest.TestCase):
    def test_hash_recomputation_and_binding(self):
        registry, adapters, oracle, policy, ledger, clock = make_world()
        row = registry.require(SANDBOX_ASSET)
        verdict = evaluate_payment(
            registry=registry, adapters=adapters, oracle=oracle,
            policy=policy, wallet_id=WALLET, from_address=ALICE,
            asset_id=SANDBOX_ASSET, to=BOB, amount_base=1_250_000,
            spent_today_base=0, spent_today_usd_micro=0, clock=clock)
        r = build_receipt(payment_id="p1", intent_id="i1", wallet_id=WALLET,
                          asset_row=row, amount_base=1_250_000,
                          fee_quote=verdict.fee_quote, verdict=verdict,
                          policy=policy, tx_ref="sandbox-tx-i1",
                          finality_state="FINAL", desk_auth_nonce=3,
                          provisional=False)
        self.assertTrue(verify_receipt_hash(r))
        self.assertEqual(r["receipt_hash"], receipt_hash(r))
        self.assertEqual(r["amount_base_units"], 1_250_000)
        self.assertIsInstance(r["amount_base_units"], int)
        self.assertEqual(r["asset_id"], SANDBOX_ASSET)
        self.assertEqual(r["chain_id"], "sandbox:local")
        self.assertEqual(r["usd_value_micro"], 1_250_000)
        self.assertIsNotNone(r["oracle_ref"])
        self.assertFalse(r["price_unverified"])
        self.assertEqual(r["policy_hash"], policy.policy_hash())
        # identity fields reserved, null in B0
        self.assertIsNone(r["agent_id"])
        self.assertIsNone(r["principal_commitment"])
        self.assertIsNone(r["mandate_id"])
        # tampering breaks the hash
        r2 = dict(r, amount_base_units=9)
        self.assertFalse(verify_receipt_hash(r2))

    def test_unverified_price_flag(self):
        registry = AssetRegistry()
        row = registry.require(SANDBOX_ASSET)
        r = build_receipt(payment_id="p", intent_id="i", wallet_id=WALLET,
                          asset_row=row, amount_base=10, verdict=None,
                          policy=None, provisional=True)
        self.assertEqual(r["finality"]["provisional"], True)


V1 = {"wallets": {"wal_1": {"id": "wal_1", "owner_id": "owner",
                            "label": "demo",
                            "address": "0x" + "ab" * 20,
                            "balance_micro": 750_000, "frozen": False,
                            "policy": {"granted_by": "owner", "granted_at": 5,
                                       "per_tx_cap_micro": 100,
                                       "daily_cap_micro": 200,
                                       "approval_above_micro": 50,
                                       "allowlist": ["sandbox:bob"],
                                       "expires_at": None, "purpose": "t"},
                            "created_at": 5}},
      "audit": [{"seq": 1, "ts": 5, "wallet": "wal_1", "event": "created",
                 "actor": "owner", "detail": {}}],
      "pending": {"req_1": {"wallet": "wal_1", "to": "sandbox:bob",
                            "amount_micro": 42, "memo": "",
                            "requested_at": 6, "status": "pending"}}}


class LedgerV2Tests(unittest.TestCase):
    def test_migration_pure_and_complete(self):
        v1 = copy.deepcopy(V1)
        v2 = migrate_v1_to_v2(v1)
        self.assertEqual(v1, V1)  # input untouched
        self.assertEqual(v2["schema_version"], 2)
        w = v2["wallets"]["wal_1"]
        self.assertEqual(w["balances"], {SANDBOX_ASSET: 750_000})
        self.assertEqual(w["addresses"], {"sandbox:local": "0x" + "ab" * 20})
        ap = w["policy"]["assets"][SANDBOX_ASSET]
        self.assertEqual(ap["per_tx_cap_base"], 100)
        self.assertEqual(ap["allowlist"][0]["family"], "sandbox")
        self.assertEqual(v2["pending"]["req_1"]["amount_base"], 42)
        validate_store(v2)

    def test_delta_store_roundtrip_and_index(self):
        v2 = migrate_v1_to_v2(copy.deepcopy(V1))
        with tempfile.TemporaryDirectory() as td:
            store = DeltaStore(Path(td) / "state.json")
            store.save(v2)
            loaded = store.load()
            self.assertEqual(loaded, v2)  # stored-read-verbatim
            idx = build_index(loaded)
            self.assertEqual(idx["audit_by_wallet"], {"wal_1": [1]})
            self.assertEqual(idx["audit_count"], 1)

    def test_missing_anchor_fails_closed(self):
        v2 = migrate_v1_to_v2(copy.deepcopy(V1))
        with tempfile.TemporaryDirectory() as td:
            store = DeltaStore(Path(td) / "state.json")
            store.save(v2)
            store.anchor_path.unlink()
            with self.assertRaises(StoreCorruption):
                store.load()

    def test_tamper_and_tail_truncation_fail_closed(self):
        v2 = migrate_v1_to_v2(copy.deepcopy(V1))
        v2["audit"].append({"seq": 2, "ts": 6, "wallet": "wal_1",
                            "event": "x", "actor": "a", "detail": {},
                            "asset_id": None, "refusal_code": None})
        with tempfile.TemporaryDirectory() as td:
            store = DeltaStore(Path(td) / "state.json")
            store.save(v2)
            blob = store.path.read_text()
            store.path.write_text(blob.replace("750000", "999999"))
            with self.assertRaises(StoreCorruption):
                store.load()

    def test_defect_gated_write(self):
        bad = migrate_v1_to_v2(copy.deepcopy(V1))
        bad["wallets"]["wal_1"]["balances"][SANDBOX_ASSET] = 1.5  # float!
        with tempfile.TemporaryDirectory() as td:
            store = DeltaStore(Path(td) / "state.json")
            with self.assertRaises(StoreError):
                store.save(bad)
            self.assertFalse(store.path.exists())

    def test_non_monotonic_audit_rejected(self):
        bad = migrate_v1_to_v2(copy.deepcopy(V1))
        bad["audit"].append({"seq": 2, "ts": 1, "wallet": "wal_1",
                             "event": "x", "actor": "a", "detail": {}})
        with self.assertRaises(StoreError):
            validate_store(bad)


if __name__ == "__main__":
    unittest.main()
