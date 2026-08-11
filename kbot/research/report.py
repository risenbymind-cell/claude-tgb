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
    lines += [
        "",
        " Backtest caveats: no market-impact model, exits require a bid at or",
        " above target, and past behaviour is not future behaviour.",
        "═" * 84,
    ]
    return "\n".join(lines)
