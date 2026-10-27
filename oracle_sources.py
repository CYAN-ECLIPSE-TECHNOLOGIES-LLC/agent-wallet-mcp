"""Real price sources for the B1 testnet cell (oracle.py's
TwoSourceOracle inputs).

Two INDEPENDENT public price sources for USDC/USD and ETH/USD:
- Coinbase public spot-price API (exchange source), and
- CoinGecko public simple/price API (aggregator source).
Neither requires credentials. Both are testnet-appropriate: the cell
values testnet assets at their mainnet peg/market references (testnet
USDC is a claim on the USDC/USD price, testnet ETH gas is budgeted
from ETH/USD) - prices are Decimal strings end to end, never float
(json is parsed with parse_float=Decimal).

Policy-layer staleness (C-1) stays ON: TwoSourceOracle enforces its
own 300 s bound and policy._fresh_or_raise re-checks every snapshot
at the default 300 s regardless of source.
"""
from __future__ import annotations

import json
import time
import urllib.request
from decimal import Decimal

from oracle import Oracle, OracleError, PriceSnapshot

COINBASE_SPOT = "https://api.coinbase.com/v2/prices/{symbol}-USD/spot"
COINGECKO_SIMPLE = ("https://api.coingecko.com/api/v3/simple/price"
                    "?ids={gid}&vs_currencies=usd")
_COINGECKO_IDS = {"USDC": "usd-coin", "ETH": "ethereum",
                  "EURC": "euro-coin"}


def _default_fetch(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": "agent-wallet-testnet/0.3",
                      "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp, parse_float=Decimal)


class _HttpSource(Oracle):
    source_name = "http"

    def __init__(self, symbols: dict, clock=None, fetch=None):
        # symbols: {asset_id: "USDC"|"ETH"|...}
        self._symbols = dict(symbols)
        self._clock = clock or (lambda: int(time.time()))
        self._fetch = fetch or _default_fetch

    def _symbol(self, asset_id: str, quote: str) -> str:
        if quote != "USD":
            raise OracleError("ORACLE_UNAVAILABLE",
                              f"{self.source_name}: only USD quotes")
        sym = self._symbols.get(asset_id)
        if sym is None:
            raise OracleError("ORACLE_UNAVAILABLE",
                              f"{self.source_name}: no symbol mapping "
                              f"for {asset_id}")
        return sym


class CoinbasePriceSource(_HttpSource):
    """Coinbase exchange spot price (independent source A)."""
    source_name = "coinbase"

    def get_price(self, asset_id: str, quote: str = "USD") -> PriceSnapshot:
        sym = self._symbol(asset_id, quote)
        try:
            out = self._fetch(COINBASE_SPOT.format(symbol=sym))
            price = out["data"]["amount"]
        except OracleError:
            raise
        except Exception as e:
            raise OracleError("ORACLE_UNAVAILABLE",
                              f"coinbase price fetch failed: {e}")
        return PriceSnapshot(asset_id, quote, str(price), self.source_name,
                             self._clock())


class CoinGeckoPriceSource(_HttpSource):
    """CoinGecko aggregator price (independent source B)."""
    source_name = "coingecko"

    def get_price(self, asset_id: str, quote: str = "USD") -> PriceSnapshot:
        sym = self._symbol(asset_id, quote)
        gid = _COINGECKO_IDS.get(sym)
        if gid is None:
            raise OracleError("ORACLE_UNAVAILABLE",
                              f"coingecko: no id for symbol {sym}")
        try:
            out = self._fetch(COINGECKO_SIMPLE.format(gid=gid))
            price = out[gid]["usd"]
        except OracleError:
            raise
        except Exception as e:
            raise OracleError("ORACLE_UNAVAILABLE",
                              f"coingecko price fetch failed: {e}")
        return PriceSnapshot(asset_id, quote, str(price), self.source_name,
                             self._clock())


def build_two_source_oracle(symbols: dict, clock=None, fetch_a=None,
                            fetch_b=None, max_staleness_s: int = 300):
    """Assemble the cell's TwoSourceOracle from the two independent
    sources. Staleness stays at the C-1 default of 300 s."""
    from oracle import TwoSourceOracle
    return TwoSourceOracle(
        CoinbasePriceSource(symbols, clock=clock, fetch=fetch_a),
        CoinGeckoPriceSource(symbols, clock=clock, fetch=fetch_b),
        max_staleness_s=max_staleness_s, clock=clock)
