"""F-1 (B1 testnet bench finding): Herald mandate status is wired at
dispatch. A presented mandate credential is verified with Herald
(signature + expiry + status-list revocation + scope) BEFORE signing
and BEFORE the desk nonce is consumed; a revoked / expired / unknown /
malformed mandate is a typed refusal with the nonce untouched; an
accepted mandate's jti binds onto the receipt.

Layers: (1) Dispatcher-level with a hermetic scratch Herald issuer +
status list (scratch mandates only — no real desk mandate is ever
revoked); (2) the MCP server surface over stdio with a fixture
ECLIPSE_HERALD_DIR; (3) the real rollout corpus — the 8 desk mandates
verify ALLOW through the production verifier, read-only.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.b0fixtures import *  # noqa: F401,F403,E402
from mandate_check import HeraldMandateVerifier, production_verifier  # noqa: E402
from policy import RefusalCode  # noqa: E402
from state_machine import Dispatcher, PaymentIntent, State  # noqa: E402

HERALD_DIR = Path.home() / "workspace" / "cyan-eclipse" / "eclipse-herald-mcp"
if str(HERALD_DIR) not in sys.path:
    sys.path.append(str(HERALD_DIR))
import identity as herald_identity  # noqa: E402
import jwscore  # noqa: E402
import mandates as herald_mandates  # noqa: E402
import status_list as herald_sl  # noqa: E402

TASK_HASH = hashlib.sha256(b"f1-task").hexdigest()
CARD_HASH = hashlib.sha256(b"f1-card").hexdigest()
AGENT_DID = "did:ethr:0xE28b991F048D139456660D4eE7aE4451ca337dc6"
SL_ID = "urn:test:f1:status-list"
SCOPE = {"assets": ["USDC"], "chains": ["sandbox:local"],
         "actions": ["pay"], "counterparties": [BOB],
         "per_tx_cap_base": 5_000_000, "aggregate_cap_base": 100_000_000}


class ScratchHerald:
    """A throwaway Herald: own issuer key, own status list file."""

    def __init__(self, tmp: Path):
        self.key = jwscore.generate_keypair(bits=1024, kid="f1-scratch")
        self.jwks = jwscore.jwks_export(self.key)
        self.sl_path = tmp / "status-list.json"
        self.sl = herald_sl.StatusList.create(self.sl_path, SL_ID)

    def verifier(self) -> HeraldMandateVerifier:
        return HeraldMandateVerifier(jwks=self.jwks,
                                     status_list_path=self.sl_path)

    def issue(self, jti: str, scope=None, ttl=3600, now_ts=None,
              index=None, sl=None) -> str:
        sl = sl or self.sl
        idx = sl.allocate(jti) if index is None else index
        return herald_mandates.issue_mandate_credential(
            issuer_key=self.key, principal_did=herald_identity.ORG_DID,
            agent_did=AGENT_DID, mandate_type="PaymentMandate",
            stage="closed", scope=scope or SCOPE, task_hash=TASK_HASH,
            counterparty_card_hash=CARD_HASH,
            status_list_id=sl.status_list_id, status_index=idx,
            ttl_seconds=ttl, now_ts=now_ts, jti=jti)


def _intent(credential=None, amount=1_000_000):
    return PaymentIntent(wallet_id=WALLET, from_address=ALICE,
                         asset_id=SANDBOX_ASSET, chain_id="sandbox:local",
                         to=BOB, amount_base=amount, desk_auth_ok=True,
                         mandate_credential=credential)


class DispatcherMandateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="f1-dispatch-")
        self.herald = ScratchHerald(Path(self.tmp.name))
        self.world = make_world()
        registry, adapters, oracle, policy, ledger, clock = self.world
        self.ledger = ledger
        self.dispatcher = Dispatcher(
            registry=registry, adapters=adapters, oracle=oracle,
            policy=policy, clock=clock,
            mandate_verifier=self.herald.verifier())

    def tearDown(self):
        self.tmp.cleanup()

    def _assert_untouched(self, it):
        self.assertEqual(it.state, State.QUEUED)
        self.assertEqual(self.dispatcher.nonces.peek(WALLET), 0)
        self.assertFalse(self.dispatcher.nonces.is_consumed(WALLET, 0))
        self.assertEqual(it.receipts, [])
        self.assertEqual(
            self.ledger.read(BOB, SANDBOX_ASSET).amount_base, 0)

    def test_valid_mandate_executes_and_binds_jti(self):
        token = self.herald.issue("mdt_f1_valid")
        it = _intent(credential=token)
        res = self.dispatcher.dispatch(it)
        self.assertEqual(res.status, "executed")
        self.assertEqual(it.mandate_jti, "mdt_f1_valid")
        self.assertEqual(res.receipt["mandate_id"], "mdt_f1_valid")
        self.assertTrue(self.dispatcher.nonces.is_consumed(WALLET, 0))

    def test_revoked_scratch_mandate_refused_pre_sign(self):
        token = self.herald.issue("mdt_f1_revoked")
        # Revoke via a fresh load — exactly what the verifier will see.
        fresh = herald_sl.StatusList.load(self.herald.sl_path)
        idx = herald_mandates.verify_mandate_credential(
            token, self.herald.jwks)["status"]["statusListIndex"]
        fresh.revoke(idx)
        it = _intent(credential=token)
        res = self.dispatcher.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code, RefusalCode.MANDATE_REVOKED)
        self._assert_untouched(it)

    def test_expired_mandate_refused(self):
        token = self.herald.issue("mdt_f1_expired", ttl=3600,
                                  now_ts=int(time.time()) - 7200)
        it = _intent(credential=token)
        res = self.dispatcher.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code, RefusalCode.MANDATE_EXPIRED)
        self._assert_untouched(it)

    def test_tampered_signature_refused(self):
        token = self.herald.issue("mdt_f1_tampered")
        head, payload, sig = token.split(".")
        forged = f"{head}.{payload}.{sig[:-2]}" + \
            ("AA" if not sig.endswith("AA") else "BB")
        it = _intent(credential=forged)
        res = self.dispatcher.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code, RefusalCode.MANDATE_INVALID)
        self._assert_untouched(it)

    def test_unknown_mandate_refused(self):
        # Signed by the same issuer, but its status index was allocated
        # only in a DIFFERENT list (same list id): unknown to ours.
        other = herald_sl.StatusList.create(
            Path(self.tmp.name) / "other-list.json", SL_ID)
        for i in range(3):
            other.allocate(f"other-{i}")
        token = self.herald.issue("mdt_f1_unknown", index=2, sl=other)
        it = _intent(credential=token)
        res = self.dispatcher.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code, RefusalCode.MANDATE_UNKNOWN)
        self._assert_untouched(it)

    def test_scope_violation_refused(self):
        scope = dict(SCOPE, counterparties=["sandbox:carol"])
        token = self.herald.issue("mdt_f1_scope", scope=scope)
        it = _intent(credential=token)
        res = self.dispatcher.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code, RefusalCode.MANDATE_SCOPE)
        self._assert_untouched(it)

    def test_mandate_aggregate_cap_refused(self):
        scope = dict(SCOPE, aggregate_cap_base=500_000)
        token = self.herald.issue("mdt_f1_aggcap", scope=scope)
        it = _intent(credential=token)  # 1_000_000 > 500_000 aggregate
        res = self.dispatcher.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code, RefusalCode.MANDATE_SCOPE)
        self._assert_untouched(it)

    def test_no_mandate_unaffected_by_default(self):
        it = _intent()
        res = self.dispatcher.dispatch(it)
        self.assertEqual(res.status, "executed")
        self.assertIsNone(res.receipt["mandate_id"])

    def test_require_mandate_refuses_absent_credential(self):
        registry, adapters, oracle, policy, ledger, clock = self.world
        d = Dispatcher(registry=registry, adapters=adapters, oracle=oracle,
                       policy=policy, clock=clock,
                       mandate_verifier=self.herald.verifier(),
                       require_mandate=True)
        it = _intent()
        res = d.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code, RefusalCode.MANDATE_REQUIRED)
        self.assertFalse(d.nonces.is_consumed(WALLET, 0))

    def test_presented_mandate_without_verifier_fails_closed(self):
        registry, adapters, oracle, policy, ledger, clock = self.world
        d = Dispatcher(registry=registry, adapters=adapters, oracle=oracle,
                       policy=policy, clock=clock)  # no verifier wired
        token = self.herald.issue("mdt_f1_noverifier")
        it = _intent(credential=token)
        res = d.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code,
                         RefusalCode.MANDATE_STATUS_UNAVAILABLE)
        self.assertFalse(d.nonces.is_consumed(WALLET, 0))


class _StdioServer:
    def __init__(self, ledger_path, herald_dir):
        env = dict(os.environ, AGENT_WALLET_LEDGER=str(ledger_path),
                   ECLIPSE_HERALD_DIR=str(herald_dir))
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


class ServerMandateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="f1-server-")
        tmp = Path(self.tmp.name)
        self.herald = ScratchHerald(tmp / "herald-state")
        herald_dir = tmp / "herald"
        (herald_dir / "keys").mkdir(parents=True)
        (herald_dir / "state").mkdir(parents=True)
        (herald_dir / "keys" / "jwks.json").write_text(
            json.dumps(self.herald.jwks))
        # The production verifier reads <dir>/state/status-list.json.
        self.sl_path = herald_dir / "state" / "status-list.json"
        self.sl = herald_sl.StatusList.create(self.sl_path, SL_ID)
        self.herald.sl = self.sl  # issue against the served list
        self.herald.sl_path = self.sl_path
        self.server = _StdioServer(tmp / "ledger.json", herald_dir)
        out, err = self.server.tool(
            "create_wallet", {"owner_id": "owner-1", "label": "A"})
        self.assertFalse(err)
        self.a = out["wallet_id"]
        out, err = self.server.tool(
            "create_wallet", {"owner_id": "owner-1", "label": "B"})
        self.assertFalse(err)
        self.b_addr = out["address"]
        self.server.tool("fund_wallet", {"wallet_id": self.a,
                                         "amount_usdc": "50.00"})
        out, err = self.server.tool(
            "set_policy", {"wallet_id": self.a, "per_tx_cap_usdc": "10.00",
                           "daily_cap_usdc": "40.00",
                           "approval_above_usdc": "10.00"})
        self.assertFalse(err)

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def _mandate_for_b(self, jti):
        scope = {"assets": ["USDC"], "chains": ["sandbox:local"],
                 "actions": ["pay"], "counterparties": [self.b_addr],
                 "per_tx_cap_base": 10_000_000,
                 "aggregate_cap_base": 100_000_000}
        return self.herald.issue(jti, scope=scope)

    def test_pay_with_valid_mandate_binds_jti_on_receipt(self):
        token = self._mandate_for_b("mdt_f1_server_ok")
        out, err = self.server.tool(
            "pay", {"wallet_id": self.a, "to": self.b_addr,
                    "amount_usdc": "1.00", "mandate": token})
        self.assertFalse(err)
        self.assertEqual(out["status"], "executed")
        self.assertEqual(out["receipt"]["mandate_id"], "mdt_f1_server_ok")

    def test_pay_with_revoked_mandate_rejected(self):
        token = self._mandate_for_b("mdt_f1_server_revoked")
        idx = herald_mandates.verify_mandate_credential(
            token, self.herald.jwks)["status"]["statusListIndex"]
        herald_sl.StatusList.load(self.sl_path).revoke(idx)
        out, err = self.server.tool(
            "pay", {"wallet_id": self.a, "to": self.b_addr,
                    "amount_usdc": "1.00", "mandate": token})
        self.assertFalse(err)
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["refusal_code"], "MANDATE_REVOKED")
        out, err = self.server.tool("get_wallet", {"wallet_id": self.a})
        self.assertEqual(out["balance_usdc"], "50.00")

    def test_pay_without_mandate_unchanged(self):
        out, err = self.server.tool(
            "pay", {"wallet_id": self.a, "to": self.b_addr,
                    "amount_usdc": "1.00"})
        self.assertFalse(err)
        self.assertEqual(out["status"], "executed")
        self.assertIsNone(out["receipt"]["mandate_id"])


@unittest.skipUnless(
    (HERALD_DIR / "state" / "desk-mandate-rollout-20261027.json").exists()
    and (HERALD_DIR / "state" / "status-list.json").exists(),
    "Herald rollout state not present")
class RolloutCorpusTests(unittest.TestCase):
    """The desk mandates issued under the rollout are the corpus:
    every one verifies ALLOW through the production verifier
    (read-only — nothing is revoked or mutated here)."""

    @classmethod
    def setUpClass(cls):
        cls.rollout = json.loads(
            (HERALD_DIR / "state" / "desk-mandate-rollout-20261027.json")
            .read_text())
        cls.verifier = production_verifier()
        assert cls.verifier is not None

    def test_all_eight_desk_mandates_verify_allow(self):
        desks = self.rollout["desks"]
        self.assertEqual(len(desks), 8)
        for name, rec in desks.items():
            with self.subTest(desk=name):
                scope = rec["scope"]
                ctx = {"asset": scope["assets"][0],
                       "chain": scope["chains"][0], "action": "pay",
                       "counterparty": scope["counterparties"][0],
                       "amount_base": 1, "aggregate_spent_base": 0}
                jti = self.verifier.verify(rec["credential"], ctx)
                self.assertEqual(jti, rec["jti"])

    def test_status_list_indices_match_rollout_record(self):
        sl = herald_sl.StatusList.load(
            HERALD_DIR / "state" / "status-list.json")
        for rec in self.rollout["desks"].values():
            self.assertFalse(sl.is_revoked(rec["status_index"]))
        # The rollout's revoked DRILL scratch mandate stays revoked —
        # proof the list the verifier reads carries live revocations.
        drill = self.rollout["drill"]
        self.assertTrue(sl.is_revoked(drill["scratch_status_index"]))


if __name__ == "__main__":
    unittest.main()
