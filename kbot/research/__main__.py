"""Research CLI: `python -m kbot.research record | replay | sweep | days`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from ..config import load_settings
from .calibrate import calibration
from .calibrate import render as render_calibration
from .replay import ReplayConfig, replay
from .report import render, summarise
from .store import available_days

DEFAULT_DIR = Path("data/recordings")


def cmd_search(args: argparse.Namespace) -> int:
    """Search the strategy grid, and price the search against noise."""
    import random

    from .search import all_plans, prepare, run_grid, shuffled

    markets = prepare(args.dir)
    plans = all_plans()
    if not markets:
        print("No settled markets recorded yet. Run `record` first.")
        return 1

    regimes: dict[str, int] = {}
    for m in markets:
        regimes[m.regime] = regimes.get(m.regime, 0) + 1

    print(f"\n{len(markets)} settled market(s) · {len(plans):,} plans")
    print("regimes: " + ", ".join(f"{k}={v}" for k, v in sorted(regimes.items())))

    # Said before the results, not after. A reader who scans the top of this
    # output and stops will otherwise see a 100% win rate presented as a
    # finding, and the caveat that unmakes it sits below the fold.
    from .signals import MIN_WINDOWS

    if len(markets) < MIN_WINDOWS:
        print()
        print("  " + "!" * 68)
        print(f"  {len(markets)} settled markets is below the {MIN_WINDOWS} needed for")
        print("  a search this size to mean anything. Searching 4,000 plans over")
        print("  a sample this small WILL produce excellent-looking winners --")
        print("  that is arithmetic, not discovery. The null calibration at the")
        print("  bottom is the only part of this output worth reading.")
        print("  " + "!" * 68)

    real = run_grid(markets, plans, args.min_trades)
    if not real:
        print(f"\nNo plan reached {args.min_trades} trades. Keep recording.")
        return 1
    real.sort(key=lambda r: (-r.win_rate, -r.net))

    print(f"\n{len(real):,} plans took at least {args.min_trades} trades\n")
    print(f"  {'plan':<50}{'n':>4}{'win%':>7}{'net$':>9}")
    print("  " + "-" * 68)
    for r in real[:10]:
        print(f"  {r.plan.name():<50}{r.trades:>4}{100*r.win_rate:>7.0f}{r.net:>9.2f}")

    # The null pass. Without it none of the above means anything.
    rng = random.Random(args.seed)
    best_wr, best_net, hi_counts = [], [], []
    for _ in range(args.trials):
        res = run_grid(shuffled(markets, rng), plans, args.min_trades)
        if not res:
            continue
        best_wr.append(max(r.win_rate for r in res))
        best_net.append(max(r.net for r in res))
        hi_counts.append(sum(1 for r in res if r.win_rate >= 0.90))

    if not best_wr:
        print("\nCould not build a null distribution.")
        return 1

    rw = real[0].win_rate
    rn = max(r.net for r in real)
    r_hi = sum(1 for r in real if r.win_rate >= 0.90)
    n_wr = sum(best_wr) / len(best_wr)
    n_net = sum(best_net) / len(best_net)
    n_hi = sum(hi_counts) / len(hi_counts)
    beat_wr = sum(1 for b in best_wr if b >= rw)
    beat_net = sum(1 for b in best_net if b >= rn)

    print(f"\n  Null calibration · {args.trials} shuffles of the settlements")
    print("  " + "-" * 68)
    print(f"  {'':<22}{'best win%':>12}{'best net$':>12}{'plans >=90%':>14}")
    print(f"  {'REAL settlements':<22}{100*rw:>12.0f}{rn:>12.2f}{r_hi:>14}")
    print(f"  {'SHUFFLED (mean)':<22}{100*n_wr:>12.0f}{n_net:>12.2f}{n_hi:>14.1f}")
    print()
    print(f"  Shuffles whose best beat the real best win rate: "
          f"{beat_wr}/{len(best_wr)}")
    print(f"  Shuffles whose best beat the real best net:      "
          f"{beat_net}/{len(best_net)}")
    print()
    if beat_wr > len(best_wr) * 0.05 or beat_net > len(best_net) * 0.05:
        print("  VERDICT: nothing here is distinguishable from the search itself.")
        print("  Shuffling the outcomes destroys every possible edge, yet the grid")
        print("  still produces winners just as good. That is what searching a")
        print("  large space over few markets buys you, and it is worth nothing.")
        print("  The fix is more settled markets, not more plans.")
        # Exit 2, so `search && something` cannot proceed on noise. A command
        # that prints "worth nothing" and then reports success to the shell is
        # inviting exactly the automation this whole pass exists to prevent.
        return 2

    print("  The best real plan beats what the same search extracts from")
    print("  noise. That is necessary, not sufficient: confirm it on a")
    print("  period this search never saw before believing it.")
    if len(markets) < MIN_WINDOWS:
        print()
        print(f"  Note: this passed on {len(markets)} markets, below the "
              f"{MIN_WINDOWS} threshold.")
        print("  Passing a null test on a small sample is itself a coin flip.")
        return 2
    return 0


def cmd_signals(args: argparse.Namespace) -> int:
    """Score every candidate predictor before any of them becomes a strategy."""
    from .signals import analyse, format_analysis

    horizons = [float(h) for h in args.horizons.split(",") if h.strip()]
    coins = [c.strip().upper() for c in args.coins.split(",")] if args.coins else None
    for horizon in horizons:
        analysis = analyse(
            args.dir,
            horizon=horizon,
            coins=coins,
            since=args.since,
            until=args.until,
        )
        print(format_analysis(analysis, buckets=args.buckets))
        print()
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    from .recorder import Recorder

    settings = load_settings(require_bot=False)
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


def cmd_inventory(args: argparse.Namespace) -> int:
    """What is recorded, and what it can support.

    Exists because every other command here refuses to answer on a thin
    sample, and a refusal tells you nothing about how far off you are.
    """
    from .inventory import format_inventory, take_inventory

    inv = take_inventory(args.dir)
    print(format_inventory(inv))
    # Non-zero while the data cannot support a verdict, so this is usable as
    # a gate in a script: run the search only when it would mean something.
    return 0 if inv.enough_for_a_verdict else 1


def _config_from(args: argparse.Namespace) -> ReplayConfig:
    return ReplayConfig(
        strategy=args.strategy,
        contracts=args.size,
        profit_cents=args.target,
        min_confidence=args.min_conf,
        min_entry_cents=args.min_entry,
        max_entry_cents=args.max_entry,
        enforce_fee_floor=not args.ignore_fee_floor,
        stop_cents=getattr(args, "stop", None),
        flatten_before_close_s=getattr(args, "flatten_before", 45.0),
        max_hold_s=getattr(args, "max_hold", None),
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


def cmd_tune(args: argparse.Namespace) -> int:
    """Search for a configuration that hits a target win rate — and price it.

    Win rate on its own is the easiest number in trading to manufacture: shrink
    the profit target and almost every trade closes green, while the rare loser
    gives back the whole stake at expiry. This searches for the requested win
    rate and then shows what it actually earns, because the two questions have
    very different answers.
    """
    from ..strategy import REGISTRY

    coins = [c.strip().upper() for c in args.coins.split(",")] if args.coins else None
    strategies = [args.strategy] if args.strategy != "all" else list(REGISTRY)
    targets = [int(t) for t in args.targets.split(",")]
    confidences = [float(c) for c in args.confs.split(",")]

    rows = []
    for name in strategies:
        for target in targets:
            for conf in confidences:
                cfg = _config_from(args)
                cfg.strategy = name
                cfg.profit_cents = target
                cfg.min_confidence = conf
                res = replay(
                    args.dir, cfg, since=args.since, until=args.until, coins=coins
                )
                stats = summarise(res.resolved, name)
                if stats.trades < args.min_trades:
                    continue
                rows.append((name, target, conf, stats))

    if not rows:
        print(f"No configuration produced at least {args.min_trades} trades.")
        print("Record more data, or lower --min-trades.")
        return 1

    goal = args.win_rate
    metric = (lambda st: st.hit_rate) if args.by_hit_rate else (lambda st: st.win_rate)
    hits = [r for r in rows if metric(r[3]) >= goal]

    # Two different "win rates", and the gap between them is the whole point:
    #   hit%  — the trade reached its exit target (what a vendor screenshots)
    #   win%  — the trade actually made money after fees (what pays you)
    print(f"{'strategy':<9}{'target':>7}{'conf':>7}{'trades':>8}"
          f"{'hit%':>7}{'win%':>7}{'NET':>11}{'avg/trade':>11}")
    print("─" * 68)

    def show(row, mark=" "):
        name, target, conf, st = row
        print(
            f"{mark}{name:<8}{f'+{target}c':>7}{conf:>7.2f}{st.trades:>8}"
            f"{st.hit_rate:>6.1f}%{st.win_rate:>6.1f}%{st.net_dc / 1000:>+11.2f}"
            f"{st.avg_net_dc / 1000:>+11.3f}"
        )

    if hits:
        print(f"\n≥{goal:.0f}% WIN RATE — {len(hits)} configuration(s):\n")
        for row in sorted(hits, key=lambda r: -metric(r[3]))[:10]:
            show(row, "★" if row[3].net_dc > 0 else "✗")
        profitable = [r for r in hits if r[3].net_dc > 0]
        print(
            f"\n  of those, {len(profitable)} actually made money."
            if profitable
            else "\n  ✗ none of them made money."
        )
    else:
        best = max(rows, key=lambda r: metric(r[3]))
        label = "target-hit rate" if args.by_hit_rate else "win rate (net > 0)"
        print(f"\nNothing reached {goal:.0f}% on {label}. Best found:\n")
        show(best)

    print("\nRanked by NET instead — the number that pays you:\n")
    for row in sorted(rows, key=lambda r: -r[3].net_dc)[:5]:
        show(row, "★" if row[3].net_dc > 0 else " ")

    best_net = max(rows, key=lambda r: r[3].net_dc)
    best_hit = max(rows, key=lambda r: r[3].hit_rate)
    best_win = max(rows, key=lambda r: r[3].win_rate)

    print(
        f"\nHighest target-hit rate: {best_hit[3].hit_rate:.1f}% hit, but only "
        f"{best_hit[3].win_rate:.1f}% made money → net "
        f"{best_hit[3].net_dc / 1000:+.2f}"
    )
    print(
        f"Highest true win rate:   {best_win[3].win_rate:.1f}% → net "
        f"{best_win[3].net_dc / 1000:+.2f}"
    )
    print(
        f"Highest NET:             {best_net[3].win_rate:.1f}% win → net "
        f"{best_net[3].net_dc / 1000:+.2f}"
    )

    gap = best_hit[3].hit_rate - best_hit[3].win_rate
    if gap > 10:
        print(
            f"\nThat {gap:.0f}-point gap is the fee. {best_hit[3].hit_rate:.0f}% of"
            "\nthose trades reached their exit target and still lost money,"
            "\nbecause the target was smaller than the round trip cost."
            "\nA screenshot of the hit rate would be true and worthless."
        )
    if best_win[3].net_dc < best_net[3].net_dc:
        print(
            "\nOptimise NET, then report whatever win rate honestly comes with"
            "\nit. Tuning toward a win-rate number selects for small targets"
            "\nthat hand the stake back at expiry."
        )
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    coins = [c.strip().upper() for c in args.coins.split(",")] if args.coins else None
    rows = calibration(
        args.dir,
        since=args.since,
        until=args.until,
        coins=coins,
        buckets=args.buckets,
        sample_at_s=args.at,
    )
    # Outcome balance is computed here rather than inside the renderer: the
    # renderer receives buckets, which have already lost the per-market
    # settlement that the balance check needs.
    from collections import Counter

    from .store import load_session

    _, settled = load_session(args.dir, args.since, args.until)
    outcomes = Counter(v for v in settled.values() if v in ("yes", "no"))
    print(render_calibration(rows, outcomes=dict(outcomes)))
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

    sub.add_parser(
        "inventory",
        help="what is recorded, and whether it can support a verdict yet",
    ).set_defaults(func=cmd_inventory)

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
        p.add_argument("--stop", type=int, default=None,
                       help="cents below entry to cut the position")
        p.add_argument("--flatten-before", type=float, default=45.0,
                       help="seconds before close to flatten regardless")
        p.add_argument("--max-hold", type=float, default=None,
                       help="seconds after which a stalled position is closed")
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

    tu = sub.add_parser("tune", help="search for a target win rate, and price it")
    add_replay_args(tu)
    tu.add_argument("--win-rate", type=float, default=92.0, help="target win %%")
    tu.add_argument("--targets", default="1,2,3,5,8,12,20,30")
    tu.add_argument("--confs", default="0.35,0.45,0.55,0.65")
    tu.add_argument("--min-trades", type=int, default=20)
    tu.add_argument(
        "--by-hit-rate",
        action="store_true",
        help="search on target-hit rate rather than trades that actually profited",
    )
    tu.set_defaults(func=cmd_tune)

    se = sub.add_parser(
        "search", help="search thousands of plans, and price the search vs noise"
    )
    se.add_argument("--min-trades", type=int, default=5)
    se.add_argument("--trials", type=int, default=12,
                    help="shuffled repeats used to build the null distribution")
    se.add_argument("--seed", type=int, default=20260813)
    se.set_defaults(func=cmd_search)

    sg = sub.add_parser(
        "signals", help="does any book feature predict the next move? run this first"
    )
    sg.add_argument("--coins")
    sg.add_argument("--since")
    sg.add_argument("--until")
    sg.add_argument(
        "--horizons", default="15,30,60,120", help="forward horizons in seconds"
    )
    sg.add_argument("--buckets", type=int, default=5)
    sg.set_defaults(func=cmd_signals)

    ca = sub.add_parser(
        "calibrate", help="does the price predict the outcome? where an edge would live"
    )
    ca.add_argument("--coins")
    ca.add_argument("--since")
    ca.add_argument("--until")
    ca.add_argument("--buckets", type=int, default=10)
    ca.add_argument(
        "--at", type=float, default=450.0, help="seconds before close to sample"
    )
    ca.set_defaults(func=cmd_calibrate)
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
