"""Live order-book feed over Kalshi's websocket, with a REST polling fallback.

A single `MarketFeed` serves every user: the 15-minute market for a given coin
is the same market for everybody, so we keep one book per ticker and let all
strategies read from it.

The websocket is the primary path — it is the only way to see the book tick by
tick. If no platform credentials are configured, or the socket keeps failing,
the feed degrades to REST snapshot polling rather than going dark.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Iterable

import websockets

from .auth import Signer
from .orderbook import OrderBook
from .rest import KalshiClient

log = logging.getLogger(__name__)


class MarketFeed:
    def __init__(
        self,
        ws_url: str,
        rest: KalshiClient,
        signer: Signer | None,
        poll_interval: float = 2.0,
    ) -> None:
        self.ws_url = ws_url
        self.rest = rest
        self.signer = signer
        self.poll_interval = poll_interval

        self.books: dict[str, OrderBook] = {}
        self._tickers: set[str] = set()
        self._dirty = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._cmd_id = 0
        self._sub_id: int | None = None
        self._connected = False

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="market-feed")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def connected(self) -> bool:
        return self._connected

    # ---------------- subscriptions ----------------

    def set_tickers(self, tickers: Iterable[str]) -> None:
        """Replace the watch list. Safe to call on every discovery pass."""
        new = set(tickers)
        if new == self._tickers:
            return
        self._tickers = new
        for ticker in new:
            self.books.setdefault(ticker, OrderBook(ticker))
        # Drop books for markets that have rolled off.
        for ticker in list(self.books):
            if ticker not in new:
                self.books.pop(ticker, None)
        self._dirty.set()

    def book(self, ticker: str) -> OrderBook | None:
        return self.books.get(ticker)

    # ---------------- internals ----------------

    def _next_id(self) -> int:
        self._cmd_id += 1
        return self._cmd_id

    async def _run(self) -> None:
        if self.signer is None:
            log.warning(
                "No platform Kalshi credentials; market data falls back to REST polling."
            )
            await self._poll_loop()
            return

        backoff = 1.0
        while True:
            try:
                await self._ws_session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self._connected = False
                log.warning("Market feed websocket dropped: %s", exc)
                # Keep books warm over the reconnect so strategies are not blind.
                poll = asyncio.create_task(self._poll_once_all())
                await asyncio.sleep(backoff)
                poll.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poll
                backoff = min(backoff * 2, 30.0)

    async def _ws_session(self) -> None:
        headers = self.signer.headers("GET", "/trade-api/ws/v2")
        async with websockets.connect(
            self.ws_url, extra_headers=headers, ping_interval=10, ping_timeout=20
        ) as ws:
            self._connected = True
            log.info("Market feed connected")
            self._dirty.set()
            resubscribe = asyncio.create_task(self._resubscribe_loop(ws))
            try:
                async for raw in ws:
                    self._handle_message(raw)
            finally:
                self._connected = False
                resubscribe.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await resubscribe

    async def _resubscribe_loop(self, ws) -> None:
        """Push the current watch list to the socket whenever it changes."""
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            tickers = sorted(self._tickers)
            if self._sub_id is not None:
                with contextlib.suppress(Exception):
                    await ws.send(
                        json.dumps(
                            {
                                "id": self._next_id(),
                                "cmd": "unsubscribe",
                                "params": {"sids": [self._sub_id]},
                            }
                        )
                    )
                self._sub_id = None
            if not tickers:
                continue
            await ws.send(
                json.dumps(
                    {
                        "id": self._next_id(),
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["orderbook_delta"],
                            "market_tickers": tickers,
                        },
                    }
                )
            )
            log.info("Subscribed to %d market(s): %s", len(tickers), ", ".join(tickers))

    def _handle_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        kind = msg.get("type")
        if kind == "subscribed":
            self._sub_id = msg.get("msg", {}).get("sid", self._sub_id)
            return
        if kind == "error":
            log.warning("Market feed error frame: %s", msg.get("msg"))
            return

        body = msg.get("msg") or {}
        ticker = body.get("market_ticker")
        if not ticker:
            return
        book = self.books.setdefault(ticker, OrderBook(ticker))

        if kind == "orderbook_snapshot":
            book.apply_snapshot(
                body.get("yes") or [], body.get("no") or [], msg.get("seq")
            )
        elif kind == "orderbook_delta":
            book.apply_delta(
                body.get("side", "yes"),
                int(body.get("price", 0)),
                int(body.get("delta", 0)),
                msg.get("seq"),
            )

    # ---------------- REST fallback ----------------

    async def _poll_loop(self) -> None:
        while True:
            await self._poll_once_all()
            await asyncio.sleep(self.poll_interval)

    async def _poll_once_all(self) -> None:
        tickers = sorted(self._tickers)
        if not tickers:
            return
        results = await asyncio.gather(
            *(self._poll_one(t) for t in tickers), return_exceptions=True
        )
        for ticker, result in zip(tickers, results):
            if isinstance(result, Exception):
                log.debug("Order book poll failed for %s: %s", ticker, result)

    async def _poll_one(self, ticker: str) -> None:
        data = await self.rest.get_orderbook(ticker)
        book = self.books.setdefault(ticker, OrderBook(ticker))
        book.apply_snapshot(data.get("yes") or [], data.get("no") or [])


class BookHistory:
    """Rolling history of a book's fair value, so strategies can see momentum.

    Kept separate from `OrderBook` because the book is a pure current-state
    object shared by every consumer, while history is a strategy input with its
    own retention policy.
    """

    def __init__(self, window_s: float = 90.0) -> None:
        self.window_s = window_s
        self._points: dict[str, list[tuple[float, float]]] = {}

    def observe(self, ticker: str, value: float, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        series = self._points.setdefault(ticker, [])
        series.append((now, value))
        cutoff = now - self.window_s
        while series and series[0][0] < cutoff:
            series.pop(0)

    def change_over(self, ticker: str, seconds: float, now: float | None = None) -> float | None:
        """Change in fair value over the last `seconds`, in cents."""
        series = self._points.get(ticker)
        if not series or len(series) < 2:
            return None
        now = now if now is not None else time.time()
        cutoff = now - seconds
        past = [v for t, v in series if t <= cutoff]
        baseline = past[-1] if past else series[0][1]
        return series[-1][1] - baseline

    def samples(self, ticker: str) -> int:
        return len(self._points.get(ticker, []))

    def tickers(self) -> list[str]:
        return list(self._points)

    def forget(self, ticker: str) -> None:
        self._points.pop(ticker, None)
