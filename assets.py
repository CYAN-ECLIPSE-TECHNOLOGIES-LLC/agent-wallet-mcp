"""Asset registry for the Agent Wallet B0 core (LOCAL/SANDBOX ONLY).

One row per asset x network, per silas-view.md S4.1. `slate_tier` (the
program slate: CORE / NEXT / WATCH) and `implementation_status` (what the
code can actually do today) are separate fields by design. The lifecycle
is three separate booleans: listed != live != used.

Validation fails closed: duplicate (symbol, chain) or asset_id, unknown
family, decimals that disagree with the canonical decimals for a symbol,
malformed contract/mint addresses, and spend-enabled rows that are not
the sandbox row all raise RegistryError.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REGISTRY_PATH = Path(__file__).with_name("assets.json")

KNOWN_FAMILIES = {"sandbox", "evm", "solana", "bitcoin", "lightning"}
KNOWN_TIERS = {"CORE", "NEXT", "WATCH"}
KNOWN_ROLES = {"payment", "gas", "reserve"}
KNOWN_ASSET_CLASSES = {"payment-stable", "native-gas", "reserve",
                       "fiat-stable-non-usd"}
# Canonical decimals per symbol; a row that disagrees fails validation.
CANONICAL_DECIMALS = {"USDC": 6, "EURC": 6, "PYUSD": 6, "USDT": 6,
                      "ETH": 18, "SOL": 9, "BTC": 8, "DAI": 18, "USDS": 18}

_EVM_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_B58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

REQUIRED_FIELDS = {
    "asset_id", "asset_group", "symbol", "display_name", "family",
    "chain_id", "network", "contract_or_mint", "decimals", "subunit",
    "transfer_method", "asset_class", "role", "volatility_class", "peg",
    "fee_asset_id", "slate_tier", "implementation_status", "listed",
    "live", "used", "live_gate", "spend_enabled", "population_required",
    "address_source", "reserve_addresses",
}


class RegistryError(ValueError):
    code = "REGISTRY_INVALID"


def _is_base58(text: str) -> bool:
    return 32 <= len(text) <= 44 and all(c in _B58_ALPHABET for c in text)


def validate_address_format(family: str, address) -> bool:
    """Format-only address validation for registry rows and grants."""
    if not isinstance(address, str) or not address:
        return False
    if family == "evm":
        return bool(_EVM_ADDR_RE.match(address))
    if family == "solana":
        return _is_base58(address)
    if family == "sandbox":
        return address.startswith("sandbox:") and len(address) > 8
    if family == "bitcoin":
        return address.startswith(("bc1", "tb1", "1", "3", "m", "n", "2"))
    return False


def validate_registry(data) -> list[dict]:
    """Validate raw registry data; returns the row list or raises."""
    if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
        raise RegistryError("registry must be an object with an assets list")
    rows = data["assets"]
    seen_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    spend_enabled_rows = []
    for row in rows:
        if not isinstance(row, dict):
            raise RegistryError("registry row must be an object")
        missing = REQUIRED_FIELDS - set(row)
        if missing:
            raise RegistryError(f"row {row.get('asset_id')!r} missing fields: "
                                f"{sorted(missing)}")
        aid = row["asset_id"]
        if not isinstance(aid, str) or not aid.startswith(row["chain_id"] + "/"):
            raise RegistryError(f"asset_id {aid!r} does not start with its "
                                f"chain_id {row['chain_id']!r}")
        if aid in seen_ids:
            raise RegistryError(f"duplicate asset_id: {aid}")
        seen_ids.add(aid)
        pair = (row["symbol"], row["chain_id"])
        if pair in seen_pairs:
            raise RegistryError(f"duplicate (asset, network) row: {pair}")
        seen_pairs.add(pair)
        if row["family"] not in KNOWN_FAMILIES:
            raise RegistryError(f"unknown family {row['family']!r} in {aid}")
        if row["slate_tier"] not in KNOWN_TIERS:
            raise RegistryError(f"unknown slate_tier {row['slate_tier']!r} in {aid}")
        if row["role"] not in KNOWN_ROLES:
            raise RegistryError(f"unknown role {row['role']!r} in {aid}")
        if row["asset_class"] not in KNOWN_ASSET_CLASSES:
            raise RegistryError(f"unknown asset_class {row['asset_class']!r} in {aid}")
        expected = CANONICAL_DECIMALS.get(row["symbol"])
        if expected is None:
            raise RegistryError(f"no canonical decimals known for {row['symbol']!r}")
        if row["decimals"] != expected or isinstance(row["decimals"], bool):
            raise RegistryError(
                f"decimals mismatch for {aid}: row says {row['decimals']}, "
                f"canonical is {expected}")
        for flag in ("listed", "live", "used", "spend_enabled",
                     "population_required"):
            if not isinstance(row[flag], bool):
                raise RegistryError(f"{flag} must be a bool in {aid}")
        contract = row["contract_or_mint"]
        if contract is not None:
            if row["population_required"]:
                raise RegistryError(f"{aid}: contract set but population "
                                    "still marked required")
            if not validate_address_format(row["family"], contract):
                raise RegistryError(f"{aid}: malformed contract/mint address "
                                    f"{contract!r}")
        elif row["transfer_method"] in ("erc20", "spl") and \
                not row["population_required"]:
            raise RegistryError(f"{aid}: token row without a contract/mint "
                                "must set population_required=true")
        peg = row["peg"]
        if peg is not None:
            if not isinstance(peg, dict) or peg.get("target") not in ("USD", "EUR"):
                raise RegistryError(f"{aid}: bad peg spec")
            if not (0 < peg["depeg_warn_bps"] < peg["depeg_refuse_bps"]):
                raise RegistryError(f"{aid}: peg bands must satisfy "
                                    "0 < warn < refuse")
        if row["role"] == "reserve" and row["spend_enabled"]:
            raise RegistryError(f"{aid}: reserve-role asset can never be "
                                "spend-enabled (R/P-1)")
        if row["spend_enabled"]:
            spend_enabled_rows.append(row)
        for addr in row["reserve_addresses"]:
            if not isinstance(addr, str) or not addr:
                raise RegistryError(f"{aid}: bad reserve address entry")
    for row in spend_enabled_rows:
        if row["family"] != "sandbox":
            raise RegistryError(
                f"{row['asset_id']}: only the sandbox row may be "
                "spend-enabled in B0")
    ids = {r["asset_id"] for r in rows}
    for row in rows:
        if row["fee_asset_id"] not in ids:
            raise RegistryError(f"{row['asset_id']}: fee_asset_id "
                                f"{row['fee_asset_id']!r} is not a registry row")
    return rows


class AssetRegistry:
    """Validated, read-only view over assets.json (or injected row data)."""

    def __init__(self, data=None, path: Path | None = None):
        if data is None:
            raw = (path or REGISTRY_PATH).read_text()
            data = json.loads(raw)
        self.rows = validate_registry(data)
        self._by_id = {r["asset_id"]: r for r in self.rows}
        self.version = data.get("registry_version")

    def get(self, asset_id: str) -> dict | None:
        return self._by_id.get(asset_id)

    def require(self, asset_id: str) -> dict:
        row = self.get(asset_id)
        if row is None:
            raise RegistryError(f"unknown asset_id: {asset_id!r}")
        return row

    def by_symbol_chain(self, symbol: str, chain_id: str) -> dict | None:
        for row in self.rows:
            if row["symbol"] == symbol and row["chain_id"] == chain_id:
                return row
        return None

    @property
    def reserve_addresses(self) -> set[str]:
        out: set[str] = set()
        for row in self.rows:
            out.update(row["reserve_addresses"])
        return out

    def spend_enabled_rows(self) -> list[dict]:
        return [r for r in self.rows if r["spend_enabled"]]
