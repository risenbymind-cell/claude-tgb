"""Strategy interface.

A strategy is a pure function of market state: it never places orders, never
touches the database, and never knows which user it is running for. That keeps
the trading logic testable in isolation and makes it safe to run the same
strategy for many users with different risk settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..kalshi.orderbook import OrderBook


@dataclass(frozen=True)
class MarketContext:
    """Everything a strategy is allowed to see about one market."""

    coin: str
    ticker: str
    book: OrderBook
    seconds_to_close: float
    window_seconds: float
    # Fair-value change over the trailing N seconds, in deci-cents. None until
    # there is enough history.
    fv_change_5s: float | None = None
    fv_change_20s: float | None = None
    fv_change_60s: float | None = None
    samples: int = 0
    spot: float | None = None
    spot_change_pct: float | None = None
    title: str = ""

    @property
    def elapsed_fraction(self) -> float:
        if self.window_seconds <= 0:
            return 0.0
        return max(0.0, min(1.0, 1 - (self.seconds_to_close / self.window_seconds)))


@dataclass(frozen=True)
class Signal:
    """A decision to buy one side of a market.

    `side` is the Kalshi contract side to buy. "yes" is a bet the market
    resolves up; "no" is a bet it resolves down.
    """

    coin: str
    ticker: str
    side: str
    confidence: float
    price_dc: int  # the ask we would pay, in deci-cents
    reason: str
    detail: dict[str, float] = field(default_factory=dict)

    @property
    def direction(self) -> str:
        return "UP" if self.side == "yes" else "DOWN"


class Strategy(Protocol):
    name: str
    description: str

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        """Return a signal, or None when the setup does not qualify."""
        ...
