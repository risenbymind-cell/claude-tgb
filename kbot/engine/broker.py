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

import httpx
from dataclasses import dataclass
from typing import Protocol

from ..kalshi.fees import fee_dc as estimate_fee_dc
from ..kalshi.orderbook import OrderBook
from ..kalshi.prices import format_cents
from ..kalshi.rest import KalshiClient, KalshiError

log = logging.getLogger(__name__)


class IntentRecorder:
    """Binds one user's orders to the durable intent log.

    A thin object rather than passing storage and a user id through every
    call, so the broker stays testable without a database and paper mode does
    not acquire one.
    """

    def __init__(self, storage, tg_id: int, mode: str) -> None:
        self.storage = storage
        self.tg_id = tg_id
        self.mode = mode

    async def record(
        self,
        *,
        client_order_id: str,
        ticker: str,
        action: str,
        side: str,
        count: int,
        price_dc: int,
    ) -> None:
        await self.storage.record_intent(
            client_order_id=client_order_id,
            tg_id=self.tg_id,
            ticker=ticker,
            action=action,
            side=side,
            count=count,
            price_dc=price_dc,
            mode=self.mode,
        )

    async def resolve(self, client_order_id: str, **kwargs) -> None:
        await self.storage.resolve_intent(client_order_id, **kwargs)


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
        self,
        ticker: str,
        side: str,
        count: int,
        price_dc: int,
        book: OrderBook | None,
        *,
        time_in_force: str = "good_till_canceled",
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
        self,
        ticker: str,
        side: str,
        count: int,
        price_dc: int,
        book: OrderBook | None,
        *,
        time_in_force: str = "good_till_canceled",
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
    """Real orders on the user's own Kalshi account.

    Every submission is preceded by a durable record of the intent, keyed by
    the `client_order_id` the order will carry. The dangerous window is between
    the exchange accepting an order and this process writing down that it did:
    a crash there leaves a position nobody knows about. Writing first inverts
    the failure into an intent with an unknown outcome, which reconciliation
    can resolve by asking the exchange.
    """

    paper = False

    def __init__(
        self,
        client: KalshiClient,
        *,
        intents: "IntentRecorder | None" = None,
    ) -> None:
        self.client = client
        self.intents = intents

    async def _submit(
        self,
        *,
        ticker: str,
        action: str,
        side: str,
        count: int,
        price_dc: int,
        time_in_force: str,
    ) -> OrderResult:
        """Persist intent, submit, record the outcome.

        The id is generated here rather than inside the REST layer so that the
        value written to disk is provably the value sent to the exchange. It
        stays constant across the client's internal retries, which is what
        makes a retry idempotent rather than a second order.
        """
        client_order_id = str(uuid.uuid4())
        if self.intents is not None:
            await self.intents.record(
                client_order_id=client_order_id,
                ticker=ticker,
                action=action,
                side=side,
                count=count,
                price_dc=price_dc,
            )

        try:
            order = await self.client.create_order(
                ticker=ticker,
                action=action,
                side=side,
                count=count,
                price_dc=price_dc,
                time_in_force=time_in_force,
                client_order_id=client_order_id,
            )
        except (KalshiError, httpx.HTTPError) as exc:
            # A transport failure is not proof the order was refused -- the
            # exchange may have accepted it and the response been lost. Mark it
            # unknown so reconciliation looks, rather than rejected, which
            # would quietly close the case on a live position.
            #
            # httpx errors are caught here too: the REST client re-raises the
            # last transport exception when its retries are exhausted, and that
            # path previously escaped the broker entirely.
            status = getattr(exc, "status", None)
            unknown = status is None or status >= 500
            if self.intents is not None:
                await self.intents.resolve(
                    client_order_id,
                    status="unknown" if unknown else "rejected",
                    error=str(exc),
                )
            return OrderResult(ok=False, error=str(exc))

        filled = int(order["fill_count"])
        fill_price = order.get("avg_price_dc") or price_dc
        fee = order.get("fee_dc")
        fee_dc = fee if fee is not None else estimate_fee_dc(filled, fill_price)
        if self.intents is not None:
            await self.intents.resolve(
                client_order_id,
                status="filled" if filled else "rejected",
                order_id=order.get("order_id"),
                filled=filled,
                fee_dc=fee_dc if filled else 0,
            )
        return OrderResult(
            ok=bool(filled),
            order_id=order.get("order_id"),
            filled=filled,
            price_dc=fill_price,
            fee_dc=fee_dc if filled else 0,
            error=None if filled else "order did not fill at the limit",
        )

    async def balance(self) -> int | None:
        try:
            return await self.client.get_balance()
        except KalshiError as exc:
            log.warning("Balance lookup failed: %s", exc)
            return None

    async def buy(
        self, ticker: str, side: str, count: int, price_dc: int, book: OrderBook | None
    ) -> OrderResult:
        # Immediate-or-cancel: we want the edge now or not at all. A resting
        # entry that fills a minute later is a different trade from the one the
        # strategy signalled.
        return await self._submit(
            ticker=ticker, action="buy", side=side, count=count,
            price_dc=price_dc, time_in_force="immediate_or_cancel",
        )

    async def sell(
        self,
        ticker: str,
        side: str,
        count: int,
        price_dc: int,
        book: OrderBook | None,
        *,
        time_in_force: str = "good_till_canceled",
    ) -> OrderResult:
        """Sell, resting by default.

        The engine places an exit immediately after an entry so the position
        is never unmanaged, and that exit is meant to sit on the book until
        the market reaches it -- `good_till_canceled` is right there.

        A caller that only sells when the price has *already* arrived wants
        the opposite, and must say so. `_submit` reports `ok` from the
        immediate fill count, so a resting order comes back `ok=False`; a
        caller that treats that as a rejection and retries will stack a live
        order per attempt. Passing `immediate_or_cancel` makes the result
        final: it filled, or it did not and nothing is resting.
        """
        return await self._submit(
            ticker=ticker, action="sell", side=side, count=count,
            price_dc=price_dc, time_in_force=time_in_force,
        )

