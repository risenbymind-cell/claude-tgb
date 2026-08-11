"""Research CLI: `python -m kbot.research record | replay | sweep | days`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from ..config import load_settings
from .replay import ReplayConfig, replay
from .report import render, summarise
from .store import available_days

DEFAULT_DIR = Path("data/recordings")


def cmd_record(args: argparse.Namespace) -> int:
    from .recorder import Recorder

    settings = load_settings()
    coins = [c.strip().upper() for c in args.coins.split(",")] if args.coins else None
    recorder = Recorder(settings, args.dir, coins=coins, interval=args.interval)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, recorder.stop)
            except NotImplementedError:  # pragma: no cover - Windows
                pass
        print(
            f"Recording {', '.join(recorder.coins)} to {args.dir} "
            f"every {args.interval}s. Ctrl-C to stop."
        )
        await recorder.run(duration=args.duration)

    asyncio.run(run())
    print(f"\nWrote {recorder.writer.written:,} records to {args.dir}")
    return 0


def cmd_days(args: argparse.Namespace) -> int:
    days = available_days(args.dir)
    if not days:
        print(f"No recordings in {args.dir}. Run: python -m kbot.research record")
        return 1
    print(f"{len(days)} day(s) recorded in {args.dir}:")
    for day in days:
        print(f"  {day}")
    return 0


def _config_from(args: argparse.Namespace) -> ReplayConfig:
    return ReplayConfig(
        strategy=args.strategy,
        contracts=args.size,
        profit_cents=args.target,
        min_confidence=args.min_conf,
        min_entry_cents=args.min_entry,
        max_entry_cents=args.max_entry,
        enforce_fee_floor=not args.ignore_fee_floor,
    )


def cmd_replay(args: argparse.Namespace) -> int:
    coins = [c.strip().upper() for c in args.coins.split(",")] if args.coins else None
    result = replay(
        args.dir, _config_from(args), since=args.since, until=args.until, coins=coins
    )
    print(render(result))
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """Compare every strategy, and a range of targets, over the same data.

    Reading one backtest in isolation invites fooling yourself; a sweep shows
    whether a result is a peak or a plateau. A single strong cell surrounded by
    weak ones is almost always overfitting.
    """
    from ..strategy import REGISTRY

    coins = [c.strip().upper() for c in args.coins.split(",")] if args.coins else None
    targets = [int(t) for t in args.targets.split(",")]
    strategies = (
        [args.strategy] if args.strategy != "all" else list(REGISTRY)
    )

    print(f"{'strategy':<10}{'target':>8}{'trades':>8}{'win':>8}{'NET':>12}{'avg':>10}")
    print("─" * 56)
    for name in strategies:
        for target in targets:
            cfg = _config_from(args)
            cfg.strategy = name
            cfg.profit_cents = target
            result = replay(
                args.dir, cfg, since=args.since, until=args.until, coins=coins
            )
            stats = summarise(result.resolved, name)
            net = stats.net_dc / 1000
            avg = stats.avg_net_dc / 1000
            print(
                f"{name:<10}{f'+{target}c':>8}{stats.trades:>8}"
                f"{stats.win_rate:>7.1f}%{net:>+12.2f}{avg:>+10.3f}"
            )
    print("\nA single strong cell among weak ones is overfitting, not an edge.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m kbot.research")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="recordings dir")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="capture live order books to disk")
    rec.add_argument("--coins", help="comma separated, default all configured")
    rec.add_argument("--interval", type=float, default=1.0, help="seconds per sample")
    rec.add_argument("--duration", type=float, help="stop after N seconds")
    rec.set_defaults(func=cmd_record)

    sub.add_parser("days", help="list recorded days").set_defaults(func=cmd_days)

    def add_replay_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--strategy", default="drift")
        p.add_argument("--size", type=int, default=1, help="contracts per trade")
        p.add_argument("--target", type=int, default=8, help="exit at entry + N cents")
        p.add_argument("--min-conf", type=float, default=0.60)
        p.add_argument("--min-entry", type=int, default=25, help="cents")
        p.add_argument("--max-entry", type=int, default=65, help="cents")
        p.add_argument("--coins", help="comma separated")
        p.add_argument("--since", help="YYYY-MM-DD")
        p.add_argument("--until", help="YYYY-MM-DD")
        p.add_argument(
            "--ignore-fee-floor",
            action="store_true",
            help="do NOT raise targets that lose money after fees (for comparison)",
        )

    rep = sub.add_parser("replay", help="score a strategy over recorded data")
    add_replay_args(rep)
    rep.set_defaults(func=cmd_replay)

    sw = sub.add_parser("sweep", help="compare strategies and targets side by side")
    add_replay_args(sw)
    sw.add_argument("--targets", default="5,8,12,20", help="comma separated cents")
    sw.set_defaults(func=cmd_sweep)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
