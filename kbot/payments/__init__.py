"""Payments: buy a key with crypto, get access the moment it confirms."""

from __future__ import annotations

import logging
import secrets
from typing import Awaitable, Callable

from ..config import Settings
from ..storage import PRICES_USD, TIERS, Storage
from .provider import (
    Invoice,
    ManualProvider,
    NOWPaymentsProvider,
    PaymentProvider,
    PaymentUpdate,
)
from .webhook import WebhookServer

log = logging.getLogger(__name__)

Notifier = Callable[[int, str], Awaitable[None]]


def build_provider(settings: Settings) -> PaymentProvider:
    """Pick a provider from configuration, defaulting to manual."""
    if settings.payment_provider == "nowpayments" and settings.nowpayments_api_key:
        return NOWPaymentsProvider(
            api_key=settings.nowpayments_api_key,
            ipn_secret=settings.nowpayments_ipn_secret or "",
            callback_url=settings.payment_callback_url,
        )
    return ManualProvider(settings.manual_addresses)


class PaymentService:
    """Creates invoices, settles them, and hands the buyer their key."""

    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        notify: Notifier,
        provider: PaymentProvider | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.notify = notify
        self.provider = provider or build_provider(settings)
        self.server: WebhookServer | None = None

    @property
    def enabled(self) -> bool:
        """Whether /buy should be offered at all."""
        if isinstance(self.provider, ManualProvider):
            return bool(self.settings.manual_addresses)
        return True

    @property
    def automatic(self) -> bool:
        return self.provider.supports_webhooks

    async def start(self) -> None:
        if not self.provider.supports_webhooks:
            return
        self.server = WebhookServer(
            self.provider,
            self.on_payment,
            host=self.settings.webhook_host,
            port=self.settings.webhook_port,
            path=self.settings.webhook_path,
        )
        await self.server.start()

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()
        aclose = getattr(self.provider, "aclose", None)
        if aclose is not None:
            await aclose()

    # ---------------- buying ----------------

    async def create_invoice(
        self, tg_id: int, tier: str, currency: str | None = None
    ) -> Invoice:
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}")
        amount = self.settings.prices.get(tier, PRICES_USD[tier])
        order_id = f"db-{tg_id}-{secrets.token_hex(6)}"

        invoice = await self.provider.create_invoice(
            order_id=order_id, amount_usd=amount, tier=tier, currency=currency
        )
        await self.storage.create_invoice(
            order_id=order_id,
            tg_id=tg_id,
            tier=tier,
            amount_usd=amount,
            provider=self.provider.name,
            provider_id=invoice.provider_id,
            currency=invoice.pay_currency,
            pay_address=invoice.pay_address,
            pay_amount=invoice.pay_amount,
            checkout_url=invoice.checkout_url,
        )
        return invoice

    # ---------------- settling ----------------

    async def on_payment(self, update: PaymentUpdate) -> None:
        """Handle a verified provider callback."""
        if update.status == "failed":
            await self.storage.fail_invoice(update.order_id)
            return
        if not update.is_paid:
            return
        await self.settle(update.order_id)

    async def settle(self, order_id: str) -> str | None:
        """Mint and deliver the key for a paid order. Safe to call twice."""
        invoice = await self.storage.get_invoice(order_id)
        if invoice is None:
            log.warning("Payment for unknown order %s", order_id)
            return None
        already_paid = invoice.status == "paid"

        settled, key = await self.storage.settle_invoice(order_id)
        if settled is None or key is None:
            return None
        if already_paid:
            # A retried callback: the key was already delivered, so stay quiet.
            return key

        await self.notify(
            settled.tg_id,
            f"✅ <b>Payment confirmed</b> — {settled.tier} access\n\n"
            f"Your key:\n<code>{key}</code>\n\n"
            f"Redeem it with <code>/redeem {key}</code>",
        )
        for admin_id in self.settings.admin_ids:
            await self.notify(
                admin_id,
                f"💰 {settled.tier} sold — ${settled.amount_usd:.2f} "
                f"(user <code>{settled.tg_id}</code>)",
            )
        return key


__all__ = [
    "Invoice",
    "ManualProvider",
    "NOWPaymentsProvider",
    "PaymentProvider",
    "PaymentService",
    "PaymentUpdate",
    "WebhookServer",
    "build_provider",
]
