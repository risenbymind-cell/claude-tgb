"""Order placement, in two interchangeable flavours.

`PaperBroker` and `LiveBroker` expose the same three calls, so the trading loop
is written once and the only difference between a simulation and a real order is
which object it holds. That is what makes paper mode trustworthy: it is not a
separate code path, it is the same code path with a different broker.

All prices crossing this boundary are integer deci-cents (see
`kalshi/prices.py`).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from ..kalshi.fees import fee_dc as estimate_fee_dc
from ..kalshi.orderbook import OrderBook
from ..kalshi.prices import format_cents
from ..kalshi.rest import KalshiClient, KalshiError

log = logging.getLogger(__name__)


@dataclass
class OrderResult:
    ok: bool
    order_id: str | None = None
    filled: int = 0
    price_dc: int | None = None
    #: Trading fee actually charged (live) or estimated (paper), in deci-cents.
    fee_dc: int = 0
    error: str | None = None


class Broker(Protocol):
    paper: bool

    async def balance(self) -> int | None: ...

    async def buy(
        self, ticker: str, side: str, count: int, price_dc: int, book: OrderBook | None
    ) -> OrderResult: ...

    async def sell(
        self, ticker: str, side: str, count: int, price_dc: int, book: OrderBook | None
    ) -> OrderResult: ...


class PaperBroker:
    """Simulated fills at real, live prices.

    Entries are modelled as marketable limit orders: they fill only if the
    requested price is at or above the current ask, and only up to the size
    actually resting there. Exits are resting orders — the runner decides when
    the market has traded through them.
    """

    paper = True

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}

    async def balance(self) -> int | None:
        return None  # paper mode is not balance-constrained

    async def buy(
        self, ticker: str, side: str, count: int, price_dc: int, book: OrderBook | None
    ) -> OrderResult:
        if book is None or book.is_stale:
            return OrderResult(ok=False, error="no live book to fill against")
        ask = book.best_ask(side)
        if ask is None:
            return OrderResult(ok=False, error="no offer on that side")
        if price_dc < ask:
            return OrderResult(
                ok=False,
                error=f"limit {format_cents(price_dc)} below ask {format_cents(ask)}",
            )

        available = book.size_at_ask(side)
        # Only whole contracts are ever placed, so a partial level rounds down.
        filled = int(min(count, available)) if available else 0
        if filled < 1:
            return OrderResult(ok=False, error="no size at the offer")

        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        fee = estimate_fee_dc(filled, ask)
        self.orders[order_id] = {
            "ticker": ticker,
            "side": side,
            "count": filled,
            "price_dc": ask,
            "fee_dc": fee,
            "ts": time.time(),
        }
        return OrderResult(
            ok=True, order_id=order_id, filled=filled, price_dc=ask, fee_dc=fee
        )

    async def sell(
        self, ticker: str, side: str, count: int, price_dc: int, book: OrderBook | None
    ) -> OrderResult:
        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        self.orders[order_id] = {
            "ticker": ticker,
            "side": side,
            "count": count,
            "price_dc": price_dc,
            "resting": True,
            "ts": time.time(),
        }
        return OrderResult(ok=True, order_id=order_id, filled=0, price_dc=price_dc)


class LiveBroker:
    """Real orders on the user's own Kalshi account."""

    paper = False

    def __init__(self, client: KalshiClient) -> None:
        self.client = client

    async def balance(self) -> int | None:
        try:
            return await self.client.get_balance()
        except KalshiError as exc:
            log.warning("Balance lookup failed: %s", exc)
            return None

    async def buy(
        self, ticker: str, side: str, count: int, price_dc: int, book: OrderBook | None
    ) -> OrderResult:
        try:
            order = await self.client.create_order(
                ticker=ticker,
                action="buy",
                side=side,
                count=count,
                price_dc=price_dc,
                # Immediate-or-cancel: we want the edge now or not at all. A
                # resting entry that fills a minute later is a different trade
                # from the one the strategy signalled.
                time_in_force="immediate_or_cancel",
            )
        except KalshiError as exc:
            return OrderResult(ok=False, error=str(exc))

        filled = int(order["fill_count"])
        if filled < 1:
            return OrderResult(
                ok=False,
                order_id=order.get("order_id"),
                error="order did not fill at the limit",
            )
        fill_price = order.get("avg_price_dc") or price_dc
        fee = order.get("fee_dc")
        return OrderResult(
            ok=True,
            order_id=order.get("order_id"),
            filled=filled,
            price_dc=fill_price,
            # Fall back to the exchange's own formula if it did not report one.
            fee_dc=fee if fee is not None else estimate_fee_dc(filled, fill_price),
        )

    async def sell(
        self, ticker: str, side: str, count: int, price_dc: int, book: OrderBook | None
    ) -> OrderResult:
        try:
            order = await self.client.create_order(
                ticker=ticker,
                action="sell",
                side=side,
                count=count,
                price_dc=price_dc,
                time_in_force="good_till_canceled",
            )
        except KalshiError as exc:
            return OrderResult(ok=False, error=str(exc))
        filled = int(order["fill_count"])
        fee = order.get("fee_dc")
        return OrderResult(
            ok=True,
            order_id=order.get("order_id"),
            filled=filled,
            price_dc=price_dc,
            fee_dc=(fee if fee is not None else estimate_fee_dc(filled, price_dc)),
        )
