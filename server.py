#!/usr/bin/env python3
# Eclipse Agent Wallet MCP
# Copyright © 2026 Cyan Eclipse Technologies LLC · cetech.ai

"""Eclipse Agent Wallet MCP — v0 reference implementation.

Powered by Cyan Eclipse Technologies (cetech.ai).

The thesis: Wells Fargo's Section 15 (effective Nov 24, 2026) makes an agent's
actions legally yours with NO controls attached. This server is the opposite
design — an agent wallet where responsibility follows SCOPE:

  * The human grants a written policy: per-transaction cap, daily cap,
    recipient allowlist, approval threshold, expiry.
  * The agent can only spend inside that scope. Anything outside it is
    rejected or routed to a human approval queue — never silently executed.
  * Every grant, payment, approval and revocation lands in an append-only
    audit log: the evidence of what was actually authorized.
  * A kill switch freezes the wallet instantly — revocation that is
    immediate and provable, not "notify the bank and wait."

SANDBOX ONLY: balances are simulated test USDC in a local JSON ledger.
No real funds move. Production rails (x402 / partner processors) plug
in behind the same policy engine.

v0.2.0 desk extension (2026-10-27): a wallet may be bound at creation
to a desk identity — a secp256k1 key held in envelope custody outside
this server (see desk_keys.py). Desk-bound wallets additionally require
a valid desk signature (EIP-191 over the canonical spend message,
verified in desk_verify.py) before any spend executes or queues for
approval; nonces are strictly increasing per wallet. Unbound wallets
behave exactly as v0.1.0 — the certified policy engine is unchanged.
The desk wallet address is the key's real EVM address.

v0.3.0 B1 integration (2026-10-27): the transport now runs on the B0
asset-aware core. All tool calls route through policy v2
(policy.evaluate_payment) + the state-machine Dispatcher, so the
dispatch-time full re-check (F4), nonce-exactly-once (F3), depeg
bands, frozen states and the USD aggregate cap apply to the live
surface, not just the core library. The v1 float parse_amount / MICRO
path is RETIRED: amounts enter via amounts.parse_amount into integer
base units and never touch a float. Desk authorization uses the v2
canonical message (desk_verify.spend_message_v2: wallet + asset x
chain + amount in base units + nonce + idempotency key); v1-signed
authorizations are still accepted so the v0.2.0 desk flow keeps
working unchanged. Every executed payment emits exactly ONE receipt
v2 (receipt contract, bench C-2/D-3), persisted in the store.
State is a schema-v2 DeltaStore (ledger_v2): the ledger file plus its
.anchor sidecar, anchor-checked and fail-closed on load. A v1-shaped
ledger is REFUSED with instructions to run tools/migrate_ledger.py —
never silently misread.

Transport: MCP over stdio (newline-delimited JSON-RPC 2.0), stdlib only.
State file: $AGENT_WALLET_LEDGER or ./ledger.json next to this file.
"""
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_verify
from amounts import AmountError, parse_amount
from assets import AssetRegistry
from families.base import SandboxLedger
from families.sandbox import SandboxAdapter
from ledger_v2 import SANDBOX_ASSET_ID, SANDBOX_CHAIN_ID, DeltaStore, StoreError
from oracle import StaticOracle
from policy import Refusal, policy_from_dict, policy_to_dict, validate_grant
from state_machine import (Dispatcher, NonceError, PaymentIntent,
                           SpendTracker)

LEDGER_PATH = Path(os.environ.get(
    "AGENT_WALLET_LEDGER", Path(__file__).with_name("ledger.json")))
SANDBOX_DECIMALS = 6  # sandbox USDC row decimals (registry-pinned)
PENDING_TTL_S = 900   # queued approvals expire after 15 min (C-3)
INTENT_TTL_S = 900    # payment intents carry the same TTL (D-4)


class ToolError(Exception):
    pass


def now() -> int:
    return int(time.time())


def fmt(micro: int) -> str:
    """Display-only 2dp rendering of integer micro-units (integer math;
    amounts themselves are never floats anywhere in this server)."""
    cents_total = (int(micro) + 5_000) // 10_000
    return f"{cents_total // 100}.{cents_total % 100:02d}"


def parse_usdc(value) -> int:
    """Boundary parse into integer base units. Floats are refused
    outright by amounts.parse_amount (the retired v1 float path)."""
    try:
        return parse_amount(value, SANDBOX_DECIMALS)
    except AmountError as e:
        raise ToolError(f"invalid amount: {e}")


# ------------------------------------------------------------------- state
def fresh_state() -> dict:
    return {"schema_version": 2, "wallets": {}, "audit": [],
            "pending": {}, "receipts": []}


def load_state() -> dict:
    if not LEDGER_PATH.exists():
        return fresh_state()
    try:
        raw = json.loads(LEDGER_PATH.read_text())
    except ValueError as e:
        raise ToolError(f"ledger at {LEDGER_PATH} is unparseable: {e}")
    if not isinstance(raw, dict) or raw.get("schema_version") != 2:
        raise ToolError(
            f"ledger at {LEDGER_PATH} is not schema v2 (v1 shape found). "
            "Run the operator migration first: "
            "tools/migrate_ledger.py --ledger <path>. Refusing to serve "
            "a mis-shaped ledger.")
    state = DeltaStore(LEDGER_PATH).load()  # anchor-checked, fail-closed
    state.setdefault("receipts", [])
    state.setdefault("idempotency", {})
    return state


def save_state(state: dict) -> None:
    DeltaStore(LEDGER_PATH).save(state)  # defect-gated + read-back verified


def audit(state: dict, wallet_id: str, event: str, actor: str, detail: dict,
          asset_id=None, refusal_code=None) -> dict:
    entry = {"seq": len(state["audit"]) + 1, "ts": now(), "wallet": wallet_id,
             "event": event, "actor": actor, "detail": detail,
             "asset_id": asset_id, "refusal_code": refusal_code}
    state["audit"].append(entry)
    return entry


def get_wallet(state: dict, wallet_id: str) -> dict:
    w = state["wallets"].get(wallet_id)
    if not w:
        raise ToolError(f"unknown wallet: {wallet_id}")
    return w


def wallet_balance(w: dict) -> int:
    return int((w.get("balances") or {}).get(SANDBOX_ASSET_ID, 0))


def spent_today_base(state: dict, wallet_id: str) -> int:
    """Today's executed spend, recomputed from persisted FINAL receipts
    (exactly one per executed payment - receipt contract D-3)."""
    day = now() // 86400
    return sum(r["amount_base_units"] for r in state.get("receipts", [])
               if r.get("wallet_id") == wallet_id
               and r.get("finality", {}).get("state") == "FINAL"
               and int(r.get("created_at", 0)) // 86400 == day)


# ----------------------------------------------------------------- runtime
class WalletNonceBook:
    """Dispatcher nonce book backed by the persisted wallet records.

    Desk-bound wallets keep v0.2.0 semantics: the next nonce is
    spend_nonce + 1 (the first desk nonce is 1) and consume() persists
    it. The dispatcher calls consume() exactly once, only after every
    dispatch check passes — so a persisted spend_nonce always means an
    accepted authorization (F3). Unbound wallets have no desk signature
    to replay, so their sequence is per-call in-memory only."""

    def __init__(self, state: dict):
        self._state = state
        self._mem: dict[str, int] = {}

    def _w(self, wallet_id: str) -> dict:
        return self._state["wallets"].get(wallet_id) or {}

    def peek(self, wallet_id: str) -> int:
        w = self._w(wallet_id)
        if w.get("desk"):
            return int(w.get("spend_nonce", 0)) + 1
        return self._mem.get(wallet_id, 0)

    def is_consumed(self, wallet_id: str, nonce: int) -> bool:
        w = self._w(wallet_id)
        if w.get("desk"):
            return nonce <= int(w.get("spend_nonce", 0))
        return nonce < self._mem.get(wallet_id, 0)

    def consume(self, wallet_id: str, nonce: int) -> None:
        if self.is_consumed(wallet_id, nonce):
            raise NonceError(f"nonce {nonce} for {wallet_id} already consumed")
        if nonce != self.peek(wallet_id):
            raise NonceError(f"nonce {nonce} is not the next nonce "
                             f"{self.peek(wallet_id)} for {wallet_id}")
        w = self._w(wallet_id)
        if w.get("desk"):
            w["spend_nonce"] = nonce
        else:
            self._mem[wallet_id] = nonce + 1


class Runtime:
    """Per-call assembly of the B0 core over the persisted v2 state:
    the sandbox ledger is seeded from stored balances, the spend
    tracker from today's persisted receipts, and the nonce book reads
    the wallet records themselves — so policy state survives process
    restarts exactly, with the store as the only truth."""

    def __init__(self, state: dict):
        self.state = state
        self.registry = AssetRegistry()
        self.ledger = SandboxLedger()
        self.adapter = SandboxAdapter(self.ledger)
        self.adapters = {"sandbox": self.adapter}
        # The sandbox asset is definitionally at its USD peg.
        self.oracle = StaticOracle({})
        self.oracle.set(SANDBOX_ASSET_ID, "USD", "1.0",
                        observed_at=now(), source="sandbox-peg")
        for w in state["wallets"].values():
            addr = (w.get("addresses") or {}).get(SANDBOX_CHAIN_ID)
            if not addr:
                continue
            for asset_id, bal in (w.get("balances") or {}).items():
                if bal:
                    self.ledger.credit(addr, asset_id, int(bal))
        self.tracker = SpendTracker()
        self._seed_tracker()

    def _seed_tracker(self) -> None:
        day = now() // 86400
        for r in self.state.get("receipts", []):
            if r.get("finality", {}).get("state") != "FINAL":
                continue
            if int(r.get("created_at", 0)) // 86400 != day:
                continue
            self.tracker.record(r["wallet_id"], r["asset_id"],
                                int(r["amount_base_units"]),
                                r.get("usd_value_micro"))

    def policy_for(self, w: dict):
        return policy_from_dict(w["id"], w.get("policy"),
                                frozen_global=bool(w.get("frozen_global")))

    def dispatcher_for(self, w: dict, desk_auth_verifier=None) -> Dispatcher:
        return Dispatcher(registry=self.registry, adapters=self.adapters,
                          oracle=self.oracle, policy=self.policy_for(w),
                          nonces=WalletNonceBook(self.state),
                          tracker=self.tracker,
                          desk_auth_verifier=desk_auth_verifier)

    def sync_wallets(self) -> None:
        """Write runtime ledger balances back to every wallet record."""
        for w in self.state["wallets"].values():
            addr = (w.get("addresses") or {}).get(SANDBOX_CHAIN_ID)
            if not addr:
                continue
            bal = self.ledger.read(addr, SANDBOX_ASSET_ID)
            w.setdefault("balances", {})[SANDBOX_ASSET_ID] = bal.amount_base


# ------------------------------------------------------------- desk authz
def _parse_nonce(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def verify_desk_authorization(w: dict, *, to: str, amount: int, memo: str,
                              idempotency_key: str, auth) -> tuple:
    """For desk-bound wallets: (ok, reason, nonce). The v2 canonical
    message is verified first (binds asset x chain + base units +
    idempotency key); a v1 signature is accepted as fallback so the
    v0.2.0 desk flow keeps working unchanged. Pure verification — no
    key material on this side."""
    if not isinstance(auth, dict):
        return False, "desk-bound wallet: spend authorization (signature) is required", None
    nonce = _parse_nonce(auth.get("nonce"))
    try:
        sig = bytes.fromhex(str(auth["signature"]))
        pub = bytes.fromhex(w["desk"]["pubkey"])
    except Exception:
        return False, "desk authorization malformed", None
    if nonce is None:
        return False, "desk authorization malformed", None
    last = int(w.get("spend_nonce", 0))
    if nonce <= last:
        return False, (f"desk authorization nonce {nonce} is not greater "
                       f"than last used {last} (replay refused)"), None
    if desk_verify.verify_spend_v2(
            pub, sig, wallet_id=w["id"], asset_id=SANDBOX_ASSET_ID,
            chain_id=SANDBOX_CHAIN_ID, to=to, amount_base=amount,
            memo=memo, nonce=nonce, idempotency_key=idempotency_key):
        return True, "desk authorization verified (v2)", nonce
    if desk_verify.verify_spend_v1(
            pub, sig, wallet_id=w["id"], to=to, amount_micro=amount,
            memo=memo, nonce=nonce):
        return True, "desk authorization verified (v1)", nonce
    return False, ("desk signature invalid for this wallet/spend "
                   "(cross-desk or forged authorization refused)"), None


def _auth_verifier(w: dict, auth, fallback_idem: str):
    """Dispatcher desk-auth verifier closure for desk-bound wallets:
    re-verifies the presented authorization against the exact intent
    at dispatch time (the dispatcher's F4 re-check point)."""
    if not w.get("desk"):
        return None

    def verify(intent) -> bool:
        ok, _reason, _nonce = verify_desk_authorization(
            w, to=intent.to, amount=intent.amount_base, memo=intent.memo,
            idempotency_key=intent.idempotency_key or fallback_idem,
            auth=auth)
        return ok

    return verify


def _idempotency_key(args: dict, auth) -> str:
    key = args.get("idempotency_key")
    if not key and isinstance(auth, dict):
        key = auth.get("idempotency_key")
    return str(key) if key else "pay_" + uuid.uuid4().hex[:16]


def _check_idempotency(state: dict, w: dict, key: str, to: str,
                       amount: int):
    seen = (state.get("idempotency") or {}).get(f"{w['id']}|{key}")
    if seen and (seen.get("to") != to or seen.get("amount_base") != amount):
        return ("idempotency key was already used for a different payment "
                "on this wallet (key reuse with a different intent refused)")
    return None


def _record_idempotency(state: dict, w: dict, key: str, to: str,
                        amount: int, nonce) -> None:
    state.setdefault("idempotency", {})[f"{w['id']}|{key}"] = {
        "to": to, "amount_base": amount, "nonce": nonce}


def build_intent(w: dict, to: str, amount: int, memo: str, *,
                 desk_nonce=None, idempotency_key: str = "",
                 approval_granted: bool = False,
                 desk_auth_ok: bool = False) -> PaymentIntent:
    return PaymentIntent(
        wallet_id=w["id"],
        from_address=w["addresses"][SANDBOX_CHAIN_ID],
        asset_id=SANDBOX_ASSET_ID, chain_id=SANDBOX_CHAIN_ID,
        to=to, amount_base=amount, memo=memo, desk_nonce=desk_nonce,
        desk_auth_ok=desk_auth_ok, approval_granted=approval_granted,
        ttl_s=INTENT_TTL_S, idempotency_key=idempotency_key)


# --------------------------------------------------------------------- tools
def t_create_wallet(state, args):
    owner = str(args.get("owner_id", "")).strip()
    if not owner:
        raise ToolError("owner_id is required — a wallet must have a named human owner")
    wid = "wal_" + uuid.uuid4().hex[:12]
    desk = None
    desk_id = str(args.get("desk_id", "")).strip()
    desk_pubkey = str(args.get("desk_pubkey", "")).strip().lower()
    if desk_id or desk_pubkey:
        if not desk_id or not desk_pubkey:
            raise ToolError("desk binding requires both desk_id and desk_pubkey")
        try:
            pub_bytes = bytes.fromhex(desk_pubkey)
            addr = desk_verify.desk_address(pub_bytes)
        except Exception:
            raise ToolError("desk_pubkey must be a 64-byte secp256k1 public key (hex)")
        desk = {"desk_id": desk_id, "pubkey": desk_pubkey}
    else:
        addr = "0x" + hashlib.sha256(wid.encode()).hexdigest()[:40]
    w = {"id": wid, "owner_id": owner, "label": args.get("label", ""),
         "addresses": {SANDBOX_CHAIN_ID: addr}, "balances": {},
         "frozen_global": False, "frozen_assets": [], "policy": None,
         "desk": desk, "spend_nonce": 0, "created_at": now()}
    state["wallets"][wid] = w
    detail = {"label": w["label"], "address": addr}
    if desk:
        detail.update({"desk_id": desk_id, "desk_pubkey": desk_pubkey,
                       "scope": "SANDBOX — simulated test USDC; desk-bound (signature-required)"})
    audit(state, wid, "wallet_created", owner, detail)
    return {"wallet_id": wid, "address": addr, "owner_id": owner,
            "desk_id": desk_id or None,
            "balance_usdc": "0.00", "status": "no policy — cannot spend yet"}


def t_get_wallet(state, args):
    w = get_wallet(state, args["wallet_id"])
    return {"wallet_id": w["id"], "owner_id": w["owner_id"], "label": w["label"],
            "address": (w.get("addresses") or {}).get(SANDBOX_CHAIN_ID),
            "balance_usdc": fmt(wallet_balance(w)),
            "frozen": bool(w.get("frozen_global")),
            "spent_today_usdc": fmt(spent_today_base(state, w["id"])),
            "desk": w.get("desk"), "spend_nonce": w.get("spend_nonce", 0),
            "policy": w.get("policy")}


def t_set_policy(state, args):
    w = get_wallet(state, args["wallet_id"])
    if w.get("frozen_global"):
        raise ToolError("wallet is frozen — revoke state cannot accept a new policy")
    per_tx = parse_usdc(args["per_tx_cap_usdc"])
    daily = parse_usdc(args["daily_cap_usdc"])
    approval = parse_usdc(args.get("approval_above_usdc",
                                   args["per_tx_cap_usdc"]))
    days = args.get("expires_in_days")
    if days is not None and (isinstance(days, bool)
                             or not isinstance(days, int)):
        raise ToolError("expires_in_days must be an integer")
    expires_at = (now() + days * 86400) if days else None
    allowlist = [{"family": "sandbox", "chain_id": SANDBOX_CHAIN_ID,
                  "address": str(a), "label": ""}
                 for a in args.get("allowlist", [])]
    rt = Runtime(state)
    try:
        pol = validate_grant(
            rt.registry, rt.adapters, w["id"], w["owner_id"],
            {SANDBOX_ASSET_ID: {"per_tx_cap_base": per_tx,
                                "daily_cap_base": daily,
                                "approval_above_base": approval,
                                "allowlist": allowlist}},
            aggregate_daily_cap_usd_micro=daily, expires_at=expires_at,
            purpose=str(args.get("purpose", "")), granted_at=now())
    except Refusal as e:
        raise ToolError(f"policy refused ({e.code.value}): {e.reason}")
    w["policy"] = policy_to_dict(pol)
    receipt = (f"Liability receipt: {w['owner_id']} granted agent scope for wallet {w['id']}: "
               f"max {fmt(per_tx)} USDC per transaction, "
               f"{fmt(daily)} per day, approval required above "
               f"{fmt(approval)}, recipients "
               f"{'restricted to allowlist' if allowlist else 'unrestricted'}, "
               f"purpose: {args.get('purpose') or 'unspecified'}. Responsibility follows this scope — "
               f"nothing outside it is authorized.")
    audit(state, w["id"], "policy_granted", w["owner_id"],
          {"policy": w["policy"], "policy_hash": pol.policy_hash(),
           "receipt": receipt}, asset_id=SANDBOX_ASSET_ID)
    return {"wallet_id": w["id"], "policy": w["policy"],
            "policy_hash": pol.policy_hash(), "liability_receipt": receipt}


def t_fund_wallet(state, args):
    w = get_wallet(state, args["wallet_id"])
    amount = parse_usdc(args["amount_usdc"])
    if amount <= 0:
        raise ToolError("amount must be positive")
    rt = Runtime(state)
    rt.ledger.credit(w["addresses"][SANDBOX_CHAIN_ID], SANDBOX_ASSET_ID,
                     amount)
    rt.sync_wallets()
    audit(state, w["id"], "funded_sandbox", "faucet",
          {"amount_base": amount}, asset_id=SANDBOX_ASSET_ID)
    return {"wallet_id": w["id"], "balance_usdc": fmt(wallet_balance(w)),
            "note": "SANDBOX funds — simulated test USDC, not real money"}


def t_check_payment(state, args):
    w = get_wallet(state, args["wallet_id"])
    amount = parse_usdc(args["amount_usdc"])
    rt = Runtime(state)
    intent = build_intent(w, str(args["to"]), amount, "",
                          desk_auth_ok=True)  # dry-run: no signature needed
    res = rt.dispatcher_for(w).authorize(intent)
    decision = {"authorized": "allow", "approval_required": "approve",
                "refused": "deny"}[res.status]
    bal = rt.ledger.read(w["addresses"][SANDBOX_CHAIN_ID],
                         SANDBOX_ASSET_ID).amount_base
    return {"decision": decision, "reason": res.reason,
            "refusal_code": (res.refusal_code.value
                             if res.refusal_code else None),
            "wallet_id": w["id"], "to": args["to"],
            "amount_usdc": fmt(amount),
            "authorization_required": bool(w.get("desk")),
            "balance_sufficient": bal >= amount}


def _queue_pending(state, w, intent, auth, reason) -> dict:
    rid = "req_" + uuid.uuid4().hex[:10]
    state["pending"][rid] = {
        "wallet": w["id"], "to": intent.to,
        "amount_base": intent.amount_base, "memo": intent.memo,
        "requested_at": now(), "status": "pending",
        "expires_at": now() + PENDING_TTL_S,
        "desk_nonce": intent.desk_nonce,
        "idempotency_key": intent.idempotency_key,
        "authorization": auth if w.get("desk") else None,
        "asset_id": SANDBOX_ASSET_ID}
    audit(state, w["id"], "approval_requested", "agent",
          {"request_id": rid, "to": intent.to,
           "amount_base": intent.amount_base, "reason": reason},
          asset_id=SANDBOX_ASSET_ID)
    return {"status": "approval_required", "request_id": rid,
            "reason": reason}


def _settle_executed(state, rt, w, intent, via_approval=None) -> dict:
    rt.sync_wallets()
    state.setdefault("receipts", []).extend(intent.receipts)
    if w.get("desk"):
        _record_idempotency(state, w, intent.idempotency_key, intent.to,
                            intent.amount_base, intent.desk_nonce)
    receipt = intent.receipts[-1] if intent.receipts else None
    detail = {"tx": intent.tx_ref, "to": intent.to,
              "amount_base": intent.amount_base, "memo": intent.memo,
              "receipt_hash": receipt["receipt_hash"] if receipt else None}
    if w.get("desk"):
        detail.update({"desk_id": w["desk"]["desk_id"],
                       "spend_nonce": w.get("spend_nonce", 0)})
    if via_approval:
        detail["via_approval"] = via_approval
    audit(state, w["id"], "payment_executed", "agent", detail,
          asset_id=SANDBOX_ASSET_ID)
    return {"status": "executed", "tx": intent.tx_ref, "to": intent.to,
            "amount_usdc": fmt(intent.amount_base),
            "balance_usdc": fmt(wallet_balance(w)),
            "receipt_hash": detail["receipt_hash"], "receipt": receipt}


def _rejected(state, w, intent, res) -> dict:
    code = res.refusal_code.value if res.refusal_code else None
    audit(state, w["id"], "payment_rejected", "agent",
          {"to": intent.to, "amount_base": intent.amount_base,
           "reason": res.reason}, asset_id=SANDBOX_ASSET_ID,
          refusal_code=code)
    return {"status": "rejected", "reason": res.reason,
            "refusal_code": code}


def t_pay(state, args):
    w = get_wallet(state, args["wallet_id"])
    to, amount = str(args["to"]), parse_usdc(args["amount_usdc"])
    memo = str(args.get("memo", ""))
    auth = args.get("authorization")
    idem = _idempotency_key(args, auth)
    nonce = None
    if w.get("desk"):
        ok, reason, nonce = verify_desk_authorization(
            w, to=to, amount=amount, memo=memo, idempotency_key=idem,
            auth=auth)
        if not ok:
            audit(state, w["id"], "payment_rejected", "agent",
                  {"to": to, "amount_base": amount, "reason": reason},
                  asset_id=SANDBOX_ASSET_ID,
                  refusal_code="DESK_AUTH_INVALID")
            return {"status": "rejected", "reason": reason,
                    "refusal_code": "DESK_AUTH_INVALID"}
        clash = _check_idempotency(state, w, idem, to, amount)
        if clash:
            audit(state, w["id"], "payment_rejected", "agent",
                  {"to": to, "amount_base": amount, "reason": clash},
                  asset_id=SANDBOX_ASSET_ID,
                  refusal_code="DESK_AUTH_INVALID")
            return {"status": "rejected", "reason": clash,
                    "refusal_code": "DESK_AUTH_INVALID"}
    rt = Runtime(state)
    intent = build_intent(w, to, amount, memo, desk_nonce=nonce,
                          idempotency_key=idem,
                          desk_auth_ok=not w.get("desk"))
    dispatcher = rt.dispatcher_for(
        w, desk_auth_verifier=_auth_verifier(w, auth, idem))
    res = dispatcher.authorize(intent)
    if res.status == "refused":
        return _rejected(state, w, intent, res)
    if res.status == "approval_required":
        return _queue_pending(state, w, intent, auth, res.reason)
    res = dispatcher.dispatch(intent)
    if res.status == "executed":
        return _settle_executed(state, rt, w, intent)
    if res.status == "approval_required":
        # Dispatch-time re-check escalated (e.g. price moved into the
        # approval band between authorize and dispatch): queue it.
        return _queue_pending(state, w, intent, auth, res.reason)
    return _rejected(state, w, intent, res)


def t_resolve_approval(state, args, approve: bool):
    rid = str(args["request_id"])
    req = state["pending"].get(rid)
    if not req or req["status"] != "pending":
        raise ToolError(f"no pending request: {rid}")
    w = get_wallet(state, req["wallet"])
    expires_at = req.get("expires_at")
    if expires_at is not None and now() > expires_at:
        req["status"] = "expired"
        req["resolved_at"] = now()
        audit(state, w["id"], "approval_expired", w["owner_id"],
              {"request_id": rid}, asset_id=SANDBOX_ASSET_ID,
              refusal_code="APPROVAL_EXPIRED")
        return {"status": "expired", "request_id": rid,
                "reason": "approval request expired before resolution "
                          "(pending TTL elapsed)"}
    if not approve:
        req["status"] = "denied"
        req["resolved_at"] = now()
        audit(state, w["id"], "approval_denied", w["owner_id"],
              {"request_id": rid})
        return {"status": "denied", "request_id": rid}
    audit(state, w["id"], "approval_granted", w["owner_id"],
          {"request_id": rid})
    rt = Runtime(state)
    intent = build_intent(
        w, req["to"], int(req["amount_base"]), req.get("memo", ""),
        desk_nonce=req.get("desk_nonce"),
        idempotency_key=req.get("idempotency_key", ""),
        approval_granted=True, desk_auth_ok=not w.get("desk"))
    dispatcher = rt.dispatcher_for(
        w, desk_auth_verifier=_auth_verifier(
            w, req.get("authorization"), req.get("idempotency_key", "")))
    res = dispatcher.dispatch(intent)
    if res.status != "executed":
        req["status"] = "execution_refused"
        req["resolved_at"] = now()
        return _rejected(state, w, intent, res)
    req["status"] = "approved"
    req["resolved_at"] = now()
    return _settle_executed(state, rt, w, intent, via_approval=rid)


def t_audit_log(state, args):
    wid = args.get("wallet_id")
    entries = [e for e in state["audit"] if not wid or e["wallet"] == wid]
    return {"entries": entries[-int(args.get("limit", 50)):], "total": len(entries)}


def t_revoke(state, args):
    w = get_wallet(state, args["wallet_id"])
    w["frozen_global"] = True
    audit(state, w["id"], "kill_switch_engaged", w["owner_id"],
          {"reason": args.get("reason", "owner revoked agent access")})
    return {"wallet_id": w["id"], "frozen": True,
            "note": "All spending stops immediately. This revocation is timestamped in the audit log — the provable 'notify' step."}


TOOLS = {
    "create_wallet": (t_create_wallet, "Create a sandbox agent wallet owned by a named human. A wallet cannot spend until its owner grants a policy. Optional desk binding (desk_id + desk_pubkey): the wallet takes the desk key's real EVM address and every spend then requires that desk key's signature.",
                      {"owner_id": {"type": "string"}, "label": {"type": "string"},
                       "desk_id": {"type": "string"}, "desk_pubkey": {"type": "string"}}, ["owner_id"]),
    "get_wallet": (t_get_wallet, "Wallet summary: balance, frozen state, spend today, active policy.",
                   {"wallet_id": {"type": "string"}}, ["wallet_id"]),
    "set_policy": (t_set_policy, "The owner grants the spending scope: per-transaction cap, daily cap, approval threshold, recipient allowlist, expiry, purpose. Returns a plain-language liability receipt. Responsibility follows this scope.",
                   {"wallet_id": {"type": "string"}, "per_tx_cap_usdc": {"type": "string"},
                    "daily_cap_usdc": {"type": "string"}, "approval_above_usdc": {"type": "string"},
                    "allowlist": {"type": "array", "items": {"type": "string"}},
                    "expires_in_days": {"type": "integer"}, "purpose": {"type": "string"}},
                   ["wallet_id", "per_tx_cap_usdc", "daily_cap_usdc"]),
    "fund_wallet": (t_fund_wallet, "SANDBOX faucet: add simulated test USDC so demos and tests can run. Not real money.",
                    {"wallet_id": {"type": "string"}, "amount_usdc": {"type": "string"}}, ["wallet_id", "amount_usdc"]),
    "check_payment": (t_check_payment, "Dry-run a payment against the policy. Returns allow / approve / deny with the reason. Agents should call this before pay.",
                      {"wallet_id": {"type": "string"}, "to": {"type": "string"}, "amount_usdc": {"type": "string"}}, ["wallet_id", "to", "amount_usdc"]),
    "pay": (t_pay, "Execute a payment if it is inside the granted scope. Outside scope: rejected, or routed to the human approval queue if it only exceeds the approval threshold. Desk-bound wallets must pass authorization {nonce, signature} from the bound desk key (v2 canonical message; v1 signatures still accepted).",
            {"wallet_id": {"type": "string"}, "to": {"type": "string"}, "amount_usdc": {"type": "string"}, "memo": {"type": "string"},
             "authorization": {"type": "object"}, "idempotency_key": {"type": "string"}}, ["wallet_id", "to", "amount_usdc"]),
    "approve_payment": (lambda s, a: t_resolve_approval(s, a, True), "Owner approves a pending payment request; it executes immediately (subject to the dispatch-time policy re-check and the pending TTL).",
                        {"request_id": {"type": "string"}}, ["request_id"]),
    "deny_payment": (lambda s, a: t_resolve_approval(s, a, False), "Owner denies a pending payment request.",
                     {"request_id": {"type": "string"}}, ["request_id"]),
    "audit_log": (t_audit_log, "The append-only audit trail: policy grants, payments, rejections, approvals, revocations. The evidence of what was authorized.",
                  {"wallet_id": {"type": "string"}, "limit": {"type": "integer"}}, []),
    "revoke": (t_revoke, "KILL SWITCH: freeze the wallet instantly. All spending stops; the revocation is timestamped in the audit log.",
               {"wallet_id": {"type": "string"}, "reason": {"type": "string"}}, ["wallet_id"]),
}


def tool_list():
    return [{"name": name,
             "description": desc,
             "inputSchema": {"type": "object", "properties": props, "required": req}}
            for name, (_, desc, props, req) in TOOLS.items()]


def handle(req: dict):
    method, rid = req.get("method", ""), req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-03-26"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "eclipse-agent-wallet", "version": "0.3.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tool_list()}}
    if method == "tools/call":
        name = req["params"]["name"]
        args = req["params"].get("arguments", {})
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            state = load_state()
            out = TOOLS[name][0](state, args)
            save_state(state)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(out, indent=1)}]}}
        except (ToolError, Refusal, StoreError) as e:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps({"error": str(e)})}], "isError": True}}
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return None  # notification


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
        except Exception as e:  # never crash the transport
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

# Eclipse Agent Wallet MCP · © 2026 Cyan Eclipse Technologies LLC · cetech.ai
