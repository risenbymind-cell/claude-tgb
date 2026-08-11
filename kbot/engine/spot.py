"""Optional spot reference prices.

Purely an extra strategy input — the order-book signal stands on its own, so a
coin with no spot feed still trades normally. Failures here are logged at debug
and never propagate into the trading loop.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)

COINBASE = "https://api.exchange.coinbase.com/products/{product}/ticker"


class SpotFeed:
    def __init__(self, products: dict[str, str], interval: float = 5.0) -> None:
        self.products = products
        self.interval = interval
        self.prices: dict[str, tuple[float, float]] = {}  # coin -> (price, ts)
        self._history: dict[str, list[tuple[float, float]]] = {}
        self._task: asyncio.Task | None = None
        self._client = httpx.AsyncClient(timeout=5.0)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="spot-feed")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._client.aclose()

    def price(self, coin: str) -> float | None:
        entry = self.prices.get(coin.upper())
        if entry is None or time.time() - entry[1] > 60:
            return None
        return entry[0]

    def change_pct(self, coin: str, seconds: float = 60.0) -> float | None:
        series = self._history.get(coin.upper())
        if not series or len(series) < 2:
            return None
        cutoff = time.time() - seconds
        past = [p for t, p in series if t <= cutoff]
        baseline = past[-1] if past else series[0][1]
        if baseline == 0:
            return None
        return (series[-1][1] - baseline) / baseline * 100

    async def _loop(self) -> None:
        while True:
            await asyncio.gather(
                *(self._fetch(coin, product) for coin, product in self.products.items()),
                return_exceptions=True,
            )
            await asyncio.sleep(self.interval)

    async def _fetch(self, coin: str, product: str) -> None:
        try:
            resp = await self._client.get(COINBASE.format(product=product))
            resp.raise_for_status()
            price = float(resp.json()["price"])
        except Exception as exc:  # noqa: BLE001 - spot is best-effort
            log.debug("Spot fetch failed for %s: %s", coin, exc)
            return
        now = time.time()
        self.prices[coin.upper()] = (price, now)
        series = self._history.setdefault(coin.upper(), [])
        series.append((now, price))
        cutoff = now - 300
        while series and series[0][0] < cutoff:
            series.pop(0)
