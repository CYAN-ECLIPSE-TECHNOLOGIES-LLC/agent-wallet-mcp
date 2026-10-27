"""Shared fixtures for the B0 suites. Temp/in-memory only."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assets import AssetRegistry  # noqa: E402
from families.base import SandboxLedger  # noqa: E402
from families.evm import EvmAdapter  # noqa: E402
from families.sandbox import SandboxAdapter  # noqa: E402
from oracle import StaticOracle  # noqa: E402
from policy import validate_grant  # noqa: E402

SANDBOX_ASSET = "sandbox:local/token:USDC"
USDC_BASE = "eip155:8453/erc20:0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_ETH = "eip155:1/erc20:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
BTC_ASSET = "bip122:000000000019d6689c085ae165831e93/satoshi"
ALICE = "sandbox:alice"
BOB = "sandbox:bob"
WALLET = "wal_test000001"


class Clock:
    def __init__(self, t=None):
        self.t = int(time.time()) if t is None else t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make_world(clock=None, fund=10_000_000, price="1.0"):
    """Registry + sandbox adapter + oracle + a granted sandbox policy."""
    clock = clock or Clock()
    registry = AssetRegistry()
    ledger = SandboxLedger()
    sandbox = SandboxAdapter(ledger, clock=clock)
    adapters = {"sandbox": sandbox}
    oracle = StaticOracle({}, clock=clock)
    oracle.set(SANDBOX_ASSET, "USD", price, source="fixture")
    policy = validate_grant(
        registry, adapters, WALLET, "owner-1",
        {SANDBOX_ASSET: {"per_tx_cap_base": 5_000_000,
                         "daily_cap_base": 8_000_000,
                         "approval_above_base": 4_000_000,
                         "allowlist": []}},
        aggregate_daily_cap_usd_micro=8_000_000, granted_at=clock())
    if fund:
        ledger.credit(ALICE, SANDBOX_ASSET, fund)
    return registry, adapters, oracle, policy, ledger, clock


def make_evm_adapter(registry=None, chain_id="eip155:8453", clock=None):
    registry = registry or AssetRegistry()
    return EvmAdapter(registry, chain_id, clock=clock or Clock())
