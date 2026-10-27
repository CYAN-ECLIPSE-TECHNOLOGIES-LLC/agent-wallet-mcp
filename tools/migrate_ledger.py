#!/usr/bin/env python3
"""Operator tool: migrate a v1 sandbox ledger.json to schema v2.

B1 integration step (c). This is an OPERATOR step, deliberately not
automatic: the v0.3.0 server refuses a v1-shaped ledger rather than
misreading it, and this tool performs the migration with proof.

Protocol:
  1. Hash the original file (sha256) - the byte-proof anchor.
  2. Run the pure v1->v2 codec (ledger_v2.migrate_v1_to_v2).
  3. FIELD-LEVEL LOSSLESSNESS ACCOUNTING: every key/value in the v1
     document must be accounted for in the v2 document under the
     known mapping (renames and additive nulls listed below). Any
     unmapped field - e.g. a desk-bound wallet's `desk` binding or
     `spend_nonce`, which the v2 wallet shape carries differently -
     ABORTS the migration with the discrepancy printed. Nothing is
     written on abort.
  4. Dry-run (default with --dry-run) stops after the accounting.
  5. Real run: a byte-identical rollback copy is written next to the
     ledger (<name>.v1-backup-<UTC ts>) and its hash verified equal
     to the original's; then the v2 store is written through
     DeltaStore (data + anchor), re-loaded through the anchor check,
     and the accounting is re-run against the reloaded document.

Usage:
  python3 tools/migrate_ledger.py --ledger /path/to/ledger.json [--dry-run]
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger_v2 import (SANDBOX_ASSET_ID, SANDBOX_CHAIN_ID, DeltaStore,
                       migrate_v1_to_v2, validate_store)

WALLET_KEY_MAP = {"id", "owner_id", "label", "address", "balance_micro",
                  "frozen", "policy", "created_at", "desk", "spend_nonce"}
POLICY_KEY_MAP = {"granted_by", "granted_at", "expires_at", "purpose",
                  "per_tx_cap_micro", "daily_cap_micro",
                  "approval_above_micro", "allowlist"}


def account_v1(v1: dict, v2: dict) -> list:
    """Return a list of loss discrepancies ([] == lossless)."""
    problems = []
    if set(v1) - {"wallets", "audit", "pending"}:
        problems.append(f"unmapped top-level keys: "
                        f"{sorted(set(v1) - {'wallets', 'audit', 'pending'})}")
    w1s, w2s = v1.get("wallets") or {}, v2.get("wallets") or {}
    if set(w1s) != set(w2s):
        problems.append("wallet id sets differ")
    for wid, w1 in w1s.items():
        w2 = w2s.get(wid)
        if w2 is None:
            continue
        extra = set(w1) - WALLET_KEY_MAP
        if extra:
            problems.append(f"wallet {wid}: unmapped v1 keys {sorted(extra)} "
                            "(the v2 shape does not carry them - migration "
                            "would be LOSSY for this wallet)")
        if w1.get("id") != w2.get("id") or w1.get("owner_id") != w2.get("owner_id") \
                or w1.get("label", "") != w2.get("label") \
                or w1.get("created_at") != w2.get("created_at"):
            problems.append(f"wallet {wid}: identity fields differ")
        if w2.get("addresses", {}).get(SANDBOX_CHAIN_ID) != w1.get("address"):
            problems.append(f"wallet {wid}: address not preserved")
        if w2.get("balances", {}).get(SANDBOX_ASSET_ID) != w1.get("balance_micro"):
            problems.append(f"wallet {wid}: balance not preserved")
        if bool(w2.get("frozen_global")) != bool(w1.get("frozen")):
            problems.append(f"wallet {wid}: frozen flag not preserved")
        for key in ("desk", "spend_nonce"):
            if key in w1 and w2.get(key) != w1[key]:
                problems.append(f"wallet {wid}: {key} not preserved")
        p1, p2 = w1.get("policy"), w2.get("policy")
        if (p1 is None) != (p2 is None):
            problems.append(f"wallet {wid}: policy presence differs")
        elif p1 is not None:
            if set(p1) - POLICY_KEY_MAP:
                problems.append(f"wallet {wid}: unmapped policy keys "
                                f"{sorted(set(p1) - POLICY_KEY_MAP)}")
            ap = (p2.get("assets") or {}).get(SANDBOX_ASSET_ID) or {}
            checks = [
                (p2.get("granted_by"), p1.get("granted_by"), "granted_by"),
                (p2.get("granted_at"), p1.get("granted_at"), "granted_at"),
                (p2.get("expires_at"), p1.get("expires_at"), "expires_at"),
                (p2.get("purpose", ""), p1.get("purpose", ""), "purpose"),
                (p2.get("aggregate_daily_cap_usd_micro"),
                 p1.get("daily_cap_micro"), "aggregate==daily_cap"),
                (ap.get("per_tx_cap_base"), p1.get("per_tx_cap_micro"),
                 "per_tx_cap"),
                (ap.get("daily_cap_base"), p1.get("daily_cap_micro"),
                 "daily_cap"),
                (ap.get("approval_above_base"), p1.get("approval_above_micro"),
                 "approval_above"),
                ([e.get("address") for e in ap.get("allowlist", [])],
                 list(p1.get("allowlist", [])), "allowlist addresses"),
            ]
            for got, want, name in checks:
                if got != want:
                    problems.append(f"wallet {wid}: policy {name} differs "
                                    f"({got!r} != {want!r})")
    a1, a2 = v1.get("audit") or [], v2.get("audit") or []
    if len(a1) != len(a2):
        problems.append("audit lengths differ")
    for i, (e1, e2) in enumerate(zip(a1, a2)):
        for k, v in e1.items():
            if e2.get(k) != v:
                problems.append(f"audit[{i}]: key {k!r} not preserved")
    p1s, p2s = v1.get("pending") or {}, v2.get("pending") or {}
    if set(p1s) != set(p2s):
        problems.append("pending id sets differ")
    for rid, r1 in p1s.items():
        r2 = p2s.get(rid)
        if r2 is None:
            continue
        if r2.get("amount_base") != r1.get("amount_micro"):
            problems.append(f"pending {rid}: amount not preserved")
        for k, v in r1.items():
            if k != "amount_micro" and r2.get(k) != v:
                problems.append(f"pending {rid}: key {k!r} not preserved")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    path = Path(a.ledger).expanduser()
    blob = path.read_bytes()
    sha_before = hashlib.sha256(blob).hexdigest()
    v1 = json.loads(blob)
    print(f"ledger: {path}")
    print(f"sha256 before: {sha_before} ({len(blob)} bytes)")
    if isinstance(v1, dict) and v1.get("schema_version") == 2:
        print("already schema v2 - nothing to do")
        return 0

    v2 = migrate_v1_to_v2(v1)
    validate_store(v2)
    problems = account_v1(v1, v2)
    if problems:
        print("LOSSLESSNESS ACCOUNTING FAILED - migration aborted, "
              "nothing written:")
        for p in problems:
            print(f"  - {p}")
        return 2
    print("losslessness accounting: PASS (every v1 field mapped; "
          "additive-only changes: audit +asset_id/+refusal_code nulls, "
          "pending amount_micro->amount_base +asset_id, policy "
          "max_fee_base=null)")
    print(f"wallets: {len(v2['wallets'])}  audit: {len(v2['audit'])}  "
          f"pending: {len(v2['pending'])}")
    if a.dry_run:
        print("dry-run: no writes made")
        return 0

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    rollback = path.with_name(path.name + f".v1-backup-{ts}")
    rollback.write_bytes(blob)
    if hashlib.sha256(rollback.read_bytes()).hexdigest() != sha_before:
        print("rollback copy hash mismatch - aborting before any write")
        return 3
    print(f"rollback copy: {rollback} (sha256 verified == original)")

    store = DeltaStore(path)
    store.save(v2)
    loaded = store.load()
    if loaded != v2:
        print("reloaded store differs from migrated document - "
              "INVESTIGATE (rollback copy is intact)")
        return 4
    problems = account_v1(v1, loaded)
    if problems:
        print("post-write accounting FAILED:", problems)
        return 5
    sha_after = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"sha256 after:  {sha_after} (canonical v2 form; the byte "
          "change is the schema change itself - the field accounting "
          "above is the losslessness proof, the rollback copy is the "
          "byte-identical original)")
    print(f"anchor: {store.anchor_path}")
    print("migration complete; post-write reload + accounting PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Eclipse Agent Wallet MCP · © 2026 Cyan Eclipse Technologies LLC · cetech.ai
