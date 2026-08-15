"""What the recordings actually contain, and what they can support.

Every statistical tool in this package refuses to answer when the sample is
too small -- `signals` withholds a verdict below 200 windows, `calibrate`
withholds one on a tape that settled mostly one way. Those refusals are the
most valuable thing the tooling does, and they are also invisible: you run the
command, it says NOT ENOUGH DATA, and nothing tells you how far off you are or
when to try again.

This turns that into a number. It counts what has been recorded, works out how
much of it is usable, and says plainly how much more is needed before any
verdict would mean anything -- so the answer to "is there an edge yet" is a
date rather than a shrug.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .signals import MIN_WINDOWS
from .store import available_days, load_session

#: A settled market is one window with a known outcome -- the unit every
#: measurement here is denominated in. Books without a settlement are still
#: useful for describing the market, but they cannot score a prediction.
#:
#: Kalshi opens four 15-minute windows an hour per coin, so one coin recorded
#: continuously yields 96 settled windows a day.
WINDOWS_PER_COIN_PER_DAY = 96

#: Below this share, a sample is one directional move counted many times
#: rather than many independent observations. Mirrors calibrate's own guard.
ONE_SIDED_SHARE = 0.75


@dataclass
class CoinInventory:
    coin: str
    windows: int = 0
    settled: int = 0
    yes: int = 0
    no: int = 0
    snapshots: int = 0

    @property
    def one_sided_share(self) -> float:
        if self.settled == 0:
            return 0.0
        return max(self.yes, self.no) / self.settled


@dataclass
class Inventory:
    days: list[str] = field(default_factory=list)
    coins: dict[str, CoinInventory] = field(default_factory=dict)
    directory: str = ""

    # ---------------- totals ----------------

    @property
    def settled(self) -> int:
        return sum(c.settled for c in self.coins.values())

    @property
    def windows(self) -> int:
        return sum(c.windows for c in self.coins.values())

    @property
    def snapshots(self) -> int:
        return sum(c.snapshots for c in self.coins.values())

    @property
    def yes(self) -> int:
        return sum(c.yes for c in self.coins.values())

    @property
    def no(self) -> int:
        return sum(c.no for c in self.coins.values())

    @property
    def one_sided_share(self) -> float:
        if self.settled == 0:
            return 0.0
        return max(self.yes, self.no) / self.settled

    # ---------------- what it can support ----------------

    @property
    def enough_for_a_verdict(self) -> bool:
        return self.settled >= MIN_WINDOWS

    @property
    def shortfall(self) -> int:
        return max(0, MIN_WINDOWS - self.settled)

    @property
    def progress(self) -> float:
        return min(1.0, self.settled / MIN_WINDOWS) if MIN_WINDOWS else 1.0

    @property
    def is_one_sided(self) -> bool:
        """True when the tape is mostly one direction.

        Worth surfacing separately from the count, because it is the failure
        that looks like success: a full sample from a single sustained trend
        produces a confident-looking edge that is really one move counted
        again and again.
        """
        return self.settled > 0 and self.one_sided_share >= ONE_SIDED_SHARE

    @property
    def observed_per_day(self) -> float | None:
        """Settled markets per day actually achieved so far.

        The number that matters for planning, and usually far below the ideal:
        it absorbs every hour the recorder was not running, which in practice
        is most of them. A container that suspends, a laptop that sleeps and a
        process that was never restarted all show up here and nowhere else.
        """
        if not self.days:
            return None
        return self.settled / len(self.days)

    def days_remaining(self, coins_recording: int | None = None) -> float | None:
        """Days of further recording at the *ideal* continuous rate.

        The floor, not the forecast -- it assumes every window of every coin
        is captured from now on. Compare `days_remaining_observed`.
        """
        if self.enough_for_a_verdict:
            return 0.0
        active = coins_recording or len(self.coins)
        if active <= 0:
            return None
        return self.shortfall / (active * WINDOWS_PER_COIN_PER_DAY)

    def days_remaining_observed(self) -> float | None:
        """Days remaining at the rate actually being achieved."""
        if self.enough_for_a_verdict:
            return 0.0
        rate = self.observed_per_day
        if not rate:
            return None
        return self.shortfall / rate

    @property
    def capture_efficiency(self) -> float | None:
        """Share of the achievable windows actually captured.

        Well under 1.0 means the recorder is not running most of the time,
        which is a different problem from "needs more days" and is fixed a
        different way.
        """
        if not self.days or not self.coins:
            return None
        ideal = len(self.days) * len(self.coins) * WINDOWS_PER_COIN_PER_DAY
        return self.settled / ideal if ideal else None

    def verdict(self) -> str:
        """One sentence on what the data currently supports."""
        if self.settled == 0:
            return (
                "No settled markets recorded. Nothing here can be measured "
                "yet -- start the recorder."
            )
        if not self.enough_for_a_verdict:
            observed = self.days_remaining_observed()
            when = "" if observed is None else f" (about {observed:.0f} more days"
            if observed is not None:
                ideal = self.days_remaining()
                if ideal is not None and ideal < observed / 2:
                    when += f", or {ideal:.1f} if the recorder ran continuously"
                when += ")"
            return (
                f"{self.settled} settled markets. Below the {MIN_WINDOWS} "
                f"needed before any verdict means anything{when}. Every "
                "strategy result until then is noise with error bars wide "
                "enough to contain any conclusion you like."
            )
        if self.is_one_sided:
            pct = 100 * self.one_sided_share
            return (
                f"{self.settled} settled markets, but {pct:.0f}% went the "
                "same way. That is one directional move counted many times, "
                "not many independent observations -- a measured edge here "
                "would mostly be a measurement of that trend."
            )
        return (
            f"{self.settled} settled markets, reasonably balanced "
            f"({self.yes} yes / {self.no} no). Enough for the tooling to "
            "return a verdict worth reading -- run `signals` and `search`, "
            "and compare `search` against its shuffled null."
        )


def take_inventory(directory: Path) -> Inventory:
    """Count what is on disk. Reads the recordings once."""
    directory = Path(directory)
    inv = Inventory(days=available_days(directory), directory=str(directory))
    if not inv.days:
        return inv

    books, settled = load_session(directory)
    for ticker, series in books.items():
        if not series:
            continue
        coin = series[0].coin
        entry = inv.coins.setdefault(coin, CoinInventory(coin=coin))
        entry.windows += 1
        entry.snapshots += len(series)
        outcome = settled.get(ticker)
        if outcome:
            entry.settled += 1
            if outcome == "yes":
                entry.yes += 1
            else:
                entry.no += 1
    return inv


def format_inventory(inv: Inventory) -> str:
    lines = [
        "RECORDING INVENTORY",
        "=" * 68,
        f"  directory   {inv.directory}",
        f"  days        {len(inv.days)}"
        + (f"  ({inv.days[0]} .. {inv.days[-1]})" if inv.days else ""),
        f"  snapshots   {inv.snapshots:,}",
        f"  windows     {inv.windows:,}",
        f"  settled     {inv.settled:,}",
        "",
    ]
    if inv.coins:
        lines.append(f"  {'coin':<8}{'windows':>9}{'settled':>9}{'yes':>7}{'no':>7}")
        for coin in sorted(inv.coins):
            c = inv.coins[coin]
            lines.append(
                f"  {coin:<8}{c.windows:>9,}{c.settled:>9,}{c.yes:>7,}{c.no:>7,}"
            )
        lines.append("")

    bar_width = 40
    filled = int(round(inv.progress * bar_width))
    lines += [
        f"  toward a verdict  [{'#' * filled}{'.' * (bar_width - filled)}] "
        f"{100 * inv.progress:.0f}%",
        f"  {inv.settled}/{MIN_WINDOWS} settled markets",
    ]

    efficiency = inv.capture_efficiency
    if efficiency is not None and efficiency < 0.5:
        lines += [
            "",
            f"  capture rate      {100 * efficiency:.0f}% of what {len(inv.coins)} "
            f"coins over {len(inv.days)} day(s) could have produced.",
            "  The recorder was not running for most of that window -- that is a",
            "  separate problem from needing more days, and no amount of waiting",
            "  fixes it. Run it somewhere that stays up.",
        ]

    lines += ["", "  " + inv.verdict()]
    return "\n".join(lines)
