"""Thin async REST client for the Kalshi trade API."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

from .auth import Signer

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

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict[str, Any]:
        data = await self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )
        return data.get("orderbook", data)

    # ---------------- portfolio ----------------

    async def get_balance(self) -> int:
        """Available balance in cents."""
        data = await self._request("GET", "/portfolio/balance")
        return int(data.get("balance", 0))

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

    async def create_order(
        self,
        *,
        ticker: str,
        action: str,  # "buy" | "sell"
        side: str,  # "yes" | "no"
        count: int,
        price: int | None = None,
        order_type: str = "limit",
        time_in_force: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": int(count),
            "type": order_type,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if order_type == "limit":
            if price is None:
                raise ValueError("limit orders require a price")
            # Kalshi prices the order on the side you are trading.
            body["yes_price" if side == "yes" else "no_price"] = int(price)
        if time_in_force:
            body["time_in_force"] = time_in_force

        data = await self._request("POST", "/portfolio/orders", json_body=body)
        return data.get("order", data)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def ping(self) -> bool:
        """Cheap authenticated call used to validate a user's credentials."""
        await self.get_balance()
        return True
