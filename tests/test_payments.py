"""Payment flow tests.

The property that matters most: one payment issues exactly one key, no matter
how many times the provider retries its callback, and an unsigned callback
issues none at all.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from cryptography.fernet import Fernet

from kbot.config import Settings
from kbot.payments import PaymentService, build_provider
from kbot.payments.provider import (
    ManualProvider,
    NOWPaymentsProvider,
    PaymentUpdate,
)
from kbot.payments.webhook import WebhookServer
from kbot.storage import Storage

IPN_SECRET = "test-ipn-secret"


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        telegram_token="test",
        admin_ids=frozenset({99}),
        master_key=Fernet.generate_key().decode(),
        db_path=tmp_path / "pay.sqlite3",
        demo=True,
        series={"BTC": "KXBTC15M"},
        spot_products={},
    )
    base.update(overrides)
    return Settings(**base)


class Harness:
    def __init__(self, tmp_path, provider=None, **settings_overrides):
        self.settings = make_settings(tmp_path, **settings_overrides)
        self.storage = Storage(self.settings.db_path, self.settings.master_key)
        self.messages: list[tuple[int, str]] = []
        self.service = PaymentService(
            self.settings, self.storage, self.notify, provider=provider
        )

    async def notify(self, tg_id: int, text: str) -> None:
        self.messages.append((tg_id, text))

    def close(self):
        self.storage.close()


@pytest.fixture()
def manual(tmp_path):
    h = Harness(
        tmp_path,
        provider=ManualProvider({"BTC": "bc1qexample", "USDT": "0xexample"}),
        manual_addresses={"BTC": "bc1qexample"},
    )
    yield h
    h.close()


# ---------------- invoices ----------------


async def test_invoice_is_recorded_and_priced(manual):
    invoice = await manual.service.create_invoice(1, "monthly")
    assert invoice.pay_address == "bc1qexample"

    stored = await manual.storage.get_invoice(invoice.provider_id)
    assert stored is not None
    assert stored.tier == "monthly"
    assert stored.amount_usd == 100.0
    assert stored.status == "pending"
    assert stored.tg_id == 1


async def test_prices_are_overridable(tmp_path):
    h = Harness(
        tmp_path,
        provider=ManualProvider({"BTC": "addr"}),
        prices={"monthly": 149.0},
    )
    invoice = await h.service.create_invoice(1, "monthly")
    stored = await h.storage.get_invoice(invoice.provider_id)
    assert stored.amount_usd == 149.0
    h.close()


async def test_currency_choice_is_honoured(manual):
    invoice = await manual.service.create_invoice(1, "daily", currency="USDT")
    assert invoice.pay_address == "0xexample"
    assert invoice.pay_currency == "USDT"


async def test_unknown_tier_is_rejected(manual):
    with pytest.raises(ValueError):
        await manual.service.create_invoice(1, "annual")


# ---------------- settlement ----------------


async def test_settlement_issues_a_key_and_delivers_it(manual):
    await manual.storage.upsert_user(1, "buyer")
    invoice = await manual.service.create_invoice(1, "weekly")

    key = await manual.service.settle(invoice.provider_id)
    assert key is not None

    stored = await manual.storage.get_invoice(invoice.provider_id)
    assert stored.status == "paid"
    assert stored.key_issued == key

    # The buyer was told, and the key actually works.
    assert any(key in text for _, text in manual.messages)
    ok, _ = await manual.storage.redeem_key(1, key)
    assert ok


async def test_admins_are_notified_of_a_sale(manual):
    await manual.storage.upsert_user(1, "buyer")
    invoice = await manual.service.create_invoice(1, "monthly")
    await manual.service.settle(invoice.provider_id)
    assert any(tg_id == 99 and "sold" in text for tg_id, text in manual.messages)


async def test_repeated_callbacks_issue_exactly_one_key(manual):
    await manual.storage.upsert_user(1, "buyer")
    invoice = await manual.service.create_invoice(1, "daily")
    update = PaymentUpdate(order_id=invoice.provider_id, status="paid")

    await manual.service.on_payment(update)
    await manual.service.on_payment(update)
    await manual.service.on_payment(update)

    stored = await manual.storage.get_invoice(invoice.provider_id)
    keys = [k for k in [stored.key_issued] if k]
    assert len(keys) == 1
    # Only one delivery message, so the buyer is not spammed on every retry.
    deliveries = [t for tg, t in manual.messages if tg == 1 and "Payment confirmed" in t]
    assert len(deliveries) == 1

    rows = manual.storage._conn.execute("SELECT COUNT(*) c FROM access_keys").fetchone()
    assert rows["c"] == 1


async def test_a_pending_callback_does_not_settle(manual):
    invoice = await manual.service.create_invoice(1, "daily")
    await manual.service.on_payment(
        PaymentUpdate(order_id=invoice.provider_id, status="pending")
    )
    stored = await manual.storage.get_invoice(invoice.provider_id)
    assert stored.status == "pending"
    assert stored.key_issued is None


async def test_a_failed_callback_marks_the_invoice_failed(manual):
    invoice = await manual.service.create_invoice(1, "daily")
    await manual.service.on_payment(
        PaymentUpdate(order_id=invoice.provider_id, status="failed")
    )
    stored = await manual.storage.get_invoice(invoice.provider_id)
    assert stored.status == "failed"


async def test_payment_for_an_unknown_order_is_ignored(manual):
    await manual.service.on_payment(PaymentUpdate(order_id="nope", status="paid"))
    assert manual.messages == []


async def test_revenue_totals(manual):
    await manual.storage.upsert_user(1, "buyer")
    for tier in ("daily", "monthly", "monthly"):
        invoice = await manual.service.create_invoice(1, tier)
        await manual.service.settle(invoice.provider_id)
    revenue = await manual.storage.revenue()
    assert revenue["monthly"] == (2, 200.0)
    assert revenue["daily"] == (1, 25.0)


# ---------------- provider selection ----------------


def test_manual_is_the_default_provider(tmp_path):
    assert build_provider(make_settings(tmp_path)).name == "manual"


def test_nowpayments_selected_when_configured(tmp_path):
    settings = make_settings(
        tmp_path, payment_provider="nowpayments", nowpayments_api_key="k"
    )
    assert build_provider(settings).name == "nowpayments"


def test_nowpayments_without_a_key_falls_back_to_manual(tmp_path):
    settings = make_settings(tmp_path, payment_provider="nowpayments")
    assert build_provider(settings).name == "manual"


def test_buy_is_disabled_without_addresses(tmp_path):
    h = Harness(tmp_path)
    assert h.service.enabled is False
    h.close()


def test_buy_is_enabled_once_an_address_exists(tmp_path):
    h = Harness(tmp_path, manual_addresses={"BTC": "bc1q"})
    assert h.service.enabled is True
    assert h.service.automatic is False
    h.close()


# ---------------- NOWPayments signature ----------------


def sign(payload: dict) -> str:
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hmac.new(IPN_SECRET.encode(), canonical.encode(), hashlib.sha512).hexdigest()


def nowpayments() -> NOWPaymentsProvider:
    return NOWPaymentsProvider(api_key="k", ipn_secret=IPN_SECRET)


def test_valid_signature_is_accepted():
    provider = nowpayments()
    payload = {"order_id": "db-1-abc", "payment_status": "finished"}
    body = json.dumps(payload).encode()
    assert provider.verify_webhook(body, {"x-nowpayments-sig": sign(payload)})


def test_signature_check_is_header_case_insensitive():
    provider = nowpayments()
    payload = {"order_id": "db-1-abc", "payment_status": "finished"}
    body = json.dumps(payload).encode()
    assert provider.verify_webhook(body, {"X-NowPayments-Sig": sign(payload)})


def test_tampered_body_is_rejected():
    provider = nowpayments()
    payload = {"order_id": "db-1-abc", "payment_status": "finished"}
    signature = sign(payload)
    tampered = json.dumps({**payload, "order_id": "db-999-hacked"}).encode()
    assert not provider.verify_webhook(tampered, {"x-nowpayments-sig": signature})


def test_missing_signature_is_rejected():
    provider = nowpayments()
    body = json.dumps({"order_id": "x", "payment_status": "finished"}).encode()
    assert not provider.verify_webhook(body, {})


def test_statuses_map_onto_our_three_outcomes():
    provider = nowpayments()
    for raw, expected in [
        ("finished", "paid"),
        ("confirmed", "paid"),
        ("waiting", "pending"),
        ("confirming", "pending"),
        ("failed", "failed"),
        ("expired", "failed"),
    ]:
        update = provider.parse_webhook(
            {"order_id": "db-1-abc", "payment_status": raw}
        )
        assert update is not None and update.status == expected, raw


def test_webhook_without_an_order_id_is_ignored():
    assert nowpayments().parse_webhook({"payment_status": "finished"}) is None


async def test_invoice_creation_posts_the_expected_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "payment_id": "pay-1",
                "pay_address": "bc1qxyz",
                "pay_amount": 0.0012,
                "pay_currency": "btc",
            },
        )

    provider = NOWPaymentsProvider(
        api_key="secret-key",
        ipn_secret=IPN_SECRET,
        callback_url="https://example.test/webhook/payment",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    invoice = await provider.create_invoice(
        order_id="db-1-abc", amount_usd=100.0, tier="monthly", currency="BTC"
    )
    assert invoice.pay_address == "bc1qxyz"
    assert invoice.pay_currency == "BTC"

    request = captured[-1]
    assert request.headers["x-api-key"] == "secret-key"
    body = json.loads(request.content)
    assert body["price_amount"] == 100.0
    assert body["order_id"] == "db-1-abc"
    assert body["ipn_callback_url"] == "https://example.test/webhook/payment"


# ---------------- webhook server ----------------


class FakeProvider:
    name = "fake"
    supports_webhooks = True

    def __init__(self, valid: bool = True):
        self.valid = valid

    def verify_webhook(self, body, headers):
        return self.valid

    def parse_webhook(self, payload):
        return PaymentUpdate(order_id=payload["order_id"], status="paid")


async def post(port: int, path: str, body: dict) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.post(f"http://127.0.0.1:{port}{path}", json=body)


async def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_signed_callback_reaches_the_handler():
    seen: list[PaymentUpdate] = []
    port = await free_port()
    server = WebhookServer(
        FakeProvider(valid=True), lambda u: _collect(seen, u), port=port
    )
    await server.start()
    try:
        resp = await post(port, "/webhook/payment", {"order_id": "db-1-abc"})
        assert resp.status_code == 200
        for _ in range(50):
            if seen:
                break
            await _sleep()
        assert [u.order_id for u in seen] == ["db-1-abc"]
    finally:
        await server.stop()


async def test_unsigned_callback_is_rejected_and_never_handled():
    seen: list[PaymentUpdate] = []
    port = await free_port()
    server = WebhookServer(
        FakeProvider(valid=False), lambda u: _collect(seen, u), port=port
    )
    await server.start()
    try:
        resp = await post(port, "/webhook/payment", {"order_id": "db-1-abc"})
        assert resp.status_code == 401
        assert seen == []
    finally:
        await server.stop()


async def test_health_endpoint_and_unknown_paths():
    port = await free_port()
    server = WebhookServer(FakeProvider(), lambda u: _collect([], u), port=port)
    await server.start()
    try:
        async with httpx.AsyncClient() as client:
            health = await client.get(f"http://127.0.0.1:{port}/healthz")
            assert health.status_code == 200
            assert health.json() == {"ok": True}

            missing = await client.post(f"http://127.0.0.1:{port}/nope", json={})
            assert missing.status_code == 404
    finally:
        await server.stop()


async def _collect(sink: list, update: PaymentUpdate) -> None:
    sink.append(update)


async def _sleep() -> None:
    import asyncio

    await asyncio.sleep(0.01)


# ---------------- invoice wording ----------------


async def test_manual_invoice_quotes_usd_not_a_fabricated_coin_amount():
    """A manual provider has no exchange rate, so it must not imply one.

    Regression guard: quoting "100.00 BTC" for a $100 product would ask the
    buyer to send roughly six figures of bitcoin.
    """
    from kbot.telegram import ui

    provider = ManualProvider({"BTC": "bc1qexample"})
    invoice = await provider.create_invoice(
        order_id="db-1-abc", amount_usd=100.0, tier="monthly", currency="BTC"
    )
    assert invoice.pay_amount is None

    text = ui.invoice_text("monthly", 100.0, invoice, automatic=False)
    assert "$100.00</b> worth of <b>BTC" in text
    assert "100.00 BTC" not in text


async def test_provider_quoted_amount_is_shown_exactly():
    from kbot.telegram import ui

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "payment_id": "pay-1",
                "pay_address": "bc1qxyz",
                "pay_amount": 0.0012,
                "pay_currency": "btc",
            },
        )

    provider = NOWPaymentsProvider(
        api_key="k",
        ipn_secret=IPN_SECRET,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    invoice = await provider.create_invoice(
        order_id="db-1-abc", amount_usd=100.0, tier="monthly", currency="BTC"
    )
    text = ui.invoice_text("monthly", 100.0, invoice, automatic=True)
    assert "Send exactly <b>0.0012 BTC</b>" in text
