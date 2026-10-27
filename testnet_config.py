"""Testnet build configuration for the B1 first cell (Base Sepolia).

TESTNET ONLY. This module is the ONLY place where the Base Sepolia
registry rows are flipped spend_enabled - and only in the in-memory
registry this module builds. The shipped assets.json defaults keep
every non-sandbox row spend_enabled=false (asserted by tests), so a
plain AssetRegistry() can never spend on eip155:84532.

Cell: USDC (payment) + ETH (gas) on eip155:84532 (Base Sepolia).
No mainnet chain id is accepted anywhere in this module.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from assets import REGISTRY_PATH, AssetRegistry

TESTNET_CHAIN_ID = "eip155:84532"
TESTNET_CHAIN_NUMERIC = 84532
USDC_TESTNET_ASSET_ID = ("eip155:84532/erc20:"
                         "0x036CbD53842c5426634e7929541eC2318f3dCF7e")
ETH_TESTNET_ASSET_ID = "eip155:84532/slip44:60"
TESTNET_ASSET_IDS = (USDC_TESTNET_ASSET_ID, ETH_TESTNET_ASSET_ID)

RPC_URL_ENV = "AGENT_WALLET_BASE_SEPOLIA_RPC"
TESTNET_STORE_ENV = "AGENT_WALLET_TESTNET_LEDGER"


def load_testnet_registry() -> AssetRegistry:
    """Registry for the testnet cell: shipped rows, with spend_enabled
    flipped ONLY for the two Base Sepolia rows, in memory only."""
    data = json.loads(Path(REGISTRY_PATH).read_text())
    data = copy.deepcopy(data)
    flipped = []
    for row in data["assets"]:
        if row["asset_id"] in TESTNET_ASSET_IDS:
            assert row["network"] == "testnet" and \
                row["chain_id"] == TESTNET_CHAIN_ID
            row["spend_enabled"] = True
            flipped.append(row["asset_id"])
    if sorted(flipped) != sorted(TESTNET_ASSET_IDS):
        raise ValueError(f"testnet registry rows missing: flipped={flipped}")
    return AssetRegistry(data=data,
                         testnet_spend_chains=frozenset({TESTNET_CHAIN_ID}))


def rpc_url_from_env(env=None) -> str | None:
    """RPC endpoint comes from config/env, never hard-coded in the
    adapter. Returns None when unset (callers decide fail-closed)."""
    env = os.environ if env is None else env
    return env.get(RPC_URL_ENV) or None


def testnet_store_path(env=None) -> Path:
    """Testnet state lives in its OWN store, separate from the sandbox
    ledger and the team ledger (both untouched by testnet state)."""
    env = os.environ if env is None else env
    raw = env.get(TESTNET_STORE_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).with_name("testnet-store.json")


# ---------------------------------------------------------------- builders
FUND_VAULT_DIR = Path("~/.secrets/eclipse-agent-wallet-funds")
FUND_MASTER_NAME = "master.key"


def fund_vault_paths() -> tuple[Path, Path]:
    vault = FUND_VAULT_DIR.expanduser()
    return vault, vault / FUND_MASTER_NAME


def build_testnet_oracle(registry=None, clock=None, **kw):
    """Two-source oracle for the cell (Coinbase + CoinGecko)."""
    from oracle_sources import build_two_source_oracle
    registry = registry or load_testnet_registry()
    symbols = {aid: registry.require(aid)["symbol"]
               for aid in TESTNET_ASSET_IDS}
    return build_two_source_oracle(symbols, clock=clock, **kw)


def build_testnet_adapter(registry=None, *, rpc_url=None, rpc_client=None,
                          fund_signer=None, clock=None):
    """Chain-backed EVM adapter for eip155:84532 only. RPC endpoint
    from the rpc_url argument or the AGENT_WALLET_BASE_SEPOLIA_RPC env
    var - never hard-coded."""
    from families.evm import EvmAdapter
    registry = registry or load_testnet_registry()
    return EvmAdapter(registry, TESTNET_CHAIN_ID, clock=clock,
                      chain_backed=True, rpc_url=rpc_url,
                      rpc_client=rpc_client, fund_signer=fund_signer)
