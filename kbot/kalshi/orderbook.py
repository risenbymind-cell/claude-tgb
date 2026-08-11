"""In-memory order book for a single Kalshi market.

Kalshi publishes two resting-bid ladders per market: bids on YES and bids on NO.
An ask on YES is just the mirror of a bid on NO, because a YES and a NO contract
together always settle to 100 cents:

    yes_ask = 100 - best_no_bid
    no_ask  = 100 - best_yes_bid

All prices are integer cents in 1..99.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class OrderBook:
    ticker: str
    yes: dict[int, int] = field(default_factory=dict)  # price -> resting contracts
    no: dict[int, int] = field(default_factory=dict)
    seq: int | None = None
    updated_at: float = 0.0

    # ---------------- mutation ----------------

    def apply_snapshot(self, yes: list, no: list, seq: int | None = None) -> None:
        self.yes = {int(p): int(q) for p, q in yes if int(q) > 0}
        self.no = {int(p): int(q) for p, q in no if int(q) > 0}
        self.seq = seq
        self.updated_at = time.time()

    def apply_delta(self, side: str, price: int, delta: int, seq: int | None = None) -> None:
        book = self.yes if side == "yes" else self.no
        new_qty = book.get(int(price), 0) + int(delta)
        if new_qty > 0:
            book[int(price)] = new_qty
        else:
            book.pop(int(price), None)
        self.seq = seq
        self.updated_at = time.time()

    # ---------------- reads ----------------

    @property
    def best_yes_bid(self) -> int | None:
        return max(self.yes) if self.yes else None

    @property
    def best_no_bid(self) -> int | None:
        return max(self.no) if self.no else None

    @property
    def yes_ask(self) -> int | None:
        best_no = self.best_no_bid
        return None if best_no is None else 100 - best_no

    @property
    def no_ask(self) -> int | None:
        best_yes = self.best_yes_bid
        return None if best_yes is None else 100 - best_yes

    def best_bid(self, side: str) -> int | None:
        return self.best_yes_bid if side == "yes" else self.best_no_bid

    def best_ask(self, side: str) -> int | None:
        return self.yes_ask if side == "yes" else self.no_ask

    @property
    def spread(self) -> int | None:
        bid, ask = self.best_yes_bid, self.yes_ask
        if bid is None or ask is None:
            return None
        return ask - bid

    @property
    def mid(self) -> float | None:
        """Mid of the YES market, in cents."""
        bid, ask = self.best_yes_bid, self.yes_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    def depth(self, side: str, levels: int = 3) -> int:
        """Total contracts resting in the top `levels` bids on `side`."""
        book = self.yes if side == "yes" else self.no
        prices = sorted(book, reverse=True)[:levels]
        return sum(book[p] for p in prices)

    def imbalance(self, levels: int = 3) -> float | None:
        """Signed book imbalance in [-1, 1]; positive means YES-heavy.

        Measured on the two bid ladders, which is where real intent rests.
        """
        yes_depth = self.depth("yes", levels)
        no_depth = self.depth("no", levels)
        total = yes_depth + no_depth
        if total == 0:
            return None
        return (yes_depth - no_depth) / total

    def microprice(self, levels: int = 3) -> float | None:
        """Depth-weighted fair value of YES, in cents.

        Weighting the two sides of the spread by opposing depth gives an
        estimate that leans toward the side likely to be hit next.
        """
        bid, ask = self.best_yes_bid, self.yes_ask
        if bid is None or ask is None:
            return None
        yes_depth = self.depth("yes", levels)
        no_depth = self.depth("no", levels)
        total = yes_depth + no_depth
        if total == 0:
            return (bid + ask) / 2
        # Heavier YES bid depth pushes fair value up toward the ask.
        return (bid * no_depth + ask * yes_depth) / total

    @property
    def is_stale(self) -> bool:
        return self.updated_at == 0.0

    def age(self) -> float:
        return float("inf") if self.is_stale else time.time() - self.updated_at
