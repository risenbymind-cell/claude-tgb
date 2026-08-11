"""Entry point: `python -m kbot`."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .config import ConfigError, load_settings
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


#: Exit code for a configuration problem. systemd is told not to restart on
#: this one, so a typo in .env stops the service instead of crash-looping.
EXIT_CONFIG = 2


async def amain(settings) -> int:
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
            logging.getLogger("kbot").error("Bot exited: %s", exc)
            storage.close()
            return 1
    else:
        logging.getLogger("kbot").info("Shutting down…")
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass

    storage.close()
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

    log = logging.getLogger("kbot")
    log.info(
        "Starting · %s · %d coin(s) · payments=%s · results channel=%s",
        "DEMO" if settings.demo else "PRODUCTION",
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
