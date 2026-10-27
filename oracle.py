"""Price oracle plumbing for policy v2 (LOCAL/SANDBOX ONLY).

Policy never values an asset from a single silent feed. An injected
Oracle supplies PriceSnapshots; TwoSourceOracle requires >=2 sources and
fails loudly (typed OracleError) on missing, stale, or divergent prices.
Prices are Decimal strings end to end - never float.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class OracleError(Exception):
    """Typed oracle failure: ORACLE_UNAVAILABLE / PRICE_STALE /
    ORACLE_DIVERGENCE / ORACLE_BAD_PRICE."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PriceSnapshot:
    asset_id: str
    quote: str            # currency the price is denominated in, e.g. "USD"
    price: str            # Decimal literal, e.g. "1.0001"
    source: str
    observed_at: int      # epoch seconds

    def price_decimal(self) -> Decimal:
        try:
            d = Decimal(self.price)
        except (InvalidOperation, ValueError, TypeError):
            raise OracleError("ORACLE_BAD_PRICE",
                              f"unparseable price {self.price!r}")
        if not d.is_finite() or d < 0:
            raise OracleError("ORACLE_BAD_PRICE",
                              f"price must be finite and non-negative: {self.price!r}")
        return d


class Oracle:
    """Interface: injected into the policy engine."""

    def get_price(self, asset_id: str, quote: str = "USD") -> PriceSnapshot:
        raise NotImplementedError


class StaticOracle(Oracle):
    """Deterministic oracle for tests and sandbox use."""

    def __init__(self, prices: dict, clock=None):
        # prices: {(asset_id, quote): (price_str, observed_at, source)}
        self._prices = dict(prices)
        self._clock = clock or (lambda: int(time.time()))

    def set(self, asset_id, quote, price, observed_at=None, source="static"):
        self._prices[(asset_id, quote)] = (
            price, self._clock() if observed_at is None else observed_at, source)

    def get_price(self, asset_id: str, quote: str = "USD") -> PriceSnapshot:
        hit = self._prices.get((asset_id, quote))
        if hit is None:
            raise OracleError("ORACLE_UNAVAILABLE",
                              f"no price for {asset_id} in {quote}")
        price, observed_at, source = hit
        return PriceSnapshot(asset_id, quote, str(price), source, observed_at)


class TwoSourceOracle(Oracle):
    """Combines two independent Oracle sources. Refuses loudly when either
    source is missing/stale or the two diverge beyond `divergence_bps`."""

    def __init__(self, source_a: Oracle, source_b: Oracle,
                 max_staleness_s: int = 300, divergence_bps: int = 50,
                 clock=None):
        if source_a is source_b:
            raise ValueError("TwoSourceOracle requires two distinct sources")
        self.a, self.b = source_a, source_b
        self.max_staleness_s = max_staleness_s
        self.divergence_bps = divergence_bps
        self._clock = clock or (lambda: int(time.time()))

    def get_price(self, asset_id: str, quote: str = "USD") -> PriceSnapshot:
        snaps = []
        for src in (self.a, self.b):
            snap = src.get_price(asset_id, quote)  # may raise OracleError
            age = self._clock() - snap.observed_at
            if age < 0 or age > self.max_staleness_s:
                raise OracleError(
                    "PRICE_STALE",
                    f"price for {asset_id}/{quote} from {snap.source} is "
                    f"stale (age {age}s > {self.max_staleness_s}s)")
            snaps.append(snap)
        pa, pb = snaps[0].price_decimal(), snaps[1].price_decimal()
        if pa == 0 and pb == 0:
            return snaps[0]
        mid = (pa + pb) / 2
        if mid == 0:
            raise OracleError("ORACLE_BAD_PRICE", "zero mid price")
        divergence_bps = int(abs(pa - pb) * Decimal(10000) / mid)
        if divergence_bps > self.divergence_bps:
            raise OracleError(
                "ORACLE_DIVERGENCE",
                f"sources diverge by {divergence_bps} bps for "
                f"{asset_id}/{quote} (bound {self.divergence_bps})")
        # Return the older (more conservative) snapshot, sources recorded.
        chosen = snaps[0] if snaps[0].observed_at <= snaps[1].observed_at else snaps[1]
        return PriceSnapshot(asset_id, quote, str(chosen.price),
                             f"{snaps[0].source}+{snaps[1].source}",
                             chosen.observed_at)
