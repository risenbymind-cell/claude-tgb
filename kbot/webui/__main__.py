"""Run the desk: `python -m kbot.webui`.

Three shapes, and the flags exist to keep them distinct rather than to offer
choices:

* **local** (default) -- localhost only, no credential needed, because the
  operating system is the boundary.
* **`--phone`** -- your own Wi-Fi, guarded by a URL token. Fine behind a
  router, and it says plainly that it is not fine anywhere else.
* **hosted** -- a public address, guarded by a password and real sessions.
  Turned on by setting `DESK_PASSWORD` / `DESK_PASSWORD_HASH`; without one the
  server refuses to bind a public address at all.

`--sandbox` is orthogonal to all three: it welds the desk into paper mode so a
public instance can be handed to strangers.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys

from ..config import ConfigError, load_settings
from ..redact import install as install_redaction
from .auth import AuthError, hash_password, password_hash_from_env
from .desk import Desk, SandboxLocked
from .server import DeskServer, lan_address


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def run(args: argparse.Namespace) -> int:
    try:
        settings = load_settings(require_bot=False)
    except (ConfigError, RuntimeError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        password_hash = password_hash_from_env()
    except AuthError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    install_redaction(
        secrets={
            "MASTER_KEY": settings.master_key,
            "KALSHI_PRIVATE_KEY": settings.md_private_key,
            "DESK_PASSWORD": os.getenv("DESK_PASSWORD"),
        }
    )

    log = logging.getLogger("kbot.desk")
    sandbox = args.sandbox or _env_bool("DESK_SANDBOX")
    log.info("Mode: %s", settings.mode.label)
    log.info("Kalshi: %s", settings.rest_base)
    if sandbox:
        log.info("Sandbox: no credentials, no order path, simulated fills only.")

    try:
        desk = Desk(
            settings, size=args.size, target_c=args.target, sandbox=sandbox
        )
    except SandboxLocked as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    desk.strategy = args.strategy
    desk.start()

    # PORT is what every PaaS injects; honouring it is the difference between
    # "deploys" and "deploys after you read the docs".
    # PORT is injected by every PaaS, and an empty or malformed value should
    # not be an uncaught ValueError at boot.
    port = args.port
    if port is None:
        raw = (os.getenv("PORT") or "").strip()
        try:
            port = int(raw) if raw else 8787
        except ValueError:
            print(f"config error: PORT must be a number, got {raw!r}", file=sys.stderr)
            return 2
    host = "0.0.0.0" if (args.phone or args.host is None and password_hash) else args.host
    host = host or "127.0.0.1"

    try:
        server = DeskServer(
            desk, host=host, port=port,
            password_hash=password_hash,
            # A URL token and a password are two doors; when a password is
            # set the token is not minted, so there is only the strong one.
            allow_token_auth=args.phone,
            trust_proxy=_env_bool("DESK_TRUST_PROXY"),
        )
        await server.start()
    except RuntimeError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        await desk.stop()
        return 2

    print()
    if password_hash:
        print(f"  Desk running on port {port} with password authentication.")
        if not server.require_https:
            print("  ⚠ Not marked HTTPS-only. Put TLS in front of it and set")
            print("    DESK_TRUST_PROXY=1, or the password crosses the network")
            print("    in the clear.")
    elif args.phone:
        lan = lan_address()
        print("  Desk is on your network. Open this on your phone:")
        print(f"\n     {server.url(lan)}\n")
        print("  Same Wi-Fi as this computer. The token in that link is the")
        print("  only thing standing between the network and a port that can")
        print("  place orders -- treat the link as a password, and do not run")
        print("  --phone on a network you do not trust.")
        print("\n  On the phone: Share -> Add to Home Screen for a full-screen app.")
    else:
        print(f"  Desk running:  {server.url()}")
        print("  Add --phone to reach it from your phone on the same Wi-Fi.")
        print("  Set DESK_PASSWORD to host it somewhere public.")
    if sandbox:
        print("\n  SANDBOX: simulated fills only. No credentials, no order path.")
    print("\n  Ctrl+C to stop.\n")

    # A hosted app is redeployed by being sent SIGTERM, which happens while
    # the desk may be mid-order. Waiting on an event rather than being killed
    # lets the shutdown path run: the feed closes, storage is flushed, and an
    # in-flight submission finishes rather than being severed at the socket.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        log.info("Shutting down; closing the feed and flushing state.")
        await server.stop()
        await desk.stop()
        log.info("Desk stopped cleanly.")
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
    p.add_argument("command", nargs="?", default="serve",
                   choices=["serve", "hash-password"],
                   help="serve the desk, or print a password hash and exit")
    p.add_argument("--phone", action="store_true",
                   help="serve on the local network with a token, for a phone")
    p.add_argument("--host", default=None,
                   help="bind address; anything but localhost exposes order placement")
    p.add_argument("--port", type=int, default=None,
                   help="listen port (default: $PORT, else 8787)")
    p.add_argument("--sandbox", action="store_true",
                   help="weld the desk into paper mode: no credentials, no order path")
    p.add_argument("--strategy", default="reversion")
    p.add_argument("--size", type=int, default=10)
    p.add_argument("--target", type=int, default=15, help="exit target in cents")
    args = p.parse_args()

    if args.command == "hash-password":
        import getpass

        password = os.getenv("DESK_PASSWORD") or getpass.getpass("Desk password: ")
        if len(password) < 12:
            print("Use at least 12 characters.", file=sys.stderr)
            return 2
        # Printed alone on stdout so it can be captured directly:
        #   DESK_PASSWORD_HASH=$(python -m kbot.webui hash-password)
        print(hash_password(password))
        return 0

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
