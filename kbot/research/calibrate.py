"""Is the market priced correctly?

The single most useful question you can ask of recorded data. For every market
observed trading at price P, what fraction actually settled YES?

In an efficiently priced market the answer tracks the diagonal: contracts at 30c
settle yes about 30% of the time, contracts at 70c about 70%. A directional
strategy has no edge there — buying at 70c wins 70% of the time and loses the
other 30%, and after fees that is a slow loss.

An edge lives in the *gap*. If contracts trading at 70c settle yes 78% of the
time, that band is underpriced and buying it is genuinely positive expectancy.
The size of that gap, and whether it survives fees, is the whole question.

This is also the fastest way to catch a broken backtest. Synthetic data
generated from a random walk will show wild miscalibration — 70c buckets
settling yes 98% of the time — because the generator's "price" and its
settlement come from the same series. A strategy that looks brilliant on data
like that has discovered the generator, not the market.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..kalshi.fees import fee_dc
from .store import load_session


@dataclass
class Bucket:
    low_dc: int
    high_dc: int
    observations: int
    settled_yes: int

    @property
    def mid_dc(self) -> int:
        return (self.low_dc + self.high_dc) // 2

    @property
    def implied(self) -> float:
        """What the price says the probability is."""
        return self.mid_dc / 1000

    @property
    def actual(self) -> float:
        """What actually happened."""
        return self.settled_yes / self.observations if self.observations else 0.0

    @property
    def edge(self) -> float:
        """Actual minus implied. Positive means YES is underpriced here."""
        return self.actual - self.implied

    @property
    def stderr(self) -> float:
        """Standard error of the observed rate — how much to trust it."""
        if self.observations < 2:
            return float("inf")
        p = self.actual
        return math.sqrt(max(p * (1 - p), 1e-9) / self.observations)

    @property
    def significant(self) -> bool:
        """Is the gap bigger than two standard errors?"""
        return self.stderr != float("inf") and abs(self.edge) > 2 * self.stderr

    def net_per_contract_dc(self) -> float:
        """Expected value of buying one YES here and holding to settlement."""
        cost = self.mid_dc + fee_dc(1, self.mid_dc)
        return self.actual * 1000 - cost


def calibration(
    directory,
    *,
    since: str | None = None,
    until: str | None = None,
    coins=None,
    buckets: int = 10,
    sample_at_s: float = 450.0,
) -> list[Bucket]:
    """Bucket markets by their price mid-window and score against settlement.

    One observation per market, taken at a fixed point in the window, so a
    market that sat still for ten minutes does not count ten times and drown
    out the ones that moved.
    """
    books, settled = load_session(directory, since, until)
    wanted = {c.upper() for c in coins} if coins else None

    width = 1000 // buckets
    counts = [[0, 0] for _ in range(buckets)]

    for ticker, records in books.items():
        result = settled.get(ticker)
        if result not in {"yes", "no"}:
            continue
        if wanted and records[0].coin.upper() not in wanted:
            continue

        # The observation closest to the chosen point in the window.
        pick = min(records, key=lambda r: abs(r.seconds_to_close - sample_at_s))
        if abs(pick.seconds_to_close - sample_at_s) > 120:
            continue
        yes_bids = [int(p) for p, _ in pick.yes]
        no_bids = [int(p) for p, _ in pick.no]
        if not yes_bids or not no_bids:
            continue
        mid = (max(yes_bids) + (1000 - max(no_bids))) / 2

        index = min(buckets - 1, max(0, int(mid // width)))
        counts[index][0] += 1
        if result == "yes":
            counts[index][1] += 1

    return [
        Bucket(i * width, (i + 1) * width, obs, yes)
        for i, (obs, yes) in enumerate(counts)
        if obs > 0
    ]


def render(rows: list[Bucket], min_observations: int = 5) -> str:
    if not rows:
        return (
            "No settled markets in this recording.\n\n"
            "Calibration needs windows that both opened and settled while the\n"
            "recorder was running. Record for a few hours and try again."
        )

    lines = [
        "═" * 76,
        " CALIBRATION — does the price predict the outcome?",
        "═" * 76,
        f"{'price band':<14}{'markets':>9}{'implied':>10}{'actual':>9}"
        f"{'gap':>9}{'EV/contract':>14}",
        "─" * 76,
    ]
    total = sum(r.observations for r in rows)
    for row in rows:
        thin = row.observations < min_observations
        flag = "  (thin)" if thin else ("  *" if row.significant else "")
        lines.append(
            f"{f'{row.low_dc//10}-{row.high_dc//10}c':<14}{row.observations:>9}"
            f"{row.implied:>9.0%}{row.actual:>9.0%}{row.edge:>+9.0%}"
            f"{row.net_per_contract_dc()/1000:>+14.3f}{flag}"
        )

    lines += ["─" * 76, f" {total} settled market(s). * = gap beyond 2 standard errors."]

    solid = [r for r in rows if r.observations >= min_observations]
    if not solid:
        lines.append(
            "\n Every bucket is thin. Nothing here is evidence yet — keep recording."
        )
        return "\n".join(lines)

    mean_abs_gap = sum(abs(r.edge) for r in solid) / len(solid)
    lines.append(f"\n Mean absolute gap: {mean_abs_gap:.1%}")
    if mean_abs_gap > 0.15:
        lines += [
            "",
            " ⚠ That is a very large gap. On real market data it would be an",
            "   extraordinary finding; on synthetic data it usually means the",
            "   generator's price and its settlement share a source, and any",
            "   strategy tested on it has learned the generator, not a market.",
        ]
    elif mean_abs_gap < 0.05:
        lines += [
            "",
            " The market is close to correctly priced. A directional strategy",
            " has no free edge here — anything it earns must come from timing",
            " inside the window, and must clear the fee twice.",
        ]

    best = max(solid, key=lambda r: r.net_per_contract_dc())
    if best.net_per_contract_dc() > 0 and best.significant:
        lines += [
            "",
            f" Best band: {best.low_dc//10}-{best.high_dc//10}c settles yes "
            f"{best.actual:.0%} against {best.implied:.0%} implied,",
            f" worth {best.net_per_contract_dc()/1000:+.3f} per contract after fees.",
            " Verify on fresh data before believing it.",
        ]
    else:
        lines += [
            "",
            " No band shows a statistically solid, fee-clearing edge in this data.",
        ]
    lines.append("═" * 76)
    return "\n".join(lines)
