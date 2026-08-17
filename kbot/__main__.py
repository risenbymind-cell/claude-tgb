"""Entry point: `python -m kbot`."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .config import ConfigError, load_settings
from .lock import AlreadyRunning, InstanceLock
from .telegram.api import TelegramError
from .redact import install as install_redaction
from .storage import Storage
from .telegram.bot import Bot


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    # Attached before anything can log. httpx at INFO writes the request URL,
    # and the Telegram token lives inside that URL -- so the leak that matters
    # most is one no line in this project writes.
    install_redaction()


#: Exit code for a configuration problem. systemd is told not to restart on
#: this one, so a typo in .env stops the service instead of crash-looping.
EXIT_CONFIG = 2


async def amain(settings) -> int:
    # Before anything opens a socket or reads a market. Two instances against
    # one account double every position and neither notices, because each
    # one's own ledger balances perfectly.
    lock = InstanceLock.for_data_dir(
        settings.db_path, places_real_orders=settings.places_real_orders
    )
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_CONFIG

    storage = Storage(settings.db_path, settings.master_key)
    bot = Bot(settings, storage)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    runner = asyncio.create_task(bot.run())
    waiter = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait(
        {runner, waiter}, return_when=asyncio.FIRST_COMPLETED
    )

    if runner in done:
        waiter.cancel()
        exc = runner.exception()
        if exc:
            log = logging.getLogger("kbot")
            # Telegram rejecting the token is not something a restart fixes,
            # and looping on it gets the token rate-limited. Report it the way
            # a bad .env is reported -- one actionable line, exit 2, which the
            # supervisors are told not to restart on.
            if isinstance(exc, TelegramError) and exc.code in (401, 404):
                log.error(
                    "Telegram rejected the bot token (%s). The token in .env is "
                    "wrong, revoked, or belongs to a deleted bot. Get a fresh "
                    "one from @BotFather with /mybots -> API Token, put it in "
                    ".env as TELEGRAM_BOT_TOKEN, and start again.",
                    exc.description,
                )
                storage.close()
                lock.release()
                return EXIT_CONFIG
            log.error("Bot exited: %s", exc)
            storage.close()
            lock.release()
            return 1
    else:
        logging.getLogger("kbot").info("Shutting down…")
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass

    storage.close()
    lock.release()
    return 0


def main() -> int:
    configure_logging()
    # Config is resolved before anything starts, so a bad value is one clear
    # line rather than a stack trace from somewhere deep in a library.
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except RuntimeError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    # Now that the real values are known, register them for exact-match
    # redaction. The pattern rules above already covered their shapes; this
    # catches anything that does not match a pattern.
    install_redaction(
        secrets={
            "TELEGRAM_TOKEN": settings.telegram_token,
            "MASTER_KEY": settings.master_key,
            "KALSHI_PRIVATE_KEY": settings.md_private_key,
        }
    )

    log = logging.getLogger("kbot")
    log.info(
        "Starting · %s · %d coin(s) · payments=%s · results channel=%s",
        settings.mode.label,
        len(settings.series),
        settings.payment_provider,
        "on" if settings.results_chat_id else "off",
    )
    if not settings.has_market_data_creds:
        log.info("No platform Kalshi key: market data will use REST polling.")

    try:
        return asyncio.run(amain(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
