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

    settings = load_settings(require_bot=False)

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

    settings = load_settings(require_bot=False)

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
    from .safety import KillSwitch, measure_clock_drift

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
    ok("config valid")
    ok(f"mode · {settings.mode.label}")

    # The host and the mode are derived from the same value now, so they
    # cannot disagree -- but assert it rather than assume it, because this is
    # the check that would have caught the old two-switch arrangement.
    expected_demo_host = ".demo.kalshi.co" in settings.rest_base
    if expected_demo_host != settings.mode.uses_demo_host:
        bad(
            f"mode {settings.mode.value} does not match host {settings.rest_base} "
            "- refusing to start"
        )
    elif settings.mode.risks_real_money:
        warn("PRODUCTION-LIVE: orders will spend real money")

    # A key minted on one environment does not authenticate against the other,
    # and the failure arrives as a signature rejection mid-session rather than
    # at startup, so name it here instead.
    if settings.has_market_data_creds and settings.mode.uses_demo_host:
        warn(
            "platform key is used against the DEMO host - a production key "
            "will be rejected there"
        )

    drift = asyncio.run(measure_clock_drift(settings.rest_base + "/exchange/status"))
    if drift.drift_s is None:
        warn(f"clock not verified - {drift.detail}")
    elif drift.ok:
        ok(f"clock in sync - {drift.detail}")
    else:
        bad(f"CLOCK DRIFT - {drift.detail}; request signatures will be rejected")

    kill = KillSwitch(path=settings.kill_file)
    kill_state = kill.state()
    if kill_state.engaged:
        warn(f"kill switch {kill_state.describe()} - no orders will be placed")
    else:
        ok("kill switch clear")

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
        # Restated rather than counted. The checks above scroll off the top of
        # a terminal the moment the bot starts printing, and "1 problem(s)"
        # with the problem itself gone is the least useful thing this could
        # say to someone whose bot is not working.
        print(f"\033[31m{len(problems)} problem(s) must be fixed before deploying:\033[0m")
        for problem in problems:
            print(f"\033[31m  - {problem}\033[0m")
        print()
        return 1
    if warnings:
        print(f"\033[33mReady to deploy, with {len(warnings)} warning(s).\033[0m\n")
    else:
        print("\033[32mReady to deploy.\033[0m\n")
    return 0


def cmd_setup(*args) -> int:
    """Interactive first-time setup: write a working .env, validating as it goes.

    Everything that can be generated is generated. The three values that can
    only come from the operator's own accounts are prompted for, and each is
    checked against the live service before being written — a token that does
    not authenticate is caught here rather than on first boot.
    """
    import asyncio
    import os
    import pathlib
    import re
    import stat

    from cryptography.fernet import Fernet

    root = pathlib.Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    force = "--force" in args

    print("\n\033[1mDirectionalBot setup\033[0m")
    print("Writing " + str(env_path) + "\n")

    if env_path.exists() and not force:
        print("\033[33m.env already exists.\033[0m Re-run with --force to replace it,")
        print("or edit it by hand. Nothing was changed.\n")
        return 1

    def ask(prompt: str, *, secret: bool = False, allow_blank: bool = False) -> str:
        while True:
            try:
                value = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print("\n\nCancelled. Nothing was written.\n")
                raise SystemExit(130)
            if value or allow_blank:
                return value
            print("  \033[31mRequired.\033[0m")

    # ---- 1. Telegram bot token -------------------------------------------
    print("\033[1m1. Telegram bot token\033[0m")
    print("   Open https://t.me/BotFather → /newbot → copy the token.\n")
    token = ""
    username = ""
    while True:
        token = ask("   Token: ", secret=True)
        if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
            print("   \033[31mThat does not look like a bot token"
                  " (should be like 123456:AA...).\033[0m")
            continue
        print("   checking…", end=" ", flush=True)

        async def check() -> dict | None:
            from .telegram.api import TelegramClient, TelegramError

            tg = TelegramClient(token)
            try:
                return await tg.get_me()
            except TelegramError:
                return None
            except Exception:  # noqa: BLE001 - offline is not a bad token
                return {"username": "?", "offline": True}
            finally:
                await tg.aclose()

        me = asyncio.run(check())
        if me is None:
            print("\033[31mrejected by Telegram. Check the token.\033[0m")
            continue
        if me.get("offline"):
            print("\033[33mcould not reach Telegram; accepting it anyway.\033[0m")
            username = ""
        else:
            username = me.get("username") or ""
            print(f"\033[32mok — @{username}\033[0m")
        break

    # ---- 2. Admin Telegram user ID ---------------------------------------
    print("\n\033[1m2. Your Telegram user ID\033[0m")
    print("   Message https://t.me/userinfobot — it replies with your numeric ID.")
    print("   This is who can mint access keys and confirm payments.\n")
    while True:
        admin = ask("   Your user ID: ")
        if admin.isdigit():
            break
        print("   \033[31mIt is a number, e.g. 123456789.\033[0m")

    # ---- 3. Environment ---------------------------------------------------
    print("\n\033[1m3. Which Kalshi environment?\033[0m")
    print("   [1] Production — real markets (paper mode is still the default")
    print("       for every user, so nothing trades real money until you flip it)")
    print("   [2] Demo — Kalshi's sandbox\n")
    demo = ask("   Choose [1]: ", allow_blank=True) == "2"

    # ---- generated --------------------------------------------------------
    master_key = Fernet.generate_key().decode()

    lines = [
        "# Generated by `python -m kbot.tools setup`.",
        "# Never commit this file. It is already in .gitignore.",
        "",
        "TELEGRAM_BOT_TOKEN=" + token,
        "ADMIN_IDS=" + admin,
        "",
        "# Encrypts users' Kalshi private keys at rest.",
        "# BACK THIS UP. Losing it makes every stored credential unreadable.",
        "MASTER_KEY=" + master_key,
        "",
        "KALSHI_DEMO=" + ("true" if demo else "false"),
        "DB_PATH=./data/kbot.sqlite3",
        "",
        "# Optional: a platform Kalshi key used ONLY for the shared market-data",
        "# websocket, never to place an order. Without it the bot polls REST,",
        "# which works but is slower to see the book move.",
        "# KALSHI_API_KEY_ID=",
        "# KALSHI_PRIVATE_KEY_PATH=./secrets/market-data.pem",
        "",
        "# Optional: sell access for crypto. Without addresses, /buy is disabled",
        "# and you hand out keys yourself with /genkeys.",
        "PAYMENT_PROVIDER=manual",
        "# MANUAL_PAY_ADDRESSES={\"BTC\":\"bc1...\",\"USDT\":\"T...\"}",
        "",
        "# Optional: public results channel. Leave POST_LOSSES on — a wins-only",
        "# feed is not evidence.",
        "# RESULTS_CHAT_ID=@YourResultsChannel",
        "RESULTS_POST_LOSSES=true",
        "",
        "WEBHOOK_HOST=0.0.0.0",
        "WEBHOOK_PORT=8080",
        "WEBHOOK_PATH=/webhook/payment",
    ]
    if username:
        lines += ["", "BOT_USERNAME=" + username]

    env_path.write_text("\n".join(lines) + "\n")
    # Contains a bot token and the master key: owner-read only.
    os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
    (root / "data").mkdir(exist_ok=True)

    print("\n\033[32m✓ Wrote " + str(env_path) + " (chmod 600)\033[0m")
    print("\033[32m✓ Generated MASTER_KEY\033[0m")
    print("\033[32m✓ Created data/\033[0m")

    if username:
        site = root / "site" / "index.html"
        if site.exists():
            html = site.read_text()
            if 'const BOT = "YourBotUsername"' in html:
                site.write_text(
                    html.replace(
                        'const BOT = "YourBotUsername"', f'const BOT = "{username}"'
                    )
                )
                print(f"\033[32m✓ Pointed site/index.html at @{username}\033[0m")

    print("\n\033[1mBack up your MASTER_KEY now.\033[0m")
    print("It is the only thing that can decrypt your users' Kalshi keys.\n")
    print("Next:")
    print("  python -m kbot.tools doctor     # verify everything")
    print("  python -m kbot                  # start the bot")
    print("  then message your bot /start\n")
    return 0


COMMANDS = {
    "setup": cmd_setup,
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
