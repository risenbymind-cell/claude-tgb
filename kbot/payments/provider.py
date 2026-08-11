"""Crypto payment providers.

The bot needs three things from a provider: create an invoice, verify a webhook
came from the provider, and read the status out of that webhook. Anything that
can do those three things can be plugged in.

`NOWPaymentsProvider` is the built-in implementation (it settles in BTC, ETH,
USDT, USDC, SOL and others, and posts an IPN callback on confirmation).
`ManualProvider` is the default: it creates an invoice record and shows the
buyer a static address to pay, with an admin confirming receipt. That keeps the
bot usable with no third-party account at all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)


@dataclass
class Invoice:
    """A request for payment, as far as the bot is concerned.

    `pay_amount` is the amount in `pay_currency` when the provider can quote a
    rate. When it is None the buyer is quoted in USD instead — never assume the
    two are interchangeable.
    """

    provider_id: str
    pay_address: str | None
    pay_amount: str | None
    pay_currency: str | None
    checkout_url: str | None = None
    amount_usd: float | None = None


@dataclass
class PaymentUpdate:
    """The normalised meaning of a provider webhook."""

    order_id: str
    status: str  # "paid" | "pending" | "failed"
    provider_id: str | None = None
    amount_paid: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def is_paid(self) -> bool:
        return self.status == "paid"


class PaymentProvider(Protocol):
    name: str
    #: Whether this provider can receive webhooks at all. A manual provider
    #: cannot, so the bot shows admin-confirmation instructions instead.
    supports_webhooks: bool

    async def create_invoice(
        self, *, order_id: str, amount_usd: float, tier: str, currency: str | None
    ) -> Invoice: ...

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool: ...

    def parse_webhook(self, payload: dict[str, Any]) -> PaymentUpdate | None: ...


class ManualProvider:
    """No third-party processor: show an address, an admin confirms receipt.

    Deliberately the default. It means the bot works out of the box, and it
    means nobody has to wire up an API account before they can sell a key.
    """

    name = "manual"
    supports_webhooks = False

    def __init__(self, addresses: dict[str, str]) -> None:
        # {"BTC": "bc1...", "USDT_TRC20": "T...", ...}
        self.addresses = addresses

    async def create_invoice(
        self, *, order_id: str, amount_usd: float, tier: str, currency: str | None
    ) -> Invoice:
        chosen = (currency or "").upper()
        address = self.addresses.get(chosen)
        if address is None and self.addresses:
            chosen, address = next(iter(self.addresses.items()))
        # No exchange rate is available here, so the buyer is quoted in USD and
        # told which coin to send. Claiming a coin amount we cannot compute
        # would tell them to send 100 BTC for a $100 product.
        return Invoice(
            provider_id=order_id,
            pay_address=address,
            pay_amount=None,
            pay_currency=chosen or None,
            amount_usd=amount_usd,
        )

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool:
        return False

    def parse_webhook(self, payload: dict[str, Any]) -> PaymentUpdate | None:
        return None


class NOWPaymentsProvider:
    """NOWPayments: hosted crypto checkout with an IPN callback.

    Chosen as the built-in because it settles in the currencies the pricing page
    advertises and signs its callbacks, so a payment can be confirmed without
    trusting the client.
    """

    name = "nowpayments"
    supports_webhooks = True
    API = "https://api.nowpayments.io/v1"

    # Their IPN signature is HMAC-SHA512 over the JSON body with keys sorted.
    SIGNATURE_HEADER = "x-nowpayments-sig"

    PAID_STATUSES = {"finished", "confirmed", "sending"}
    FAILED_STATUSES = {"failed", "refunded", "expired"}

    def __init__(
        self,
        api_key: str,
        ipn_secret: str,
        callback_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.ipn_secret = ipn_secret
        self.callback_url = callback_url
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(timeout=15.0)

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def create_invoice(
        self, *, order_id: str, amount_usd: float, tier: str, currency: str | None
    ) -> Invoice:
        body: dict[str, Any] = {
            "price_amount": amount_usd,
            "price_currency": "usd",
            "order_id": order_id,
            "order_description": f"DirectionalBot {tier} access",
        }
        if self.callback_url:
            body["ipn_callback_url"] = self.callback_url

        if currency:
            # A specific coin gives the buyer an address to send to directly.
            body["pay_currency"] = currency.lower()
            endpoint, key = "/payment", "payment_id"
        else:
            # Otherwise hand them a hosted checkout to pick a coin themselves.
            endpoint, key = "/invoice", "id"

        resp = await self._client.post(
            f"{self.API}{endpoint}", json=body, headers={"x-api-key": self.api_key}
        )
        resp.raise_for_status()
        data = resp.json()
        return Invoice(
            provider_id=str(data.get(key) or order_id),
            pay_address=data.get("pay_address"),
            pay_amount=str(data["pay_amount"]) if data.get("pay_amount") else None,
            pay_currency=(data.get("pay_currency") or currency or "").upper() or None,
            checkout_url=data.get("invoice_url"),
        )

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool:
        """Constant-time check that the callback really came from the provider."""
        signature = ""
        for name, value in headers.items():
            if name.lower() == self.SIGNATURE_HEADER:
                signature = value
                break
        if not signature or not self.ipn_secret:
            return False
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return False
        # Sorted-key re-serialisation is what they sign.
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        expected = hmac.new(
            self.ipn_secret.encode(), canonical.encode(), hashlib.sha512
        ).hexdigest()
        return hmac.compare_digest(expected, signature.strip())

    def parse_webhook(self, payload: dict[str, Any]) -> PaymentUpdate | None:
        order_id = payload.get("order_id")
        if not order_id:
            return None
        raw_status = str(payload.get("payment_status", "")).lower()
        if raw_status in self.PAID_STATUSES:
            status = "paid"
        elif raw_status in self.FAILED_STATUSES:
            status = "failed"
        else:
            status = "pending"
        return PaymentUpdate(
            order_id=str(order_id),
            status=status,
            provider_id=str(payload.get("payment_id") or ""),
            amount_paid=str(payload.get("actually_paid") or ""),
            raw=payload,
        )
