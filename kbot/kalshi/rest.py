"""Thin async REST client for the Kalshi trade API.

Order placement uses the V2 event-market endpoints (`/portfolio/events/orders`).
The legacy `/portfolio/orders` POST is rejected outright by the exchange with
`deprecated_v1_order_endpoint`, so there is no fallback to keep.

The V2 order API models a binary market as a **single book quoted in YES
terms**: `bid` buys YES, `ask` sells YES (economically, buying NO at the
mirrored price). This module is where that translation happens — the rest of the
bot speaks in "buy the yes side" / "buy the no side", which is what the strategy
and the dashboard mean.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

from .auth import Signer
from .prices import (
    complement,
    count_to_fp,
    dc_to_dollars,
    dollars_to_dc,
    parse_count,
)

log = logging.getLogger(__name__)


class KalshiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Kalshi API {status}: {body}")
        self.status = status
        self.body = body

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403)


class KalshiClient:
    """One client per credential set.

    Market-data reads work unauthenticated; anything under /portfolio requires a
    signer. Callers that only need public data can construct with signer=None.
    """

    def __init__(
        self,
        base_url: str,
        signer: Signer | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        if self.signer is not None:
            headers.update(self.signer.headers(method, url))

        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code < 400:
                    return resp.json() if resp.content else {}
                # 4xx other than rate limiting is a caller error: do not retry.
                if resp.status_code != 429 and resp.status_code < 500:
                    raise KalshiError(resp.status_code, resp.text[:500])
                last_exc = KalshiError(resp.status_code, resp.text[:500])

            if attempt < retries:
                await asyncio.sleep(0.5 * (2**attempt))
                if self.signer is not None:
                    # Timestamps are part of the signature; re-sign each attempt.
                    headers.update(self.signer.headers(method, url))

        assert last_exc is not None
        raise last_exc

    # ---------------- market data ----------------

    async def get_markets(
        self,
        *,
        series_ticker: str | None = None,
        status: str = "open",
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/markets", params=params)

    async def get_market(self, ticker: str) -> dict[str, Any]:
        data = await self._request("GET", f"/markets/{ticker}")
        return data.get("market", data)

    async def get_series_list(self, category: str = "Crypto") -> list[dict[str, Any]]:
        data = await self._request("GET", "/series", params={"category": category})
        return data.get("series", [])

    async def get_orderbook(self, ticker: str, depth: int = 10) -> tuple[Any, Any]:
        """Return the raw (yes, no) bid ladders for a market.

        The response has moved between shapes — `orderbook_fp.yes_dollars` is
        current, `orderbook.yes` is the legacy form — so both are accepted and
        handed to the book parser as-is.
        """
        data = await self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )
        book = data.get("orderbook_fp") or data.get("orderbook") or data
        yes = book.get("yes_dollars_fp") or book.get("yes_dollars") or book.get("yes")
        no = book.get("no_dollars_fp") or book.get("no_dollars") or book.get("no")
        return yes, no

    # ---------------- portfolio ----------------

    async def get_balance(self) -> int:
        """Available balance in deci-cents."""
        data = await self._request("GET", "/portfolio/balance")
        if "balance_dollars" in data:
            return dollars_to_dc(data["balance_dollars"])
        # Legacy field is in whole cents.
        return int(data.get("balance", 0)) * 10

    async def get_positions(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/portfolio/positions")
        return data.get("market_positions", [])

    async def get_order(self, order_id: str) -> dict[str, Any]:
        data = await self._request("GET", f"/portfolio/orders/{order_id}")
        return data.get("order", data)

    async def get_fills(self, ticker: str | None = None, limit: int = 100) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = await self._request("GET", "/portfolio/fills", params=params)
        return data.get("fills", [])

    # ---------------- orders (V2) ----------------

    @staticmethod
    def _to_book_side(side: str, action: str) -> str:
        """Map our (side, action) onto the V2 single-book bid/ask side.

        Buying YES and selling NO both add demand for YES, so both are `bid`;
        selling YES and buying NO are both `ask`.
        """
        buying = action == "buy"
        if side == "yes":
            return "bid" if buying else "ask"
        return "ask" if buying else "bid"

    @staticmethod
    def _to_book_price_dc(side: str, price_dc: int) -> int:
        """Convert a price on our side into the YES-quoted book price."""
        return int(price_dc) if side == "yes" else complement(int(price_dc))

    async def create_order(
        self,
        *,
        ticker: str,
        action: str,  # "buy" | "sell"
        side: str,  # "yes" | "no" — the side we are trading
        count: int,
        price_dc: int,
        time_in_force: str = "immediate_or_cancel",
        client_order_id: str | None = None,
        post_only: bool = False,
    ) -> dict[str, Any]:
        """Place an order, returning a normalised result.

        Returns `{order_id, fill_count, remaining_count, avg_price_dc}` with
        prices converted back onto the side we asked for, so callers never see
        the YES-quoted book price.
        """
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": self._to_book_side(side, action),
            "count": count_to_fp(count),
            "price": dc_to_dollars(self._to_book_price_dc(side, price_dc)),
            "time_in_force": time_in_force,
            # The exchange requires an explicit self-trade policy. `taker_at_cross`
            # cancels our resting order rather than trading against ourselves,
            # which is the right call for a bot that may hold a resting exit on
            # the same market it is entering.
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if post_only:
            body["post_only"] = True

        data = await self._request("POST", "/portfolio/events/orders", json_body=body)

        fill_count = parse_count(data.get("fill_count", 0))
        avg_price_dc: int | None = None
        if data.get("average_fill_price") is not None and fill_count > 0:
            book_price_dc = dollars_to_dc(data["average_fill_price"])
            # Translate the YES-quoted fill back onto our side.
            avg_price_dc = self._to_book_price_dc(side, book_price_dc)

        # average_fee_paid is per contract; the bot tracks the total actually
        # charged so P/L is never an estimate when the exchange has told us.
        fee_dc: int | None = None
        if data.get("average_fee_paid") is not None and fill_count > 0:
            fee_dc = round(dollars_to_dc(data["average_fee_paid"]) * fill_count)

        return {
            "order_id": data.get("order_id"),
            "client_order_id": data.get("client_order_id"),
            "fill_count": fill_count,
            "remaining_count": parse_count(data.get("remaining_count", 0)),
            "avg_price_dc": avg_price_dc,
            "fee_dc": fee_dc,
            "raw": data,
        }

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/portfolio/events/orders/{order_id}")

    async def ping(self) -> bool:
        """Cheap authenticated call used to validate a user's credentials."""
        await self.get_balance()
        return True
