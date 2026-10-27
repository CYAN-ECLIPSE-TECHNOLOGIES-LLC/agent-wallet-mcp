"""B1 integration battery: the v0.3.0 server over its real stdio
transport - policy v2 + Dispatcher behind the tools, integer-only
amounts, receipts persisted exactly once, daily caps surviving a
process restart (tracker seeded from persisted receipts), the desk
v2 flow with v1 fallback, the pending-approval TTL (C-3), and a
transport-level malformed-args battery that must fail typed, never
crash."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import desk_verify  # noqa: E402
from ledger_v2 import DeltaStore  # noqa: E402
from receipts import verify_receipt_hash  # noqa: E402
from tests.test_desk_auth_v2 import PRIV, PUB_BYTES, sign  # noqa: E402

BOB = "sandbox:bob"


class Server:
    def __init__(self, ledger_path):
        env = dict(os.environ, AGENT_WALLET_LEDGER=str(ledger_path))
        self.p = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env=env)
        self._id = 0
        self.call("initialize", {})

    def call(self, method, params=None):
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params:
            req["params"] = params
        self.p.stdin.write(json.dumps(req) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def tool(self, name, args):
        resp = self.call("tools/call", {"name": name, "arguments": args})
        result = resp["result"]
        return json.loads(result["content"][0]["text"]), \
            bool(result.get("isError"))

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


class ServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ledger = Path(self.td.name) / "ledger.json"
        self.srv = Server(self.ledger)

    def tearDown(self):
        self.srv.close()
        self.td.cleanup()

    def wallet(self, srv=None, **kw):
        srv = srv or self.srv
        out, err = srv.tool("create_wallet", {"owner_id": "owner", **kw})
        self.assertFalse(err, out)
        return out["wallet_id"]

    def funded_policy_wallet(self, srv=None, per_tx="50", daily="80",
                             approval=None, fund="100"):
        srv = srv or self.srv
        wid = self.wallet(srv)
        out, err = srv.tool("fund_wallet", {"wallet_id": wid,
                                            "amount_usdc": fund})
        self.assertFalse(err, out)
        args = {"wallet_id": wid, "per_tx_cap_usdc": per_tx,
                "daily_cap_usdc": daily}
        if approval:
            args["approval_above_usdc"] = approval
        out, err = srv.tool("set_policy", args)
        self.assertFalse(err, out)
        return wid

    # ---------------------------------------------------------- surface
    def test_surface_is_exactly_ten_tools(self):
        resp = self.srv.call("tools/list")
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(names, ["create_wallet", "get_wallet",
                                 "set_policy", "fund_wallet",
                                 "check_payment", "pay", "approve_payment",
                                 "deny_payment", "audit_log", "revoke"])
        info = self.srv.call("initialize", {})["result"]["serverInfo"]
        self.assertEqual(info["version"], "0.3.0")

    # ------------------------------------------------------ happy paths
    def test_pay_executes_and_persists_exactly_one_receipt(self):
        wid = self.funded_policy_wallet()
        out, err = self.srv.tool("pay", {"wallet_id": wid, "to": BOB,
                                         "amount_usdc": "10"})
        self.assertFalse(err, out)
        self.assertEqual(out["status"], "executed")
        self.assertEqual(out["balance_usdc"], "90.00")
        state = DeltaStore(self.ledger).load()
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(len(state["receipts"]), 1)  # D-3 at the store
        r = state["receipts"][0]
        self.assertTrue(verify_receipt_hash(r))
        self.assertEqual(r["amount_base_units"], 10_000_000)
        self.assertEqual(r["finality"]["state"], "FINAL")
        self.assertFalse(r["finality"]["provisional"])
        self.assertEqual(r["receipt_hash"], out["receipt_hash"])
        self.assertEqual(state["wallets"][wid]["balances"]
                         ["sandbox:local/token:USDC"], 90_000_000)

    def test_daily_cap_survives_process_restart(self):
        wid = self.funded_policy_wallet(per_tx="50", daily="15")
        out, _ = self.srv.tool("pay", {"wallet_id": wid, "to": BOB,
                                       "amount_usdc": "10"})
        self.assertEqual(out["status"], "executed")
        self.srv.close()
        self.srv = Server(self.ledger)  # fresh process, same ledger
        out, _ = self.srv.tool("pay", {"wallet_id": wid, "to": BOB,
                                       "amount_usdc": "10"})
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["refusal_code"], "OVER_DAILY_CAP")
        w, _ = self.srv.tool("get_wallet", {"wallet_id": wid})
        self.assertEqual(w["spent_today_usdc"], "10.00")

    def test_approval_flow_executes_on_approve(self):
        wid = self.funded_policy_wallet(approval="5")
        out, _ = self.srv.tool("pay", {"wallet_id": wid, "to": BOB,
                                       "amount_usdc": "10"})
        self.assertEqual(out["status"], "approval_required")
        rid = out["request_id"]
        out, _ = self.srv.tool("approve_payment", {"request_id": rid})
        self.assertEqual(out["status"], "executed")
        state = DeltaStore(self.ledger).load()
        self.assertEqual(len(state["receipts"]), 1)
        self.assertEqual(state["pending"][rid]["status"], "approved")

    def test_pending_approval_expires(self):
        wid = self.funded_policy_wallet(approval="5")
        out, _ = self.srv.tool("pay", {"wallet_id": wid, "to": BOB,
                                       "amount_usdc": "10"})
        rid = out["request_id"]
        # Operator ages the pending request past its TTL, legitimately
        # re-anchored through the store (not a file hack).
        store = DeltaStore(self.ledger)
        state = store.load()
        state["pending"][rid]["expires_at"] = 1
        store.save(state)
        self.srv.close()
        self.srv = Server(self.ledger)
        out, _ = self.srv.tool("approve_payment", {"request_id": rid})
        self.assertEqual(out["status"], "expired")
        state = DeltaStore(self.ledger).load()
        self.assertEqual(len(state["receipts"]), 0)  # nothing executed

    def test_revoke_freezes_via_policy(self):
        wid = self.funded_policy_wallet()
        self.srv.tool("revoke", {"wallet_id": wid})
        out, _ = self.srv.tool("pay", {"wallet_id": wid, "to": BOB,
                                       "amount_usdc": "1"})
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["refusal_code"], "POLICY_FROZEN")

    def test_check_payment_decisions(self):
        wid = self.funded_policy_wallet(approval="5")
        out, _ = self.srv.tool("check_payment",
                               {"wallet_id": wid, "to": BOB,
                                "amount_usdc": "1"})
        self.assertEqual(out["decision"], "allow")
        out, _ = self.srv.tool("check_payment",
                               {"wallet_id": wid, "to": BOB,
                                "amount_usdc": "10"})
        self.assertEqual(out["decision"], "approve")
        out, _ = self.srv.tool("check_payment",
                               {"wallet_id": wid, "to": BOB,
                                "amount_usdc": "60"})
        self.assertEqual(out["decision"], "deny")
        self.assertEqual(out["refusal_code"], "OVER_PER_TX_CAP")

    # ------------------------------------------------------------ desk
    def test_desk_v2_flow_replay_and_v1_fallback(self):
        pub_hex = PUB_BYTES.hex()
        wid = self.wallet(desk_id="desk-t", desk_pubkey=pub_hex)
        self.srv.tool("fund_wallet", {"wallet_id": wid,
                                      "amount_usdc": "100"})
        self.srv.tool("set_policy", {"wallet_id": wid,
                                     "per_tx_cap_usdc": "50",
                                     "daily_cap_usdc": "80"})
        v2 = dict(wallet_id=wid, asset_id="sandbox:local/token:USDC",
                  chain_id="sandbox:local", to=BOB, amount_base=10_000_000,
                  memo="", nonce=1, idempotency_key="idem-1")
        sig = sign(PRIV, desk_verify.spend_digest(
            desk_verify.spend_message_v2(**v2))).hex()
        out, err = self.srv.tool("pay", {
            "wallet_id": wid, "to": BOB, "amount_usdc": "10",
            "authorization": {"nonce": 1, "signature": sig,
                              "idempotency_key": "idem-1"}})
        self.assertFalse(err, out)
        self.assertEqual(out["status"], "executed")
        # Replay of the same authorization is refused.
        out, _ = self.srv.tool("pay", {
            "wallet_id": wid, "to": BOB, "amount_usdc": "10",
            "authorization": {"nonce": 1, "signature": sig,
                              "idempotency_key": "idem-1"}})
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["refusal_code"], "DESK_AUTH_INVALID")
        # v1-signed authorization still works (v0.2.0 flow unchanged).
        v1_sig = sign(PRIV, desk_verify.spend_digest(
            desk_verify.spend_message(wid, BOB, 5_000_000, "", 2))).hex()
        out, _ = self.srv.tool("pay", {
            "wallet_id": wid, "to": BOB, "amount_usdc": "5",
            "authorization": {"nonce": 2, "signature": v1_sig}})
        self.assertEqual(out["status"], "executed")
        # A signature over the wrong amount is refused.
        bad_sig = sign(PRIV, desk_verify.spend_digest(
            desk_verify.spend_message_v2(**{**v2, "nonce": 3,
                                            "amount_base": 1}))).hex()
        out, _ = self.srv.tool("pay", {
            "wallet_id": wid, "to": BOB, "amount_usdc": "5",
            "authorization": {"nonce": 3, "signature": bad_sig,
                              "idempotency_key": "idem-3"}})
        self.assertEqual(out["status"], "rejected")
        # Idempotency-key reuse with a different intent is refused even
        # with a valid signature over that intent.
        v2b = {**v2, "nonce": 3, "amount_base": 5_000_000,
               "idempotency_key": "idem-1"}
        sig_b = sign(PRIV, desk_verify.spend_digest(
            desk_verify.spend_message_v2(**v2b))).hex()
        out, _ = self.srv.tool("pay", {
            "wallet_id": wid, "to": BOB, "amount_usdc": "5",
            "authorization": {"nonce": 3, "signature": sig_b,
                              "idempotency_key": "idem-1"}})
        self.assertEqual(out["status"], "rejected")
        self.assertIn("idempotency", out["reason"])
        # No authorization at all is refused.
        out, _ = self.srv.tool("pay", {"wallet_id": wid, "to": BOB,
                                       "amount_usdc": "1"})
        self.assertEqual(out["status"], "rejected")
        w, _ = self.srv.tool("get_wallet", {"wallet_id": wid})
        self.assertEqual(w["spend_nonce"], 2)  # exactly the 2 accepted

    # -------------------------------------------------------- malformed
    def test_malformed_battery(self):
        wid = self.funded_policy_wallet()
        cases = [
            ("pay", {"wallet_id": wid, "to": BOB, "amount_usdc": 1.5}),
            ("pay", {"wallet_id": wid, "to": BOB, "amount_usdc": "abc"}),
            ("pay", {"wallet_id": wid, "to": BOB, "amount_usdc": "-5"}),
            ("pay", {"wallet_id": wid, "to": BOB, "amount_usdc": "1.0000001"}),
            ("pay", {"wallet_id": "wal_nope", "to": BOB,
                     "amount_usdc": "1"}),
            ("fund_wallet", {"wallet_id": wid, "amount_usdc": "0"}),
            ("fund_wallet", {"wallet_id": wid, "amount_usdc": 2.5}),
            ("set_policy", {"wallet_id": wid, "per_tx_cap_usdc": "x",
                            "daily_cap_usdc": "1"}),
            ("create_wallet", {"owner_id": ""}),
            ("get_wallet", {"wallet_id": "wal_nope"}),
        ]
        for name, args in cases:
            out, err = self.srv.tool(name, args)
            self.assertTrue(err, (name, args, out))
            self.assertIn("error", out)
        # Typed refusals (not transport errors) for policy-level badness:
        out, err = self.srv.tool("pay", {"wallet_id": wid, "to": "bogus!!",
                                         "amount_usdc": "1"})
        self.assertFalse(err)
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["refusal_code"],
                         "ADDRESS_INVALID_FOR_FAMILY")
        out, err = self.srv.tool("pay", {"wallet_id": wid, "to": BOB,
                                         "amount_usdc": "60"})
        self.assertEqual(out["refusal_code"], "OVER_PER_TX_CAP")
        # The server is still alive and exact after the battery.
        out, _ = self.srv.tool("pay", {"wallet_id": wid, "to": BOB,
                                       "amount_usdc": "1"})
        self.assertEqual(out["status"], "executed")

    def test_v1_ledger_is_refused_with_instructions(self):
        v1 = {"wallets": {}, "audit": [], "pending": {}}
        self.ledger.write_text(json.dumps(v1))
        out, err = self.srv.tool("get_wallet", {"wallet_id": "wal_x"})
        self.assertTrue(err)
        self.assertIn("migrate_ledger", out["error"])


if __name__ == "__main__":
    unittest.main()
