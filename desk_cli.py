#!/usr/bin/env python3
"""Desk operator console for Eclipse Agent Wallet MCP (v0.2.0 desk extension).

Drives server.py over its real MCP stdio transport. Holds the desk
custody side: envelope-encrypted desk keys live in the vault dir
($AGENT_WALLET_DESK_VAULT, default ~/.secrets/eclipse-agent-wallet-desks,
master key 0600, outside any repo). The server never sees key material;
this console signs spend authorizations with the desk key (sign-only)
and passes {nonce, signature} to the pay tool.

Run with the Anchor venv python (needs eth_account + cryptography):
  ~/workspace/tools/venv-anchor/bin/python desk_cli.py <command> ...

Ledger selection passes through $AGENT_WALLET_LEDGER to the server.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_envelope
import desk_keys
from amounts import parse_amount
from ledger_v2 import SANDBOX_ASSET_ID, SANDBOX_CHAIN_ID

HERE = Path(__file__).parent
VAULT = Path(os.environ.get("AGENT_WALLET_DESK_VAULT",
                            "~/.secrets/eclipse-agent-wallet-desks")).expanduser()
MASTER = VAULT / "master.key"


class Mcp:
    def __init__(self):
        env = dict(os.environ)
        self.p = subprocess.Popen(
            ["python3", str(HERE / "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self._id = 0
        self.call_raw("initialize", {})

    def call_raw(self, method, params):
        self._id += 1
        self.p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        return json.loads(line)

    def tool(self, name, args):
        resp = self.call_raw("tools/call", {"name": name, "arguments": args})
        result = resp["result"]
        out = json.loads(result["content"][0]["text"])
        if result.get("isError"):
            raise SystemExit(f"TOOL ERROR {name}: {out}")
        return out

    def close(self):
        self.p.terminate()


def policy_hash(policy: dict) -> str:
    return hashlib.sha256(json.dumps(
        policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cmd_issue(a):
    desk_envelope.ensure_master_key(MASTER)
    info = desk_keys.create_desk_key(VAULT, MASTER, a.desk, a.purpose)
    mcp = Mcp()
    try:
        w = mcp.tool("create_wallet", {
            "owner_id": a.owner, "label": a.label,
            "desk_id": a.desk, "desk_pubkey": info["public_key"]})
    finally:
        mcp.close()
    desk_keys.bind_wallet(VAULT, a.desk, w["wallet_id"])
    print(json.dumps({"desk": a.desk, "wallet_id": w["wallet_id"],
                      "address": w["address"], "desk_pubkey": info["public_key"],
                      "envelope": info["envelope"]}, indent=1))


def cmd_policy(a):
    mcp = Mcp()
    try:
        out = mcp.tool("set_policy", {
            "wallet_id": a.wallet, "per_tx_cap_usdc": a.per_tx,
            "daily_cap_usdc": a.daily, "approval_above_usdc": a.approval,
            "allowlist": [x.strip() for x in a.allowlist.split(",") if x.strip()],
            "expires_in_days": a.expires_days, "purpose": a.purpose})
    finally:
        mcp.close()
    out["policy_hash"] = policy_hash(out["policy"])
    print(json.dumps(out, indent=1))


def cmd_fund(a):
    mcp = Mcp()
    try:
        print(json.dumps(mcp.tool("fund_wallet",
                                  {"wallet_id": a.wallet, "amount_usdc": a.amount}), indent=1))
    finally:
        mcp.close()


def cmd_pay(a):
    mcp = Mcp()
    try:
        w = mcp.tool("get_wallet", {"wallet_id": a.wallet})
        nonce = int(w.get("spend_nonce", 0)) + 1
        # Integer base units via the core parser - no float (the v1
        # client-side float conversion is retired with the server's).
        amount_base = parse_amount(a.amount, 6)
        idem = "desk_" + uuid.uuid4().hex[:16]
        signer = desk_keys.load_desk_signer(VAULT, MASTER, a.desk)
        sig = signer.sign_spend_v2(
            wallet_id=a.wallet, asset_id=SANDBOX_ASSET_ID,
            chain_id=SANDBOX_CHAIN_ID, to=a.to, amount_base=amount_base,
            memo=a.memo, nonce=nonce, idempotency_key=idem)
        out = mcp.tool("pay", {"wallet_id": a.wallet, "to": a.to,
                               "amount_usdc": a.amount, "memo": a.memo,
                               "authorization": {"nonce": nonce,
                                                 "signature": sig,
                                                 "idempotency_key": idem}})
        out["nonce_presented"] = nonce
        print(json.dumps(out, indent=1))
    finally:
        mcp.close()


def cmd_check(a):
    mcp = Mcp()
    try:
        print(json.dumps(mcp.tool("check_payment", {
            "wallet_id": a.wallet, "to": a.to, "amount_usdc": a.amount}), indent=1))
    finally:
        mcp.close()


def cmd_revoke(a):
    mcp = Mcp()
    try:
        print(json.dumps(mcp.tool("revoke",
                                  {"wallet_id": a.wallet, "reason": a.reason}), indent=1))
    finally:
        mcp.close()


def cmd_show(a):
    mcp = Mcp()
    try:
        w = mcp.tool("get_wallet", {"wallet_id": a.wallet})
        log = mcp.tool("audit_log", {"wallet_id": a.wallet, "limit": 200})
        print(json.dumps({"wallet": w, "audit_entries": log["total"],
                          "audit": log["entries"]}, indent=1))
    finally:
        mcp.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("issue"); p.add_argument("--desk", required=True)
    p.add_argument("--owner", default="steven@cetech.ai")
    p.add_argument("--label", required=True); p.add_argument("--purpose", required=True)
    p.set_defaults(fn=cmd_issue)

    p = sub.add_parser("policy"); p.add_argument("--wallet", required=True)
    p.add_argument("--per-tx", required=True); p.add_argument("--daily", required=True)
    p.add_argument("--approval", required=True); p.add_argument("--allowlist", default="")
    p.add_argument("--expires-days", type=int, default=90)
    p.add_argument("--purpose", required=True); p.set_defaults(fn=cmd_policy)

    p = sub.add_parser("fund"); p.add_argument("--wallet", required=True)
    p.add_argument("--amount", required=True); p.set_defaults(fn=cmd_fund)

    p = sub.add_parser("pay"); p.add_argument("--desk", required=True)
    p.add_argument("--wallet", required=True); p.add_argument("--to", required=True)
    p.add_argument("--amount", required=True); p.add_argument("--memo", default="")
    p.set_defaults(fn=cmd_pay)

    p = sub.add_parser("check"); p.add_argument("--wallet", required=True)
    p.add_argument("--to", required=True); p.add_argument("--amount", required=True)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("revoke"); p.add_argument("--wallet", required=True)
    p.add_argument("--reason", default="owner revoked agent access")
    p.set_defaults(fn=cmd_revoke)

    p = sub.add_parser("show"); p.add_argument("--wallet", required=True)
    p.set_defaults(fn=cmd_show)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
