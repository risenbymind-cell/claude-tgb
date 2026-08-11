"""REST client tests, especially the V2 single-book order translation.

Kalshi's V2 order API quotes everything from the YES side: `bid` buys YES,
`ask` sells YES (which is economically buying NO at the mirrored price). The bot
speaks in "buy the yes side" / "buy the no side", so this translation is the
place a sign error would silently invert every DOWN trade.
"""

from __future__ import annotations

import json

import httpx
import pytest

from kbot.kalshi.rest import KalshiClient, KalshiError


class Recorder:
    """Captures the request the client sends and replies with a canned body."""

    def __init__(self, response: dict, status: int = 200):
        self.response = response
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.response)

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)

    @property
    def path(self) -> str:
        return self.requests[-1].url.path


def make_client(recorder: Recorder) -> KalshiClient:
    transport = httpx.MockTransport(recorder.handler)
    return KalshiClient(
        "https://example.test/trade-api/v2",
        client=httpx.AsyncClient(transport=transport),
    )


ORDER_OK = {
    "order_id": "abc",
    "client_order_id": "cid",
    "fill_count": "3.00",
    "remaining_count": "0.00",
    "average_fill_price": "0.4800",
}


# ---------------- side translation ----------------


async def test_buying_yes_is_a_bid_at_our_price():
    rec = Recorder(ORDER_OK)
    client = make_client(rec)
    await client.create_order(
        ticker="KXBTC15M-1", action="buy", side="yes", count=3, price_dc=480
    )
    assert rec.path.endswith("/portfolio/events/orders")
    assert rec.body["side"] == "bid"
    assert rec.body["price"] == "0.4800"
    assert rec.body["count"] == "3.00"


async def test_buying_no_is_an_ask_at_the_mirrored_price():
    rec = Recorder(ORDER_OK)
    client = make_client(rec)
    await client.create_order(
        ticker="KXBTC15M-1", action="buy", side="no", count=1, price_dc=480
    )
    # Buying NO at 48c == selling YES at 52c.
    assert rec.body["side"] == "ask"
    assert rec.body["price"] == "0.5200"


async def test_selling_yes_is_an_ask():
    rec = Recorder(ORDER_OK)
    client = make_client(rec)
    await client.create_order(
        ticker="KXBTC15M-1", action="sell", side="yes", count=1, price_dc=560
    )
    assert rec.body["side"] == "ask"
    assert rec.body["price"] == "0.5600"


async def test_selling_no_is_a_bid_at_the_mirrored_price():
    rec = Recorder(ORDER_OK)
    client = make_client(rec)
    await client.create_order(
        ticker="KXBTC15M-1", action="sell", side="no", count=1, price_dc=560
    )
    # Closing a NO position at 56c == bidding 44c for YES.
    assert rec.body["side"] == "bid"
    assert rec.body["price"] == "0.4400"


async def test_fill_price_is_translated_back_onto_our_side():
    # The exchange reports a YES-quoted fill of 52c; on the NO side we paid 48c.
    rec = Recorder({**ORDER_OK, "average_fill_price": "0.5200"})
    client = make_client(rec)
    result = await client.create_order(
        ticker="KXBTC15M-1", action="buy", side="no", count=3, price_dc=490
    )
    assert result["avg_price_dc"] == 480
    assert result["fill_count"] == 3.0


async def test_yes_fill_price_passes_through_unchanged():
    rec = Recorder(ORDER_OK)
    client = make_client(rec)
    result = await client.create_order(
        ticker="KXBTC15M-1", action="buy", side="yes", count=3, price_dc=490
    )
    assert result["avg_price_dc"] == 480


async def test_unfilled_order_reports_no_price():
    rec = Recorder(
        {"order_id": "abc", "fill_count": "0.00", "remaining_count": "0.00"}
    )
    client = make_client(rec)
    result = await client.create_order(
        ticker="KXBTC15M-1", action="buy", side="yes", count=1, price_dc=480
    )
    assert result["fill_count"] == 0
    assert result["avg_price_dc"] is None


# ---------------- required V2 fields ----------------


async def test_required_v2_fields_are_always_present():
    rec = Recorder(ORDER_OK)
    client = make_client(rec)
    await client.create_order(
        ticker="KXBTC15M-1", action="buy", side="yes", count=1, price_dc=480
    )
    body = rec.body
    # The exchange rejects the order outright if any of these are missing.
    for field in (
        "ticker",
        "side",
        "count",
        "price",
        "time_in_force",
        "self_trade_prevention_type",
    ):
        assert field in body, field
    assert body["time_in_force"] == "immediate_or_cancel"
    assert body["self_trade_prevention_type"] in {"taker_at_cross", "maker"}
    assert body["client_order_id"]


async def test_cancel_uses_the_v2_path():
    rec = Recorder({"order_id": "abc", "reduced_by": "1.00"})
    client = make_client(rec)
    await client.cancel_order("abc")
    assert rec.path == "/trade-api/v2/portfolio/events/orders/abc"
    assert rec.requests[-1].method == "DELETE"


# ---------------- reads ----------------


async def test_orderbook_reads_the_current_fp_shape():
    rec = Recorder(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.2400", "73.00"]],
                "no_dollars": [["0.6800", "154.34"]],
            }
        }
    )
    client = make_client(rec)
    yes, no = await client.get_orderbook("KXBTC15M-1")
    assert yes == [["0.2400", "73.00"]]
    assert no == [["0.6800", "154.34"]]


async def test_orderbook_still_reads_the_legacy_shape():
    rec = Recorder({"orderbook": {"yes": [["0.2400", "73.00"]], "no": []}})
    client = make_client(rec)
    yes, no = await client.get_orderbook("KXBTC15M-1")
    assert yes == [["0.2400", "73.00"]]
    assert no == []


async def test_balance_prefers_the_fixed_point_dollar_field():
    rec = Recorder({"balance": 1234, "balance_dollars": "12.3400"})
    client = make_client(rec)
    assert await client.get_balance() == 12340  # deci-cents


async def test_balance_falls_back_to_legacy_cents():
    rec = Recorder({"balance": 1234})
    client = make_client(rec)
    assert await client.get_balance() == 12340


# ---------------- errors ----------------


async def test_client_errors_are_not_retried_and_surface_the_body():
    rec = Recorder({"error": {"code": "bad_request"}}, status=400)
    client = make_client(rec)
    with pytest.raises(KalshiError) as excinfo:
        await client.create_order(
            ticker="KXBTC15M-1", action="buy", side="yes", count=1, price_dc=480
        )
    assert excinfo.value.status == 400
    assert len(rec.requests) == 1  # no retry storm on a caller error


async def test_auth_errors_are_flagged():
    rec = Recorder({"error": "unauthorized"}, status=401)
    client = make_client(rec)
    with pytest.raises(KalshiError) as excinfo:
        await client.get_balance()
    assert excinfo.value.is_auth_error
