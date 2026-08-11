"""Entry point: `python -m kbot`."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .config import load_settings
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


async def amain() -> int:
    settings = load_settings()
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
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
