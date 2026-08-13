"""Run the desk: `python -m kbot.webui`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from ..config import ConfigError, load_settings
from ..redact import install as install_redaction
from .desk import Desk
from .server import DeskServer


async def run(args: argparse.Namespace) -> int:
    try:
        settings = load_settings(require_bot=False)
    except (ConfigError, RuntimeError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    install_redaction(
        secrets={
            "MASTER_KEY": settings.master_key,
            "KALSHI_PRIVATE_KEY": settings.md_private_key,
        }
    )

    log = logging.getLogger("kbot.desk")
    log.info("Mode: %s", settings.mode.label)
    log.info("Kalshi: %s", settings.rest_base)

    desk = Desk(settings, size=args.size, target_c=args.target)
    desk.strategy = args.strategy
    desk.start()

    server = DeskServer(desk, host=args.host, port=args.port)
    await server.start()
    print(f"\n  Desk running:  http://{args.host}:{args.port}\n  Ctrl+C to stop.\n")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()
        await desk.stop()
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    p = argparse.ArgumentParser(prog="python -m kbot.webui")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address; anything but localhost exposes order placement")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--strategy", default="reversion")
    p.add_argument("--size", type=int, default=10)
    p.add_argument("--target", type=int, default=15, help="exit target in cents")
    args = p.parse_args()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
