"""Turn a replay into numbers you can make a decision with.

Every figure is net of fees. Gross is shown alongside so the fee drag is
visible rather than assumed, because on these markets it is most of the
difference between a strategy that looks good and one that is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..kalshi.prices import format_dollars
from .replay import ReplayResult, ReplayTrade


@dataclass
class Stats:
    label: str
    trades: int
    wins: int
    gross_dc: int
    fees_dc: int
    net_dc: int
    best_dc: int
    worst_dc: int
    max_drawdown_dc: int
    target_hits: int
    settled: int

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0

    @property
    def avg_net_dc(self) -> float:
        return self.net_dc / self.trades if self.trades else 0.0

    @property
    def hit_rate(self) -> float:
        """Share of trades that reached the target rather than going to expiry."""
        return self.target_hits / self.trades * 100 if self.trades else 0.0


def summarise(trades: list[ReplayTrade], label: str) -> Stats:
    resolved = [t for t in trades if t.resolved]
    if not resolved:
        return Stats(label, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    nets = [t.net_dc for t in resolved]
    equity = 0
    peak = 0
    drawdown = 0
    for net in nets:
        equity += net
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)

    return Stats(
        label=label,
        trades=len(resolved),
        wins=len([n for n in nets if n > 0]),
        gross_dc=sum(t.gross_dc for t in resolved),
        fees_dc=sum(t.entry_fee_dc + t.exit_fee_dc for t in resolved),
        net_dc=sum(nets),
        best_dc=max(nets),
        worst_dc=min(nets),
        max_drawdown_dc=drawdown,
        target_hits=len([t for t in resolved if t.outcome == "target"]),
        settled=len([t for t in resolved if t.outcome == "settled"]),
    )


def trades_needed(nets: list[int], detect_dc: float | None = None) -> tuple[int, float]:
    """How many trades before an edge this size is distinguishable from noise.

    Standard power calculation: at 95% confidence and 80% power,
    n ≈ 7.85 · σ² / μ². Returns (trades_needed, per_trade_edge_being_tested).

    This is the honest answer to "is it profitable?" — the question cannot be
    settled by watching it for an afternoon. Per-trade P/L on these markets has
    enormous variance relative to its mean, because most trades clip a few
    cents and a few give back the whole stake, so it takes a lot of trades
    before the average means anything.
    """
    if len(nets) < 2:
        return 0, 0.0
    mean = sum(nets) / len(nets)
    var = sum((n - mean) ** 2 for n in nets) / (len(nets) - 1)
    # Default to detecting an edge the size of what has been observed; if that
    # is ~0, fall back to a practically interesting 1c per contract.
    target = detect_dc if detect_dc is not None else abs(mean)
    if target < 1:
        target = 10.0
    if var <= 0:
        return 0, target
    return int(round(7.85 * var / (target**2))), target


def _signed(dc: int) -> str:
    return f"{'+' if dc >= 0 else '-'}${format_dollars(abs(dc))}"


def _row(s: Stats) -> str:
    return (
        f"{s.label:<12}{s.trades:>7}{s.win_rate:>8.1f}%"
        f"{_signed(s.gross_dc):>12}{'$' + format_dollars(s.fees_dc):>10}"
        f"{_signed(s.net_dc):>12}{_signed(int(s.avg_net_dc)):>10}"
        f"{'$' + format_dollars(s.max_drawdown_dc):>11}"
    )


HEADER = (
    f"{'':<12}{'trades':>7}{'win':>9}{'gross':>12}{'fees':>10}"
    f"{'NET':>12}{'avg':>10}{'maxDD':>11}"
)


def render(result: ReplayResult) -> str:
    """A full report: overall, per coin, and the caveats that apply to it."""
    cfg = result.config
    lines = [
        "═" * 84,
        f" REPLAY · strategy={cfg.strategy} · size={cfg.contracts} "
        f"· target=+{cfg.profit_cents}c · min-conf={cfg.min_confidence:.0%}",
        "═" * 84,
        f" {result.windows_seen} market windows · {result.snapshots:,} snapshots "
        f"· {len(result.trades)} trades taken",
        "",
    ]

    resolved = result.resolved
    if not resolved:
        lines += [
            " No resolved trades.",
            "",
            " Either the filters never matched, or no recorded window settled.",
            " Record for longer, or loosen --min-conf / the entry band.",
        ]
        return "\n".join(lines)

    overall = summarise(resolved, "ALL")
    lines += [HEADER, "─" * 84, _row(overall), ""]

    by_coin: dict[str, list[ReplayTrade]] = {}
    for t in resolved:
        by_coin.setdefault(t.coin, []).append(t)
    if len(by_coin) > 1:
        lines.append(" By coin")
        lines.append("─" * 84)
        for coin in sorted(by_coin, key=lambda c: -summarise(by_coin[c], c).net_dc):
            lines.append(_row(summarise(by_coin[coin], coin)))
        lines.append("")

    lines += [
        " Outcomes",
        "─" * 84,
        f" target hit  {overall.target_hits:>5}  ({overall.hit_rate:.0f}% of trades)",
        f" held to expiry {overall.settled:>2}",
        f" best {_signed(overall.best_dc)} · worst {_signed(overall.worst_dc)}"
        f" · max drawdown ${format_dollars(overall.max_drawdown_dc)}",
    ]

    if result.unresolved:
        lines += [
            "",
            f" ⚠ {len(result.unresolved)} trade(s) excluded: the market's settlement",
            "   was never recorded, so they cannot be scored honestly.",
        ]

    # The number that actually decides it.
    edge = overall.net_dc / overall.trades
    lines += [
        "",
        "─" * 84,
        f" Edge per trade: {_signed(int(edge))} net of fees, over "
        f"{overall.trades} trades.",
    ]
    # A rough significance read: is the mean plausibly just noise?
    nets = [t.net_dc for t in resolved]
    if len(nets) > 1:
        mean = sum(nets) / len(nets)
        var = sum((n - mean) ** 2 for n in nets) / (len(nets) - 1)
        stderr = math.sqrt(var / len(nets)) if var > 0 else 0.0
        if stderr > 0:
            t_stat = mean / stderr
            verdict = (
                "plausibly real, keep testing"
                if abs(t_stat) > 2
                else "indistinguishable from noise at this sample size"
            )
            lines.append(f" t ≈ {t_stat:+.2f} — {verdict}.")
    # How much more data would settle it.
    needed, target = trades_needed(nets)
    if needed:
        lines += ["", "─" * 84]
        if needed <= overall.trades:
            lines.append(
                f" Sample size: {overall.trades} trades is enough to resolve an edge"
                f" of {_signed(int(target))} per trade."
            )
        else:
            short = needed - overall.trades
            # Rate must come from windows, not wall-clock. The bot takes at
            # most one trade per market window, and many markets run
            # concurrently — extrapolating from elapsed time across
            # simultaneous windows overstates the rate by an order of
            # magnitude.
            coins = len({t.coin for t in resolved})
            per_window = (
                overall.trades / result.windows_seen if result.windows_seen else 0.0
            )
            windows_per_day = 96 * max(1, coins)  # four 15-min windows an hour
            per_day = per_window * windows_per_day
            days = short / per_day if per_day > 0 else float("inf")
            lines += [
                f" Sample size: NOT ENOUGH. To tell an edge of {_signed(int(target))}"
                f" per trade",
                f" from noise you need about {needed} resolved trades — "
                f"{short} more than you have.",
            ]
            if per_day > 0 and days < 3650:
                lines.append(
                    f" At {per_window:.2f} trades per window across {coins} coin(s)"
                    f" — about {per_day:.0f}/day —"
                )
                lines.append(
                    f" that is roughly {days:.1f} more days of recording."
                )
                # The number above tests an edge as large as the current noise,
                # which flatters the timeline. A real edge worth trading is
                # usually smaller, and costs far more data to establish.
                modest_dc = 100.0  # 10c per trade
                n_modest, _ = trades_needed(nets, detect_dc=modest_dc)
                if n_modest > needed and per_day > 0:
                    lines.append(
                        f" To resolve a subtler edge of {_signed(int(modest_dc))}"
                        f" per trade: ~{n_modest} trades"
                        f" ({n_modest / per_day:.1f} days)."
                    )

    lines += [
        "",
        " Backtest caveats: no market-impact model, exits require a bid at or",
        " above target, and past behaviour is not future behaviour.",
        "═" * 84,
    ]
    return "\n".join(lines)
