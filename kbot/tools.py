"""Small operator utilities: `python -m kbot.tools <command>`."""

from __future__ import annotations

import asyncio
import sys

from cryptography.fernet import Fernet


def cmd_genkey() -> int:
    """Print a fresh MASTER_KEY for encrypting stored Kalshi credentials."""
    print(Fernet.generate_key().decode())
    return 0


def cmd_mintkeys(tier: str, count: str) -> int:
    """Mint access keys straight into the database, without Telegram."""
    from .config import load_settings
    from .storage import Storage

    settings = load_settings()
    storage = Storage(settings.db_path, settings.master_key)
    try:
        keys = asyncio.run(storage.mint_keys(tier, int(count)))
    finally:
        storage.close()
    for key in keys:
        print(key)
    return 0


def cmd_markets() -> int:
    """List the 15-minute markets the bot can currently see, per coin."""
    from .config import load_settings
    from .engine.discovery import MarketDiscovery
    from .kalshi.rest import KalshiClient

    settings = load_settings()

    async def run() -> None:
        client = KalshiClient(settings.rest_base)
        discovery = MarketDiscovery(client, settings.series)
        try:
            markets = await discovery.refresh(settings.coins)
        finally:
            await client.aclose()
        if not markets:
            print("No 15-minute markets found. Check KALSHI_SERIES.")
            return
        for coin in sorted(markets):
            m = markets[coin]
            secs = int(m.seconds_to_close())
            print(f"{coin:<5} {m.ticker:<28} closes in {secs//60}m{secs%60:02d}s")

    asyncio.run(run())
    return 0


def cmd_series() -> int:
    """Re-derive the 15-minute series map from the live API.

    Kalshi adds and renames series, so rather than trusting the built-in
    defaults forever, this asks the exchange which crypto series actually have a
    `fifteen_min` frequency and prints a KALSHI_SERIES value you can paste
    straight into .env.
    """
    import json
    import re

    from .config import load_settings
    from .kalshi.rest import KalshiClient

    settings = load_settings()

    async def run() -> None:
        client = KalshiClient(settings.rest_base)
        try:
            series = await client.get_series_list("Crypto")
        finally:
            await client.aclose()

        found: dict[str, str] = {}
        for entry in series:
            if entry.get("frequency") != "fifteen_min":
                continue
            ticker = entry.get("ticker") or ""
            # KX<COIN>15M -> COIN. Multi-coin series (KXCRYPTOLEAD15M and
            # friends) are not up/down markets, so they are skipped.
            match = re.fullmatch(r"KX([A-Z0-9]+)15M", ticker)
            if not match:
                continue
            coin = match.group(1)
            if coin.startswith("CRYPTO"):
                continue
            found[coin] = ticker

        if not found:
            print("No fifteen_min crypto series found.")
            return

        configured = settings.series
        for coin in sorted(found):
            mark = " " if configured.get(coin) == found[coin] else "*"
            print(f"{mark} {coin:<6} {found[coin]}")
        missing = sorted(set(configured) - set(found))
        if missing:
            print(f"\nConfigured but not found: {', '.join(missing)}")
        print("\nKALSHI_SERIES=" + json.dumps(found, separators=(",", ":")))

    asyncio.run(run())
    return 0


COMMANDS = {
    "genkey": cmd_genkey,
    "mintkeys": cmd_mintkeys,
    "markets": cmd_markets,
    "series": cmd_series,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: python -m kbot.tools {{{'|'.join(COMMANDS)}}} [args]")
        return 2
    return COMMANDS[argv[0]](*argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
