"""In-memory order book for a single Kalshi market.

Kalshi publishes two resting-bid ladders per market: bids on YES and bids on NO.
An ask on YES is just the mirror of a bid on NO, because a YES and a NO contract
together always settle to $1.00:

    yes_ask = $1.00 - best_no_bid
    no_ask  = $1.00 - best_yes_bid

All prices here are integer **deci-cents** (0-1000; see `prices.py`), which
represents every tick of Kalshi's tapered deci-cent structure exactly. Counts
are floats, because Kalshi supports fractional contracts down to 0.01.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .prices import complement, parse_levels


@dataclass
class OrderBook:
    ticker: str
    yes: dict[int, float] = field(default_factory=dict)  # price_dc -> contracts
    no: dict[int, float] = field(default_factory=dict)
    seq: int | None = None
    updated_at: float = 0.0
    #: Set when a delta arrives out of order. A book that has missed a frame is
    #: wrong in a way it cannot detect from its own contents -- the levels look
    #: perfectly plausible -- so the flag is the only thing standing between a
    #: dropped frame and an order priced off a book that silently diverged from
    #: the exchange's. Cleared only by a fresh snapshot.
    desynced: bool = False

    # ---------------- mutation ----------------

    def apply_snapshot(self, yes: object, no: object, seq: int | None = None) -> None:
        """Replace the book from a snapshot frame (raw wire ladders).

        A snapshot is ground truth, so it also clears a desync.
        """
        self.yes = dict(parse_levels(yes))
        self.no = dict(parse_levels(no))
        self.seq = seq
        self.desynced = False
        self.updated_at = time.time()

    def apply_delta(
        self, side: str, price_dc: int, delta: float, seq: int | None = None
    ) -> bool:
        """Apply one incremental change. Returns False if a frame was missed.

        Deltas are only meaningful in order. When the sequence jumps, the
        missing frame cannot be reconstructed from anything we hold, so the
        book is marked desynced and the delta is dropped: applying it would
        produce a plausible-looking book that is quietly wrong, which is worse
        than an obviously stale one. The caller is expected to re-snapshot.
        """
        if seq is not None and self.seq is not None:
            if seq <= self.seq:
                # A duplicate or replayed frame. Ignore it, but this is not a
                # desync -- nothing was lost.
                return True
            if seq != self.seq + 1:
                self.desynced = True
                return False

        book = self.yes if side == "yes" else self.no
        price_dc = int(price_dc)
        new_qty = book.get(price_dc, 0.0) + float(delta)
        # Fractional contracts mean a level can land just above zero on
        # rounding; treat anything under the 0.01 minimum as gone.
        if new_qty >= 0.01:
            book[price_dc] = new_qty
        else:
            book.pop(price_dc, None)
        self.seq = seq
        self.updated_at = time.time()
        return True

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
        return None if best_no is None else complement(best_no)

    @property
    def no_ask(self) -> int | None:
        best_yes = self.best_yes_bid
        return None if best_yes is None else complement(best_yes)

    def best_bid(self, side: str) -> int | None:
        return self.best_yes_bid if side == "yes" else self.best_no_bid

    def best_ask(self, side: str) -> int | None:
        return self.yes_ask if side == "yes" else self.no_ask

    def size_at(self, side: str, price_dc: int) -> float:
        book = self.yes if side == "yes" else self.no
        return book.get(int(price_dc), 0.0)

    def size_at_ask(self, side: str) -> float:
        """Contracts available to lift on `side` at the touch.

        Buying YES means hitting the best NO bid, so the size on offer is the
        size resting at the mirrored price on the opposite ladder.
        """
        ask = self.best_ask(side)
        if ask is None:
            return 0.0
        opposite = "no" if side == "yes" else "yes"
        return self.size_at(opposite, complement(ask))

    @property
    def spread(self) -> int | None:
        """YES spread in deci-cents."""
        bid, ask = self.best_yes_bid, self.yes_ask
        if bid is None or ask is None:
            return None
        return ask - bid

    @property
    def mid(self) -> float | None:
        """Mid of the YES market, in deci-cents."""
        bid, ask = self.best_yes_bid, self.yes_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    def depth(self, side: str, levels: int = 3) -> float:
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
        """Depth-weighted fair value of YES, in deci-cents.

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
        """True when this book must not be used to price an order.

        A desynced book counts as stale. It has contents, and they look
        entirely reasonable, which is precisely why it needs saying.
        """
        return self.updated_at == 0.0 or self.desynced

    def age(self) -> float:
        return float("inf") if self.is_stale else time.time() - self.updated_at
