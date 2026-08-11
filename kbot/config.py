"""Process-wide configuration, loaded from the environment."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

# Kalshi hosts. The demo host is a full paper-trading environment run by Kalshi
# itself; `paper mode` in this bot is a separate, purely local simulation that
# needs no credentials at all.
# `external-api` are the hosts Kalshi recommends for API traders; the older
# shared hosts remain supported but are not preferred.
PROD_REST = "https://external-api.kalshi.com/trade-api/v2"
PROD_WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
DEMO_REST = "https://external-api.demo.kalshi.co/trade-api/v2"
DEMO_WS = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"

# Series tickers for the 15-minute up/down crypto markets ("<COIN> price up in
# next 15 mins?"). These are the `15M` series — the similarly-named `KX<COIN>D`
# series are the *hourly* directional markets, not these.
#
# Kalshi adds and renames series over time, so this map is overridable via
# KALSHI_SERIES (JSON), and `python -m kbot.tools series` re-derives it from the
# live API by looking for series whose frequency is `fifteen_min`.
DEFAULT_SERIES: dict[str, str] = {
    "BTC": "KXBTC15M",
    "ETH": "KXETH15M",
    "SOL": "KXSOL15M",
    "XRP": "KXXRP15M",
    "DOGE": "KXDOGE15M",
    "BNB": "KXBNB15M",
    "HYPE": "KXHYPE15M",
    "NEAR": "KXNEAR15M",
    "ZEC": "KXZEC15M",
    "ADA": "KXADA15M",
    "BCH": "KXBCH15M",
    "TON": "KXTON15M",
}

# Spot reference feeds, used only as an optional strategy input. Coins without a
# feed still trade — the order-book signal does not require spot.
DEFAULT_SPOT_PRODUCTS: dict[str, str] = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
    "DOGE": "DOGE-USD",
    "NEAR": "NEAR-USD",
    "ZEC": "ZEC-USD",
    "ADA": "ADA-USD",
    "BCH": "BCH-USD",
}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_json(name: str, default: dict[str, str]) -> dict[str, str]:
    raw = os.getenv(name)
    if not raw:
        return dict(default)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object")
    return {str(k).upper(): str(v) for k, v in parsed.items()}


def _env_prices() -> dict[str, float]:
    """Per-tier USD prices, overridable without touching code."""
    raw = os.getenv("PRICES_USD")
    if not raw:
        return {}
    return {str(k).lower(): float(v) for k, v in json.loads(raw).items()}


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    admin_ids: frozenset[int]
    master_key: str
    db_path: Path
    demo: bool
    series: dict[str, str] = field(default_factory=dict)
    spot_products: dict[str, str] = field(default_factory=dict)

    # Platform-level Kalshi credentials, used for shared market data only. Never
    # used to place an order — orders always go through the user's own key.
    md_key_id: str | None = None
    md_private_key: str | None = None

    # Payments.
    payment_provider: str = "manual"
    nowpayments_api_key: str | None = None
    nowpayments_ipn_secret: str | None = None
    payment_callback_url: str | None = None
    manual_addresses: dict[str, str] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    webhook_path: str = "/webhook/payment"
    bot_username: str | None = None

    # Engine cadence.
    scan_interval_s: float = 1.0
    discovery_interval_s: float = 20.0
    orderbook_poll_interval_s: float = 2.0
    require_access_key: bool = True

    @property
    def rest_base(self) -> str:
        return DEMO_REST if self.demo else PROD_REST

    @property
    def ws_url(self) -> str:
        return DEMO_WS if self.demo else PROD_WS

    @property
    def has_market_data_creds(self) -> bool:
        return bool(self.md_key_id and self.md_private_key)

    def series_for(self, coin: str) -> str | None:
        return self.series.get(coin.upper())

    def spot_product_for(self, coin: str) -> str | None:
        return self.spot_products.get(coin.upper())

    @property
    def coins(self) -> list[str]:
        return sorted(self.series)


def _read_private_key() -> str | None:
    inline = os.getenv("KALSHI_PRIVATE_KEY")
    if inline and inline.strip():
        # Newlines commonly survive .env only as literal backslash-n.
        return inline.replace("\\n", "\n")
    path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if path:
        return Path(path).expanduser().read_text()
    return None


def load_settings() -> Settings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    master_key = os.getenv("MASTER_KEY", "").strip()
    if not master_key:
        raise RuntimeError(
            "MASTER_KEY is required (generate one with: python -m kbot.tools genkey)"
        )

    admin_raw = os.getenv("ADMIN_IDS", "")
    admins = frozenset(
        int(part) for part in admin_raw.replace(",", " ").split() if part.strip()
    )

    return Settings(
        telegram_token=token,
        admin_ids=admins,
        master_key=master_key,
        db_path=Path(os.getenv("DB_PATH", str(ROOT / "data" / "kbot.sqlite3"))),
        demo=_env_bool("KALSHI_DEMO", False),
        series=_env_json("KALSHI_SERIES", DEFAULT_SERIES),
        spot_products=_env_json("SPOT_PRODUCTS", DEFAULT_SPOT_PRODUCTS),
        payment_provider=os.getenv("PAYMENT_PROVIDER", "manual").strip().lower(),
        nowpayments_api_key=os.getenv("NOWPAYMENTS_API_KEY") or None,
        nowpayments_ipn_secret=os.getenv("NOWPAYMENTS_IPN_SECRET") or None,
        payment_callback_url=os.getenv("PAYMENT_CALLBACK_URL") or None,
        manual_addresses=_env_json("MANUAL_PAY_ADDRESSES", {}),
        prices=_env_prices(),
        webhook_host=os.getenv("WEBHOOK_HOST", "0.0.0.0"),
        webhook_port=_env_int("WEBHOOK_PORT", 8080),
        webhook_path=os.getenv("WEBHOOK_PATH", "/webhook/payment"),
        bot_username=(os.getenv("BOT_USERNAME") or "").lstrip("@") or None,
        md_key_id=os.getenv("KALSHI_API_KEY_ID") or None,
        md_private_key=_read_private_key(),
        scan_interval_s=float(os.getenv("SCAN_INTERVAL_S", "1.0")),
        discovery_interval_s=float(os.getenv("DISCOVERY_INTERVAL_S", "20")),
        orderbook_poll_interval_s=float(os.getenv("ORDERBOOK_POLL_INTERVAL_S", "2")),
        require_access_key=_env_bool("REQUIRE_ACCESS_KEY", True),
    )
