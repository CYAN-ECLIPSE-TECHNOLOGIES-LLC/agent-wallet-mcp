"""Receipt v2 for Agent Wallet B0 (LOCAL/SANDBOX ONLY).

A receipt binds: asset_id, chain_id, amount_base_units (int), fee,
policy_hash, policy_verdict, refusal_code, oracle references/values (or a
price_unverified flag), and the finality state. Identity fields
agent_id / principal_commitment / mandate_id are reserved and null in B0
(populated by the identity track, I-phases). The hash is SHA-256 over the
canonical JSON of the receipt without the hash field; receipts are
marked provisional vs FINAL. No float or bool may appear in amount
fields - build_receipt validates recursively.
"""
from __future__ import annotations

import hashlib
import json
import time

from amounts import format_amount

RESERVED_IDENTITY_FIELDS = ("agent_id", "principal_commitment", "mandate_id")


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _assert_no_float(obj, path="$"):
    if isinstance(obj, float):
        raise ValueError(f"float found in receipt at {path}: {obj!r}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _assert_no_float(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _assert_no_float(v, f"{path}[{i}]")


def receipt_hash(receipt: dict) -> str:
    body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    return hashlib.sha256(canonical_json(body).encode()).hexdigest()


def verify_receipt_hash(receipt: dict) -> bool:
    return isinstance(receipt.get("receipt_hash"), str) and \
        receipt["receipt_hash"] == receipt_hash(receipt)


def build_receipt(*, payment_id: str, intent_id: str, wallet_id: str,
                  asset_row: dict, amount_base: int, fee_quote=None,
                  verdict=None, policy=None, tx_ref: str | None = None,
                  finality_state: str = "QUEUED",
                  refusal_code=None, desk_auth_nonce: int | None = None,
                  provisional: bool = True, created_at: int | None = None,
                  agent_id=None, principal_commitment=None,
                  mandate_id=None) -> dict:
    if isinstance(amount_base, bool) or not isinstance(amount_base, int) \
            or amount_base < 0:
        raise ValueError("amount_base must be a non-negative int of base units")
    fee = None
    if fee_quote is not None:
        fee = {"amount_base": fee_quote.amount_base,
               "fee_asset_id": fee_quote.fee_asset_id,
               "quoted_at": fee_quote.quoted_at,
               "expires_at": fee_quote.expires_at}
    receipt = {
        "receipt_version": 2,
        "scope": "LOCAL/SANDBOX",
        "payment_id": payment_id,
        "intent_id": intent_id,
        "wallet_id": wallet_id,
        "asset_id": asset_row["asset_id"],
        "chain_id": asset_row["chain_id"],
        "amount_base_units": amount_base,
        "amount_display": format_amount(amount_base, asset_row["decimals"]),
        "decimals": asset_row["decimals"],
        "fee": fee,
        "policy_hash": policy.policy_hash() if policy is not None else None,
        "policy_verdict": (verdict.decision if verdict is not None else None),
        "refusal_code": (refusal_code.value if refusal_code is not None
                         else (verdict.code.value
                               if verdict is not None and verdict.code
                               else None)),
        "usd_value_micro": (verdict.usd_value_micro
                            if verdict is not None else None),
        "oracle_ref": (verdict.oracle_ref if verdict is not None else None),
        "price_unverified": bool(verdict.price_unverified)
        if verdict is not None else False,
        "depeg_bps": (verdict.depeg_bps if verdict is not None else None),
        "tx_ref": tx_ref,
        "finality": {"state": finality_state,
                     "provisional": bool(provisional)},
        "desk_auth_nonce": desk_auth_nonce,
        # Identity binding fields: reserved in B0, populated by I-phases.
        "agent_id": agent_id,
        "principal_commitment": principal_commitment,
        "mandate_id": mandate_id,
        "created_at": int(time.time()) if created_at is None else created_at,
    }
    _assert_no_float(receipt)
    receipt["receipt_hash"] = receipt_hash(receipt)
    return receipt
