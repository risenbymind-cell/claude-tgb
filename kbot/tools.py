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


def cmd_doctor() -> int:
    """Preflight: check everything a deploy needs, before it is a deploy.

    Exits non-zero if anything is actually broken, so it can gate a release.
    """
    import asyncio
    import socket

    from .config import ConfigError, load_settings

    problems: list[str] = []
    warnings: list[str] = []

    def ok(msg: str) -> None:
        print(f"  \033[32m✓\033[0m {msg}")

    def warn(msg: str) -> None:
        warnings.append(msg)
        print(f"  \033[33m!\033[0m {msg}")

    def bad(msg: str) -> None:
        problems.append(msg)
        print(f"  \033[31m✗\033[0m {msg}")

    print("\nConfiguration")
    try:
        settings = load_settings()
    except (ConfigError, RuntimeError) as exc:
        print(f"  \033[31m✗\033[0m {exc}")
        print("\nFix the configuration and run again.\n")
        return 2
    ok(f"config valid · {'DEMO' if settings.demo else 'PRODUCTION'} environment")
    ok(f"{len(settings.series)} coin(s) configured")
    if not settings.admin_ids:
        warn("ADMIN_IDS is empty — nobody can mint keys or confirm payments")
    else:
        ok(f"{len(settings.admin_ids)} admin(s)")

    print("\nStorage")
    try:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        probe = settings.db_path.parent / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
        ok(f"{settings.db_path.parent} is writable")
    except OSError as exc:
        bad(f"cannot write to {settings.db_path.parent}: {exc}")

    if settings.db_path.exists():
        from .storage import Storage

        try:
            store = Storage(settings.db_path, settings.master_key)
            store.close()
            ok("existing database opens with this MASTER_KEY")
        except Exception as exc:  # noqa: BLE001
            bad(f"database will not open: {exc}")
    else:
        ok("no database yet — one will be created on first run")

    print("\nNetwork")
    try:
        with socket.socket() as sock:
            sock.settimeout(2)
            sock.bind(("127.0.0.1", settings.webhook_port))
        ok(f"webhook port {settings.webhook_port} is free")
    except OSError:
        warn(f"port {settings.webhook_port} is already in use")

    async def checks() -> None:
        from .engine.discovery import MarketDiscovery
        from .kalshi.rest import KalshiClient
        from .telegram.api import TelegramClient, TelegramError

        client = KalshiClient(settings.rest_base)
        try:
            markets = await MarketDiscovery(client, settings.series).refresh(
                settings.coins
            )
            if markets:
                ok(f"Kalshi reachable · {len(markets)} live 15-minute market(s)")
            else:
                warn(
                    "Kalshi reachable but no live windows found — "
                    "run `python -m kbot.tools series` to check tickers"
                )
        except Exception as exc:  # noqa: BLE001
            bad(f"cannot reach Kalshi: {str(exc)[:90]}")
        finally:
            await client.aclose()

        tg = TelegramClient(settings.telegram_token)
        try:
            me = await tg.get_me()
            ok(f"Telegram token valid · @{me.get('username')}")
        except TelegramError as exc:
            bad(f"Telegram token rejected: {exc}")
        except Exception as exc:  # noqa: BLE001
            bad(f"cannot reach Telegram: {str(exc)[:90]}")
        finally:
            await tg.aclose()

    asyncio.run(checks())

    print("\nPayments & results")
    if settings.payment_provider == "manual":
        if settings.manual_addresses:
            ok(f"manual payments · {len(settings.manual_addresses)} address(es)")
        else:
            warn("no payment addresses set — /buy is disabled, use /genkeys")
    else:
        if settings.nowpayments_api_key and settings.nowpayments_ipn_secret:
            ok("NOWPayments configured")
        else:
            bad("PAYMENT_PROVIDER=nowpayments but API key or IPN secret is missing")
        if not settings.payment_callback_url:
            bad("PAYMENT_CALLBACK_URL is required for automatic payments")
    if settings.results_chat_id:
        ok(f"results channel · {settings.results_chat_id}")
        if not settings.results_post_losses:
            warn("RESULTS_POST_LOSSES is off — a wins-only feed is not evidence")
    else:
        warn("no results channel configured")

    print()
    if problems:
        print(f"\033[31m{len(problems)} problem(s) must be fixed before deploying.\033[0m\n")
        return 1
    if warnings:
        print(f"\033[33mReady to deploy, with {len(warnings)} warning(s).\033[0m\n")
    else:
        print("\033[32mReady to deploy.\033[0m\n")
    return 0


COMMANDS = {
    "doctor": cmd_doctor,
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
