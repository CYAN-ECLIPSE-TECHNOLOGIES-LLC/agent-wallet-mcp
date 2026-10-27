"""Ledger schema v2 codec + anchor-checked store (LOCAL/SANDBOX ONLY).

ledger_v2 is a PURE codec: migrate_v1_to_v2 wraps a v1 sandbox ledger
shape into v2 without touching any live file. Nothing in this module
reads or writes the repo's ledger.json.

DeltaStore is the persistent store for NEW B0 state and follows the
house 14-point delta law:
 1 stored-read-verbatim        - load returns exactly what was saved
 2 hash anchor                 - sidecar anchor holds sha256 of the data
 3 set anchor                   - anchor holds counts (wallets/audit len)
 4 anchor-presence required     - load without an anchor fails closed
 5 cross-artifact commitments   - anchor commits to data hash + audit head
 6 derived indexes regenerated  - build_index() derives, never persists truth
 7 fail-closed on missing key / corruption
 8 temporal monotonicity        - audit ts must be non-decreasing
 9 whole-store validation on load
10 no tail-truncation resurrection - audit length below anchor = corruption
11 defect-gated writes          - save validates before writing anything
12 schema version pinned        - schema_version must equal 2
13 integer amounts only         - bool/float amounts rejected in validation
14 atomic-ish replace           - data written then anchor, both re-read
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

SCHEMA_VERSION = 2
SANDBOX_ASSET_ID = "sandbox:local/token:USDC"
SANDBOX_CHAIN_ID = "sandbox:local"


class StoreError(Exception):
    code = "STORE_INVALID"


class StoreCorruption(StoreError):
    code = "STORE_CORRUPTION"


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def migrate_v1_to_v2(v1: dict) -> dict:
    """Pure migration from the v1 sandbox shape. Input is not mutated."""
    if not isinstance(v1, dict):
        raise StoreError("v1 ledger must be an object")
    src = copy.deepcopy(v1)
    wallets = {}
    for wid, w in (src.get("wallets") or {}).items():
        policy = w.get("policy")
        policy_v2 = None
        if policy is not None:
            policy_v2 = {
                "granted_by": policy.get("granted_by"),
                "granted_at": policy.get("granted_at"),
                "expires_at": policy.get("expires_at"),
                "purpose": policy.get("purpose", ""),
                "aggregate_daily_cap_usd_micro": policy["daily_cap_micro"],
                "assets": {SANDBOX_ASSET_ID: {
                    "per_tx_cap_base": policy["per_tx_cap_micro"],
                    "daily_cap_base": policy["daily_cap_micro"],
                    "approval_above_base": policy["approval_above_micro"],
                    "allowlist": [{"family": "sandbox",
                                   "chain_id": SANDBOX_CHAIN_ID,
                                   "address": a, "label": ""}
                                  for a in policy.get("allowlist", [])],
                    "max_fee_base": None}},
            }
        carried = {
            "id": w["id"], "owner_id": w["owner_id"],
            "label": w.get("label", ""),
            "addresses": {SANDBOX_CHAIN_ID: w["address"]},
            "balances": {SANDBOX_ASSET_ID: w["balance_micro"]},
            "frozen_global": bool(w["frozen"]),
            "frozen_assets": [],
            "policy": policy_v2,
            "created_at": w["created_at"],
        }
        # Desk extension state (v0.2.0) is carried verbatim when
        # present - dropping it would make the migration lossy for
        # desk-bound wallets (found at B1 integration, 2026-10-27).
        for key in ("desk", "spend_nonce"):
            if key in w:
                carried[key] = w[key]
        wallets[wid] = carried
    audit = []
    for e in src.get("audit") or []:
        e2 = dict(e)
        e2.setdefault("asset_id", None)
        e2.setdefault("refusal_code", None)
        audit.append(e2)
    pending = {}
    for rid, p in (src.get("pending") or {}).items():
        p2 = dict(p)
        p2["asset_id"] = SANDBOX_ASSET_ID
        p2["amount_base"] = p2.pop("amount_micro")
        pending[rid] = p2
    return {"schema_version": SCHEMA_VERSION, "wallets": wallets,
            "audit": audit, "pending": pending}


def validate_store(state) -> None:
    """Whole-store validation. Raises StoreError on any defect."""
    if not isinstance(state, dict):
        raise StoreError("store must be an object")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise StoreError(f"schema_version must be {SCHEMA_VERSION}, got "
                         f"{state.get('schema_version')!r}")
    if not isinstance(state.get("wallets"), dict):
        raise StoreError("wallets must be an object")
    if not isinstance(state.get("audit"), list):
        raise StoreError("audit must be a list")
    if not isinstance(state.get("pending"), dict):
        raise StoreError("pending must be an object")
    for wid, w in state["wallets"].items():
        if not isinstance(w, dict) or w.get("id") != wid:
            raise StoreError(f"wallet {wid!r} malformed or id mismatch")
        for asset_id, bal in (w.get("balances") or {}).items():
            if not _is_int(bal) or bal < 0:
                raise StoreError(f"wallet {wid} balance for {asset_id} must "
                                 "be a non-negative int")
        pol = w.get("policy")
        if pol is not None:
            for asset_id, ap in (pol.get("assets") or {}).items():
                for key in ("per_tx_cap_base", "daily_cap_base",
                            "approval_above_base"):
                    if not _is_int(ap.get(key)) or ap[key] < 0:
                        raise StoreError(f"policy cap {key} for {asset_id} "
                                         "must be a non-negative int")
                peg_cap = ap.get("daily_cap_peg_micro")
                if peg_cap is not None and (not _is_int(peg_cap)
                                            or peg_cap < 0):
                    raise StoreError(f"policy cap daily_cap_peg_micro for "
                                     f"{asset_id} must be a non-negative "
                                     "int or null")
            if not _is_int(pol.get("aggregate_daily_cap_usd_micro")):
                raise StoreError("aggregate cap must be an int")
    last_ts = None
    for i, e in enumerate(state["audit"]):
        if not isinstance(e, dict) or e.get("seq") != i + 1:
            raise StoreError(f"audit seq must be contiguous from 1; entry "
                             f"{i} has seq {e.get('seq') if isinstance(e, dict) else None!r}")
        if not _is_int(e.get("ts")):
            raise StoreError("audit ts must be an int")
        if last_ts is not None and e["ts"] < last_ts:
            raise StoreError("audit timestamps must be monotonic "
                             "non-decreasing")
        last_ts = e["ts"]


def build_index(state: dict) -> dict:
    """Derived index, regenerated on demand. Never persisted as truth."""
    validate_store(state)
    return {
        "audit_by_wallet": {wid: [e["seq"] for e in state["audit"]
                                  if e["wallet"] == wid]
                            for wid in state["wallets"]},
        "audit_count": len(state["audit"]),
        "wallet_count": len(state["wallets"]),
    }


def _canonical(state: dict) -> str:
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


class DeltaStore:
    """Anchor-checked JSON store for B0 state (temp/local use only)."""

    def __init__(self, path):
        self.path = Path(path)
        self.anchor_path = self.path.with_suffix(self.path.suffix + ".anchor")

    def save(self, state: dict) -> dict:
        validate_store(state)  # defect-gated writes: validate BEFORE writing
        blob = _canonical(state)
        digest = hashlib.sha256(blob.encode()).hexdigest()
        anchor = {"sha256": digest, "wallet_count": len(state["wallets"]),
                  "audit_len": len(state["audit"]),
                  "audit_head_seq": state["audit"][-1]["seq"]
                  if state["audit"] else 0,
                  "schema_version": SCHEMA_VERSION}
        self.path.write_text(blob)
        self.anchor_path.write_text(json.dumps(anchor, sort_keys=True))
        # Read-back verification of both artifacts.
        if self.path.read_text() != blob:
            raise StoreCorruption("data file read-back mismatch after save")
        if json.loads(self.anchor_path.read_text()) != anchor:
            raise StoreCorruption("anchor read-back mismatch after save")
        return anchor

    def load(self) -> dict:
        if not self.path.exists():
            raise StoreCorruption(f"store file missing: {self.path}")
        if not self.anchor_path.exists():
            raise StoreCorruption("anchor missing: refusing to load an "
                                  "unanchored store")
        try:
            anchor = json.loads(self.anchor_path.read_text())
            blob = self.path.read_text()
            state = json.loads(blob)
        except (json.JSONDecodeError, ValueError) as e:
            raise StoreCorruption(f"unparseable store or anchor: {e}")
        if hashlib.sha256(blob.encode()).hexdigest() != anchor.get("sha256"):
            raise StoreCorruption("data hash does not match anchor")
        validate_store(state)
        if len(state["wallets"]) != anchor.get("wallet_count"):
            raise StoreCorruption("wallet count does not match anchor")
        if len(state["audit"]) != anchor.get("audit_len"):
            raise StoreCorruption("audit length does not match anchor "
                                  "(possible tail truncation)")
        head = state["audit"][-1]["seq"] if state["audit"] else 0
        if head != anchor.get("audit_head_seq"):
            raise StoreCorruption("audit head does not match anchor")
        return state
