"""Policy engine v2 for Agent Wallet B0 (LOCAL/SANDBOX ONLY).

Extracted from the transport so it can be benched without MCP. Per-asset
caps in native integer base units (oracle-free) + one USD-normalized
aggregate daily cap (the anti-structuring control). Typed refusals only:
every denial carries a RefusalCode. Frozen states are first-class: global
freeze, per-asset freeze, and issuer-frozen balances (never spendable).
Depeg bands per asset: warn at the registry warn band -> forced human
approval; at/over the refuse band -> DEPEG_BAND_EXCEEDED.

BTC invariant R/P-1 lives here at grant time: a policy naming a
reserve-role asset, or a reserve address as an allowlist recipient, is
invalid at grant time; a payment attempt in a reserve asset returns
ASSET_RESERVE_ONLY.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from amounts import usd_value_micro
from oracle import Oracle, OracleError


class RefusalCode(str, Enum):
    POLICY_FROZEN = "POLICY_FROZEN"
    POLICY_FROZEN_ASSET = "POLICY_FROZEN_ASSET"
    NO_POLICY = "NO_POLICY"
    POLICY_EXPIRED = "POLICY_EXPIRED"
    ASSET_NOT_ENABLED = "ASSET_NOT_ENABLED"
    ASSET_RESERVE_ONLY = "ASSET_RESERVE_ONLY"
    NOT_ALLOWLISTED = "NOT_ALLOWLISTED"
    ADDRESS_INVALID_FOR_FAMILY = "ADDRESS_INVALID_FOR_FAMILY"
    OVER_PER_TX_CAP = "OVER_PER_TX_CAP"
    OVER_DAILY_CAP = "OVER_DAILY_CAP"
    OVER_USD_DAILY_CAP = "OVER_USD_DAILY_CAP"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    BALANCE_ISSUER_FROZEN = "BALANCE_ISSUER_FROZEN"
    INSUFFICIENT_GAS_BALANCE = "INSUFFICIENT_GAS_BALANCE"
    INSUFFICIENT_CHANNEL_LIQUIDITY = "INSUFFICIENT_CHANNEL_LIQUIDITY"
    ORACLE_UNAVAILABLE = "ORACLE_UNAVAILABLE"
    ORACLE_DIVERGENCE = "ORACLE_DIVERGENCE"
    PRICE_STALE = "PRICE_STALE"
    PRICE_MOVED = "PRICE_MOVED"
    DEPEG_BAND_EXCEEDED = "DEPEG_BAND_EXCEEDED"
    FEE_OVER_CAP = "FEE_OVER_CAP"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"
    INVOICE_EXPIRED = "INVOICE_EXPIRED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    DESK_AUTH_INVALID = "DESK_AUTH_INVALID"
    DESK_AUTH_NONCE = "DESK_AUTH_NONCE"
    UTXO_UNAVAILABLE = "UTXO_UNAVAILABLE"
    CHAIN_UNHEALTHY = "CHAIN_UNHEALTHY"
    FINALITY_REORGED = "FINALITY_REORGED"
    AMOUNT_INVALID = "AMOUNT_INVALID"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class Refusal(Exception):
    """A typed policy refusal. Carries its code into receipts/audit."""

    def __init__(self, code: RefusalCode, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class AllowlistEntry:
    family: str
    chain_id: str
    address: str
    label: str = ""


@dataclass
class AssetPolicy:
    per_tx_cap_base: int
    daily_cap_base: int
    approval_above_base: int
    allowlist: list = field(default_factory=list)  # list[AllowlistEntry]
    max_fee_base: int | None = None  # per-pay fee ceiling, in fee asset units


@dataclass
class PolicyV2:
    wallet_id: str
    granted_by: str
    granted_at: int
    expires_at: int | None
    purpose: str
    assets: dict  # asset_id -> AssetPolicy
    aggregate_daily_cap_usd_micro: int
    frozen_global: bool = False
    frozen_assets: set = field(default_factory=set)

    def policy_hash(self) -> str:
        doc = {
            "wallet_id": self.wallet_id, "granted_by": self.granted_by,
            "granted_at": self.granted_at, "expires_at": self.expires_at,
            "purpose": self.purpose,
            "aggregate_daily_cap_usd_micro": self.aggregate_daily_cap_usd_micro,
            "assets": {aid: {
                "per_tx_cap_base": ap.per_tx_cap_base,
                "daily_cap_base": ap.daily_cap_base,
                "approval_above_base": ap.approval_above_base,
                "max_fee_base": ap.max_fee_base,
                "allowlist": [{"family": e.family, "chain_id": e.chain_id,
                               "address": e.address, "label": e.label}
                              for e in ap.allowlist]}
                for aid, ap in sorted(self.assets.items())},
        }
        canon = json.dumps(doc, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()


@dataclass
class Verdict:
    decision: str  # allow | approve | deny
    reason: str
    code: RefusalCode | None = None
    usd_value_micro: int | None = None
    price_unverified: bool = False
    oracle_ref: dict | None = None
    fee_quote: object | None = None
    depeg_bps: int | None = None


def _require_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Refusal(RefusalCode.AMOUNT_INVALID,
                      f"{name} must be an int of base units")
    if value < 0:
        raise Refusal(RefusalCode.AMOUNT_INVALID, f"{name} must not be negative")
    return value


def validate_grant(registry, adapters: dict, wallet_id: str, granted_by: str,
                   asset_policies: dict, aggregate_daily_cap_usd_micro: int,
                   expires_at=None, purpose: str = "",
                   granted_at: int | None = None) -> PolicyV2:
    """Build a PolicyV2, validating everything AT GRANT TIME: assets exist
    and are spend-enabled, reserve assets/addresses are refused (R/P-1),
    caps are non-negative ints, allowlist entries are typed and valid for
    their family (via the family's adapter)."""
    _require_int(aggregate_daily_cap_usd_micro, "aggregate_daily_cap_usd_micro")
    if not isinstance(asset_policies, dict) or not asset_policies:
        raise Refusal(RefusalCode.NO_POLICY,
                      "a grant must name at least one asset policy")
    reserve_addresses = registry.reserve_addresses
    built: dict[str, AssetPolicy] = {}
    for asset_id, spec in asset_policies.items():
        if not isinstance(spec, dict):
            raise Refusal(RefusalCode.AMOUNT_INVALID,
                          f"asset policy for {asset_id!r} must be an object")
        row = registry.get(asset_id)
        if row is None:
            raise Refusal(RefusalCode.ASSET_NOT_ENABLED,
                          f"unknown asset_id in grant: {asset_id!r}")
        if row["role"] == "reserve" or row["asset_class"] == "reserve":
            raise Refusal(RefusalCode.ASSET_RESERVE_ONLY,
                          f"{asset_id} is a reserve-role asset (R/P-1): "
                          "no payment policy may name it")
        if not row["spend_enabled"]:
            raise Refusal(RefusalCode.ASSET_NOT_ENABLED,
                          f"{asset_id} is not spend-enabled in this scope "
                          f"(tier {row['slate_tier']}, status "
                          f"{row['implementation_status']})")
        per_tx = _require_int(spec.get("per_tx_cap_base"), "per_tx_cap_base")
        daily = _require_int(spec.get("daily_cap_base"), "daily_cap_base")
        approval = _require_int(spec.get("approval_above_base"),
                                "approval_above_base")
        max_fee = spec.get("max_fee_base")
        if max_fee is not None:
            max_fee = _require_int(max_fee, "max_fee_base")
        entries = []
        for raw in spec.get("allowlist", []):
            if not isinstance(raw, dict):
                raise Refusal(RefusalCode.ADDRESS_INVALID_FOR_FAMILY,
                              "allowlist entries must be typed objects")
            entry = AllowlistEntry(family=str(raw.get("family", "")),
                                   chain_id=str(raw.get("chain_id", "")),
                                   address=str(raw.get("address", "")),
                                   label=str(raw.get("label", "")))
            if entry.address in reserve_addresses:
                raise Refusal(RefusalCode.ASSET_RESERVE_ONLY,
                              "allowlist names a reserve address (R/P-1): "
                              "invalid at grant time")
            if entry.family != row["family"] or entry.chain_id != row["chain_id"]:
                raise Refusal(RefusalCode.ADDRESS_INVALID_FOR_FAMILY,
                              "allowlist entry family/chain does not match "
                              "the asset row")
            adapter = adapters.get(row["family"])
            if adapter is None or not adapter.validateAddress(entry.address):
                raise Refusal(RefusalCode.ADDRESS_INVALID_FOR_FAMILY,
                              f"allowlist address {entry.address!r} is not "
                              f"valid for family {entry.family}")
            entries.append(entry)
        built[asset_id] = AssetPolicy(per_tx, daily, approval, entries, max_fee)
    return PolicyV2(wallet_id=wallet_id, granted_by=granted_by,
                    granted_at=int(time.time()) if granted_at is None else granted_at,
                    expires_at=expires_at, purpose=purpose, assets=built,
                    aggregate_daily_cap_usd_micro=aggregate_daily_cap_usd_micro)


def evaluate_payment(*, registry, adapters: dict, oracle: Oracle | None,
                     policy: PolicyV2 | None, wallet_id: str,
                     from_address: str, asset_id: str, to: str,
                     amount_base, spent_today_base: int,
                     spent_today_usd_micro: int, clock=None,
                     fee_quote=None) -> Verdict:
    """The full ordered check used at intent time AND re-run in full at
    dispatch time (F4 fix). Returns a Verdict; never mutates state.

    Order: frozen (global, asset) -> reserve (R/P-1) -> policy present /
    unexpired -> asset enabled -> address valid -> allowlist -> per-asset
    caps -> oracle/depeg + USD aggregate -> balance (frozen-aware) ->
    fee quote + gas balance -> adapter health -> approval threshold.
    """
    now = int((clock or (lambda: int(time.time())))())
    amount = amount_base
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        return Verdict("deny", "amount_base must be a positive int of base units",
                       RefusalCode.AMOUNT_INVALID)
    row = registry.get(asset_id)
    if row is None:
        return Verdict("deny", f"unknown asset_id: {asset_id!r}",
                       RefusalCode.ASSET_NOT_ENABLED)
    if policy is not None and policy.frozen_global:
        return Verdict("deny", "wallet is frozen (kill switch engaged)",
                       RefusalCode.POLICY_FROZEN)
    if policy is not None and asset_id in policy.frozen_assets:
        return Verdict("deny", f"asset {asset_id} is frozen",
                       RefusalCode.POLICY_FROZEN_ASSET)
    if row["role"] == "reserve" or row["asset_class"] == "reserve":
        return Verdict("deny", f"{asset_id} is reserve-only (R/P-1): "
                       "no pay path exists", RefusalCode.ASSET_RESERVE_ONLY)
    if to in registry.reserve_addresses:
        return Verdict("deny", "recipient is a registered reserve address "
                       "(R/P-1)", RefusalCode.ASSET_RESERVE_ONLY)
    if policy is None:
        return Verdict("deny", "no policy granted - an agent may not spend "
                       "without a written scope", RefusalCode.NO_POLICY)
    if policy.expires_at is not None and now > policy.expires_at:
        return Verdict("deny", "policy has expired", RefusalCode.POLICY_EXPIRED)
    ap = policy.assets.get(asset_id)
    if ap is None:
        return Verdict("deny", f"policy does not cover asset {asset_id}",
                       RefusalCode.ASSET_NOT_ENABLED)
    if not row["spend_enabled"]:
        return Verdict("deny", f"{asset_id} is not spend-enabled in this scope",
                       RefusalCode.ASSET_NOT_ENABLED)
    adapter = adapters.get(row["family"])
    if adapter is None:
        return Verdict("deny", f"no adapter for family {row['family']}",
                       RefusalCode.ASSET_NOT_ENABLED)
    if adapter.chain_id != row["chain_id"]:
        return Verdict("deny", "adapter chain does not match asset row",
                       RefusalCode.ASSET_NOT_ENABLED)
    if not adapter.validateAddress(to):
        return Verdict("deny", f"recipient {to!r} is not a valid "
                       f"{row['family']} address",
                       RefusalCode.ADDRESS_INVALID_FOR_FAMILY)
    if ap.allowlist and to not in {e.address for e in ap.allowlist}:
        return Verdict("deny", f"recipient {to} is not on the policy allowlist",
                       RefusalCode.NOT_ALLOWLISTED)
    if amount > ap.per_tx_cap_base:
        return Verdict("deny", "amount exceeds per-transaction cap",
                       RefusalCode.OVER_PER_TX_CAP)
    if spent_today_base + amount > ap.daily_cap_base:
        return Verdict("deny", "amount would exceed the per-asset daily cap",
                       RefusalCode.OVER_DAILY_CAP)

    # ---- oracle: depeg band + USD valuation for the aggregate cap
    usd_value = None
    price_unverified = False
    oracle_ref = None
    depeg_bps = None
    forced_approval = False
    peg = row["peg"]
    if oracle is None:
        if peg is None:
            return Verdict("deny", "no oracle: volatile asset cannot be "
                           "valued", RefusalCode.ORACLE_UNAVAILABLE)
        price_unverified = True
    else:
        quote_ccy = peg["target"] if peg else "USD"
        try:
            snap = oracle.get_price(asset_id, quote_ccy)
            price = snap.price_decimal()
        except OracleError as e:
            code = {"PRICE_STALE": RefusalCode.PRICE_STALE,
                    "ORACLE_DIVERGENCE": RefusalCode.ORACLE_DIVERGENCE}.get(
                        e.code, RefusalCode.ORACLE_UNAVAILABLE)
            if peg is None:
                return Verdict("deny", f"oracle failure for volatile asset: "
                               f"{e}", code)
            # Pegged asset, oracle down: per-asset caps are unit-exact and
            # may proceed, but the aggregate check degrades - see below.
            price_unverified = True
            snap = None
            price = None
        if snap is not None:
            oracle_ref = {"source": snap.source, "quote": snap.quote,
                          "price": snap.price, "observed_at": snap.observed_at}
            if peg is not None:
                deviation = abs(price - Decimal(1)) * Decimal(10000)
                depeg_bps = int(deviation)
                if depeg_bps >= peg["depeg_refuse_bps"]:
                    return Verdict("deny",
                                   f"price is {depeg_bps} bps off its "
                                   f"{peg['target']} peg (refuse band "
                                   f"{peg['depeg_refuse_bps']})",
                                   RefusalCode.DEPEG_BAND_EXCEEDED,
                                   depeg_bps=depeg_bps, oracle_ref=oracle_ref)
                if depeg_bps >= peg["depeg_warn_bps"]:
                    forced_approval = True
            if quote_ccy == "USD":
                usd_value = usd_value_micro(amount, row["decimals"], price)
            else:
                # FX-pegged asset: USD value needs the USD quote as well.
                try:
                    usd_snap = oracle.get_price(asset_id, "USD")
                    usd_value = usd_value_micro(amount, row["decimals"],
                                                usd_snap.price_decimal())
                    oracle_ref["usd_price"] = usd_snap.price
                    oracle_ref["usd_source"] = usd_snap.source
                except OracleError:
                    price_unverified = True
    if usd_value is not None:
        if spent_today_usd_micro + usd_value > policy.aggregate_daily_cap_usd_micro:
            return Verdict("deny", "amount would exceed the USD-normalized "
                           "aggregate daily cap", RefusalCode.OVER_USD_DAILY_CAP,
                           usd_value_micro=usd_value,
                           price_unverified=price_unverified,
                           oracle_ref=oracle_ref, depeg_bps=depeg_bps)
    elif price_unverified and amount > ap.approval_above_base:
        # No trustworthy USD value and the amount is non-trivial: the
        # aggregate cap cannot be proven, so a human must approve.
        forced_approval = True

    # ---- balances: spendable / frozen / absent are different facts
    balance = adapter.readBalance(from_address, asset_id)
    if balance.state == "frozen":
        return Verdict("deny", "balance is issuer-frozen and never spendable",
                       RefusalCode.BALANCE_ISSUER_FROZEN,
                       usd_value_micro=usd_value, oracle_ref=oracle_ref)
    if balance.state == "absent" or balance.amount_base < amount:
        return Verdict("deny", "insufficient spendable balance",
                       RefusalCode.INSUFFICIENT_BALANCE,
                       usd_value_micro=usd_value, oracle_ref=oracle_ref)

    # ---- fee quote + gas balance
    quote = fee_quote if fee_quote is not None else adapter.quoteFee(
        {"intent_id": "", "asset_id": asset_id, "to": to,
         "amount_base": amount, "from_address": from_address})
    if quote.is_expired(now):
        return Verdict("deny", "fee quote has expired",
                       RefusalCode.QUOTE_EXPIRED, usd_value_micro=usd_value,
                       oracle_ref=oracle_ref)
    if ap.max_fee_base is not None and quote.amount_base > ap.max_fee_base:
        return Verdict("deny", "fee quote exceeds the policy fee cap",
                       RefusalCode.FEE_OVER_CAP, usd_value_micro=usd_value,
                       oracle_ref=oracle_ref, fee_quote=quote)
    if quote.amount_base > 0:
        fee_balance = adapter.readBalance(from_address, quote.fee_asset_id)
        if quote.fee_asset_id == asset_id:
            fee_balance_amount = balance.amount_base - amount
            fee_ok = fee_balance.state == "spendable" and \
                fee_balance_amount >= quote.amount_base
        else:
            fee_ok = fee_balance.state == "spendable" and \
                fee_balance.amount_base >= quote.amount_base
        if not fee_ok:
            return Verdict("deny", "insufficient gas/fee balance",
                           RefusalCode.INSUFFICIENT_GAS_BALANCE,
                           usd_value_micro=usd_value, oracle_ref=oracle_ref,
                           fee_quote=quote)

    health = adapter.health()
    if not health.get("ok"):
        return Verdict("deny", f"adapter for {row['family']} is unhealthy",
                       RefusalCode.CHAIN_UNHEALTHY, usd_value_micro=usd_value,
                       oracle_ref=oracle_ref, fee_quote=quote)

    if forced_approval:
        return Verdict("approve", "depeg warning band or unverified price: "
                       "human approval required", None,
                       usd_value_micro=usd_value,
                       price_unverified=price_unverified,
                       oracle_ref=oracle_ref, fee_quote=quote,
                       depeg_bps=depeg_bps)
    if amount > ap.approval_above_base:
        return Verdict("approve", "amount exceeds the no-approval threshold - "
                       "human approval required", None,
                       usd_value_micro=usd_value,
                       price_unverified=price_unverified,
                       oracle_ref=oracle_ref, fee_quote=quote,
                       depeg_bps=depeg_bps)
    return Verdict("allow", "within granted scope", None,
                   usd_value_micro=usd_value,
                   price_unverified=price_unverified, oracle_ref=oracle_ref,
                   fee_quote=quote, depeg_bps=depeg_bps)
