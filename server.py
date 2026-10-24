#!/usr/bin/env python3
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
No real funds, no private keys, no external calls. Production rails
(x402 / partner processors) plug in behind the same policy engine.

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

LEDGER_PATH = Path(os.environ.get(
    "AGENT_WALLET_LEDGER", Path(__file__).with_name("ledger.json")))
MICRO = 1_000_000  # USDC has 6 decimals; all amounts stored as integer micro-units


def now() -> int:
    return int(time.time())


def fmt(micro: int) -> str:
    return f"{micro / MICRO:.2f}"


def parse_amount(value) -> int:
    """Accept '12.50' or 12.5 (USDC) -> integer micro-units."""
    return int(round(float(value) * MICRO))


def load_state() -> dict:
    if LEDGER_PATH.exists():
        return json.loads(LEDGER_PATH.read_text())
    return {"wallets": {}, "audit": [], "pending": {}}


def save_state(state: dict) -> None:
    LEDGER_PATH.write_text(json.dumps(state, indent=1))


def audit(state: dict, wallet_id: str, event: str, actor: str, detail: dict) -> dict:
    entry = {"seq": len(state["audit"]) + 1, "ts": now(), "wallet": wallet_id,
             "event": event, "actor": actor, "detail": detail}
    state["audit"].append(entry)
    return entry


def get_wallet(state: dict, wallet_id: str) -> dict:
    w = state["wallets"].get(wallet_id)
    if not w:
        raise ToolError(f"unknown wallet: {wallet_id}")
    return w


def spent_today(state: dict, wallet_id: str) -> int:
    day = now() // 86400
    return sum(e["detail"]["amount_micro"] for e in state["audit"]
               if e["wallet"] == wallet_id and e["event"] == "payment_executed"
               and e["ts"] // 86400 == day)


def check_policy(state: dict, w: dict, to: str, amount: int) -> tuple:
    """Returns (decision, reason): decision in allow / approve / deny."""
    if w["frozen"]:
        return "deny", "wallet is frozen (kill switch engaged)"
    p = w.get("policy")
    if not p:
        return "deny", "no policy granted — an agent may not spend without a written scope"
    if p.get("expires_at") and now() > p["expires_at"]:
        return "deny", "policy has expired"
    if p["allowlist"] and to not in p["allowlist"]:
        return "deny", f"recipient {to} is not on the policy allowlist"
    if amount > p["per_tx_cap_micro"]:
        return "deny", f"amount {fmt(amount)} exceeds per-transaction cap {fmt(p['per_tx_cap_micro'])}"
    if spent_today(state, w["id"]) + amount > p["daily_cap_micro"]:
        return "deny", f"amount would exceed daily cap {fmt(p['daily_cap_micro'])}"
    if amount > p["approval_above_micro"]:
        return "approve", f"amount {fmt(amount)} exceeds the no-approval threshold {fmt(p['approval_above_micro'])} — human approval required"
    return "allow", "within granted scope"


class ToolError(Exception):
    pass


# --------------------------------------------------------------------- tools
def t_create_wallet(state, args):
    owner = str(args.get("owner_id", "")).strip()
    if not owner:
        raise ToolError("owner_id is required — a wallet must have a named human owner")
    wid = "wal_" + uuid.uuid4().hex[:12]
    addr = "0x" + hashlib.sha256(wid.encode()).hexdigest()[:40]
    w = {"id": wid, "owner_id": owner, "label": args.get("label", ""),
         "address": addr, "balance_micro": 0, "frozen": False, "policy": None,
         "created_at": now()}
    state["wallets"][wid] = w
    audit(state, wid, "wallet_created", owner, {"label": w["label"], "address": addr})
    return {"wallet_id": wid, "address": addr, "owner_id": owner,
            "balance_usdc": "0.00", "status": "no policy — cannot spend yet"}


def t_get_wallet(state, args):
    w = get_wallet(state, args["wallet_id"])
    return {"wallet_id": w["id"], "owner_id": w["owner_id"], "label": w["label"],
            "address": w["address"], "balance_usdc": fmt(w["balance_micro"]),
            "frozen": w["frozen"], "spent_today_usdc": fmt(spent_today(state, w["id"])),
            "policy": w["policy"]}


def t_set_policy(state, args):
    w = get_wallet(state, args["wallet_id"])
    if w["frozen"]:
        raise ToolError("wallet is frozen — revoke state cannot accept a new policy")
    policy = {
        "granted_by": w["owner_id"],
        "granted_at": now(),
        "per_tx_cap_micro": parse_amount(args["per_tx_cap_usdc"]),
        "daily_cap_micro": parse_amount(args["daily_cap_usdc"]),
        "approval_above_micro": parse_amount(args.get("approval_above_usdc", args["per_tx_cap_usdc"])),
        "allowlist": list(args.get("allowlist", [])),
        "expires_at": (now() + int(args["expires_in_days"]) * 86400) if args.get("expires_in_days") else None,
        "purpose": args.get("purpose", ""),
    }
    w["policy"] = policy
    receipt = (f"Liability receipt: {w['owner_id']} granted agent scope for wallet {w['id']}: "
               f"max {fmt(policy['per_tx_cap_micro'])} USDC per transaction, "
               f"{fmt(policy['daily_cap_micro'])} per day, approval required above "
               f"{fmt(policy['approval_above_micro'])}, recipients "
               f"{'restricted to allowlist' if policy['allowlist'] else 'unrestricted'}, "
               f"purpose: {policy['purpose'] or 'unspecified'}. Responsibility follows this scope — "
               f"nothing outside it is authorized.")
    audit(state, w["id"], "policy_granted", w["owner_id"], {"policy": policy, "receipt": receipt})
    return {"wallet_id": w["id"], "policy": policy, "liability_receipt": receipt}


def t_fund_wallet(state, args):
    w = get_wallet(state, args["wallet_id"])
    amount = parse_amount(args["amount_usdc"])
    if amount <= 0:
        raise ToolError("amount must be positive")
    w["balance_micro"] += amount
    audit(state, w["id"], "funded_sandbox", "faucet", {"amount_micro": amount})
    return {"wallet_id": w["id"], "balance_usdc": fmt(w["balance_micro"]),
            "note": "SANDBOX funds — simulated test USDC, not real money"}


def t_check_payment(state, args):
    w = get_wallet(state, args["wallet_id"])
    amount = parse_amount(args["amount_usdc"])
    decision, reason = check_policy(state, w, str(args["to"]), amount)
    return {"decision": decision, "reason": reason, "wallet_id": w["id"],
            "to": args["to"], "amount_usdc": fmt(amount),
            "balance_sufficient": w["balance_micro"] >= amount}


def t_pay(state, args):
    w = get_wallet(state, args["wallet_id"])
    to, amount = str(args["to"]), parse_amount(args["amount_usdc"])
    memo = args.get("memo", "")
    decision, reason = check_policy(state, w, to, amount)
    if decision == "deny":
        audit(state, w["id"], "payment_rejected", "agent", {"to": to, "amount_micro": amount, "reason": reason})
        return {"status": "rejected", "reason": reason}
    if w["balance_micro"] < amount:
        audit(state, w["id"], "payment_rejected", "agent", {"to": to, "amount_micro": amount, "reason": "insufficient sandbox balance"})
        return {"status": "rejected", "reason": "insufficient sandbox balance"}
    if decision == "approve":
        rid = "req_" + uuid.uuid4().hex[:10]
        state["pending"][rid] = {"wallet": w["id"], "to": to, "amount_micro": amount,
                                 "memo": memo, "requested_at": now(), "status": "pending"}
        audit(state, w["id"], "approval_requested", "agent", {"request_id": rid, "to": to, "amount_micro": amount, "reason": reason})
        return {"status": "approval_required", "request_id": rid, "reason": reason}
    w["balance_micro"] -= amount
    tx = "tx_" + uuid.uuid4().hex[:12]
    audit(state, w["id"], "payment_executed", "agent",
          {"tx": tx, "to": to, "amount_micro": amount, "memo": memo})
    return {"status": "executed", "tx": tx, "to": to, "amount_usdc": fmt(amount),
            "balance_usdc": fmt(w["balance_micro"])}


def t_resolve_approval(state, args, approve: bool):
    rid = args["request_id"]
    req = state["pending"].get(rid)
    if not req or req["status"] != "pending":
        raise ToolError(f"no pending request: {rid}")
    w = get_wallet(state, req["wallet"])
    req["status"] = "approved" if approve else "denied"
    req["resolved_at"] = now()
    audit(state, w["id"], "approval_granted" if approve else "approval_denied",
          w["owner_id"], {"request_id": rid})
    if not approve:
        return {"status": "denied", "request_id": rid}
    if w["frozen"]:
        return {"status": "rejected", "reason": "wallet frozen since request"}
    if w["balance_micro"] < req["amount_micro"]:
        return {"status": "rejected", "reason": "insufficient sandbox balance"}
    w["balance_micro"] -= req["amount_micro"]
    tx = "tx_" + uuid.uuid4().hex[:12]
    audit(state, w["id"], "payment_executed", "agent",
          {"tx": tx, "to": req["to"], "amount_micro": req["amount_micro"],
           "memo": req["memo"], "via_approval": rid})
    return {"status": "executed", "tx": tx, "amount_usdc": fmt(req["amount_micro"]),
            "balance_usdc": fmt(w["balance_micro"])}


def t_audit_log(state, args):
    wid = args.get("wallet_id")
    entries = [e for e in state["audit"] if not wid or e["wallet"] == wid]
    return {"entries": entries[-int(args.get("limit", 50)):], "total": len(entries)}


def t_revoke(state, args):
    w = get_wallet(state, args["wallet_id"])
    w["frozen"] = True
    audit(state, w["id"], "kill_switch_engaged", w["owner_id"],
          {"reason": args.get("reason", "owner revoked agent access")})
    return {"wallet_id": w["id"], "frozen": True,
            "note": "All spending stops immediately. This revocation is timestamped in the audit log — the provable 'notify' step."}


TOOLS = {
    "create_wallet": (t_create_wallet, "Create a sandbox agent wallet owned by a named human. A wallet cannot spend until its owner grants a policy.",
                      {"owner_id": {"type": "string"}, "label": {"type": "string"}}, ["owner_id"]),
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
    "pay": (t_pay, "Execute a payment if it is inside the granted scope. Outside scope: rejected, or routed to the human approval queue if it only exceeds the approval threshold.",
            {"wallet_id": {"type": "string"}, "to": {"type": "string"}, "amount_usdc": {"type": "string"}, "memo": {"type": "string"}}, ["wallet_id", "to", "amount_usdc"]),
    "approve_payment": (lambda s, a: t_resolve_approval(s, a, True), "Owner approves a pending payment request; it executes immediately.",
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
            "serverInfo": {"name": "eclipse-agent-wallet", "version": "0.1.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tool_list()}}
    if method == "tools/call":
        name = req["params"]["name"]
        args = req["params"].get("arguments", {})
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        state = load_state()
        try:
            out = TOOLS[name][0](state, args)
            save_state(state)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(out, indent=1)}]}}
        except ToolError as e:
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
