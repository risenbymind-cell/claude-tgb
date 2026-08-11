"""Finds the live 15-minute market for each coin.

Kalshi rolls a fresh market every window, so the ticker we trade changes four
times an hour. Rather than hard-coding ticker formats — which Kalshi changes —
we list the open markets in each configured series and pick the one whose
window is ~15 minutes long and closes soonest.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..kalshi.rest import KalshiClient, KalshiError

log = logging.getLogger(__name__)

# A "15-minute" market, with tolerance for Kalshi's exact open/close stamps.
MIN_WINDOW_S = 5 * 60
MAX_WINDOW_S = 20 * 60
DEFAULT_WINDOW_S = 15 * 60


def parse_ts(value: str | int | float | None) -> float | None:
    """Parse an RFC3339 string or epoch number into epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@dataclass
class LiveMarket:
    coin: str
    ticker: str
    title: str
    open_time: float
    close_time: float

    @property
    def window_seconds(self) -> float:
        span = self.close_time - self.open_time
        return span if span > 0 else DEFAULT_WINDOW_S

    def seconds_to_close(self, now: float | None = None) -> float:
        return self.close_time - (now if now is not None else time.time())


class MarketDiscovery:
    def __init__(self, rest: KalshiClient, series: dict[str, str]) -> None:
        self.rest = rest
        self.series = series
        self.markets: dict[str, LiveMarket] = {}
        self._missing_series: set[str] = set()

    async def refresh(self, coins: list[str]) -> dict[str, LiveMarket]:
        """Refresh the current market for each requested coin."""
        wanted = [c for c in coins if c.upper() in self.series]
        results = await asyncio.gather(
            *(self._for_coin(c.upper()) for c in wanted), return_exceptions=True
        )
        found: dict[str, LiveMarket] = {}
        for coin, result in zip((c.upper() for c in wanted), results):
            if isinstance(result, Exception):
                log.debug("Discovery failed for %s: %s", coin, result)
                # Keep the previous market rather than blanking the coin on a
                # single failed request.
                if coin in self.markets:
                    found[coin] = self.markets[coin]
                continue
            if result is not None:
                found[coin] = result
        self.markets = found
        return found

    async def _for_coin(self, coin: str) -> LiveMarket | None:
        series_ticker = self.series[coin]
        try:
            data = await self.rest.get_markets(series_ticker=series_ticker, status="open")
        except KalshiError as exc:
            if exc.status == 404 and series_ticker not in self._missing_series:
                self._missing_series.add(series_ticker)
                log.warning(
                    "Series %r for %s does not exist on this environment; "
                    "set KALSHI_SERIES to the correct ticker.",
                    series_ticker,
                    coin,
                )
            raise

        now = time.time()
        candidates: list[LiveMarket] = []
        for market in data.get("markets", []):
            close_time = parse_ts(market.get("close_time"))
            open_time = parse_ts(market.get("open_time"))
            ticker = market.get("ticker")
            if not ticker or close_time is None:
                continue
            if close_time <= now:
                continue
            if open_time is None:
                open_time = close_time - DEFAULT_WINDOW_S
            span = close_time - open_time
            if not (MIN_WINDOW_S <= span <= MAX_WINDOW_S):
                continue
            candidates.append(
                LiveMarket(
                    coin=coin,
                    ticker=ticker,
                    title=market.get("title") or market.get("subtitle") or ticker,
                    open_time=open_time,
                    close_time=close_time,
                )
            )

        if not candidates:
            return None
        # The soonest close is the window currently in play.
        return min(candidates, key=lambda m: m.close_time)
