"""Replay recorded order books through a strategy and score the result.

This is the honest-measurement half of the project. Everything it reports is
net of Kalshi's real trading fee, and every simplification it makes is listed
here rather than buried:

* **Entries fill at the recorded ask**, capped by the size actually resting
  there. No assumption of infinite liquidity.
* **Exits are resting limit orders** and fill only when a later snapshot shows a
  bid at or above the target. That is pessimistic — a real resting order has
  queue position and might fill on a touch — and pessimistic is the right
  direction for research.
* **Held positions settle from the recorded result.** If a market's settlement
  was never captured, its trades are reported separately as unresolved rather
  than silently dropped or marked flat.
* **Market impact is not modelled.** The bot's own order would move a thin book.
  Results at large size are therefore optimistic.
* **One trade per market window**, matching the live engine's dedupe.

A backtest is evidence, not proof. Treat a thin edge here as noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ..kalshi.fees import fee_dc, min_profitable_exit_dc
from ..kalshi.orderbook import OrderBook
from ..kalshi.prices import cents_to_dc
from ..kalshi.ws import BookHistory
from ..strategy import MarketContext, Signal, get_strategy
from .store import BookRecord, load_session


@dataclass
class ReplayConfig:
    """Trading settings for a run, in the same units the dashboard uses."""

    strategy: str = "drift"
    contracts: int = 1
    profit_cents: int = 8
    min_confidence: float = 0.60
    min_entry_cents: int = 25
    max_entry_cents: int = 65
    #: Raise a target that would not clear the round-trip fee, as live does.
    enforce_fee_floor: bool = True

    def target_dc(self, entry_dc: int) -> int:
        target = entry_dc + cents_to_dc(self.profit_cents)
        if self.enforce_fee_floor:
            floor = min_profitable_exit_dc(self.contracts, entry_dc)
            if floor is not None:
                target = max(target, floor)
        return min(999, target)


@dataclass
class ReplayTrade:
    coin: str
    ticker: str
    side: str
    count: int
    entry_dc: int
    entry_fee_dc: int
    target_dc: int
    confidence: float
    reason: str
    entered_at: float
    exit_dc: int | None = None
    exit_fee_dc: int = 0
    exited_at: float | None = None
    outcome: str = "open"  # "target" | "settled" | "unresolved" | "open"

    @property
    def gross_dc(self) -> int:
        if self.exit_dc is None:
            return 0
        return (self.exit_dc - self.entry_dc) * self.count

    @property
    def net_dc(self) -> int:
        return self.gross_dc - self.entry_fee_dc - self.exit_fee_dc

    @property
    def resolved(self) -> bool:
        return self.outcome in {"target", "settled"}


@dataclass
class ReplayResult:
    config: ReplayConfig
    trades: list[ReplayTrade] = field(default_factory=list)
    windows_seen: int = 0
    snapshots: int = 0

    @property
    def resolved(self) -> list[ReplayTrade]:
        return [t for t in self.trades if t.resolved]

    @property
    def unresolved(self) -> list[ReplayTrade]:
        return [t for t in self.trades if not t.resolved]


def book_from_record(rec: BookRecord) -> OrderBook:
    """Rebuild an order book from a recording, exactly as it was stored."""
    book = OrderBook(rec.ticker)
    book.yes = {int(p): float(q) for p, q in rec.yes}
    book.no = {int(p): float(q) for p, q in rec.no}
    book.updated_at = rec.t
    return book


def _fillable(book: OrderBook, side: str, wanted: int) -> int:
    """Contracts we could actually lift at the touch."""
    return int(min(wanted, book.size_at_ask(side)))


def replay_market(
    records: list[BookRecord],
    settlement: str | None,
    config: ReplayConfig,
) -> ReplayTrade | None:
    """Run one market window end to end. Returns the trade taken, if any."""
    if len(records) < 10:
        return None

    strategy = get_strategy(config.strategy)
    history = BookHistory()
    min_dc = cents_to_dc(config.min_entry_cents)
    max_dc = cents_to_dc(config.max_entry_cents)
    trade: ReplayTrade | None = None

    for rec in records:
        book = book_from_record(rec)
        fair = book.microprice()
        if fair is not None:
            history.observe(rec.ticker, fair, rec.t)

        if trade is None:
            ctx = MarketContext(
                coin=rec.coin,
                ticker=rec.ticker,
                book=book,
                seconds_to_close=rec.seconds_to_close,
                window_seconds=rec.window_seconds,
                fv_change_5s=history.change_over(rec.ticker, 5, rec.t),
                fv_change_20s=history.change_over(rec.ticker, 20, rec.t),
                fv_change_60s=history.change_over(rec.ticker, 60, rec.t),
                samples=history.samples(rec.ticker),
                # A snapshot is by definition current at its own timestamp.
                book_age_s=0.0,
            )
            signal: Signal | None = strategy.evaluate(ctx)
            if signal is None or signal.confidence < config.min_confidence:
                continue
            if not (min_dc <= signal.price_dc <= max_dc):
                continue
            filled = _fillable(book, signal.side, config.contracts)
            if filled < 1:
                continue

            entry_fee = fee_dc(filled, signal.price_dc)
            trade = ReplayTrade(
                coin=rec.coin,
                ticker=rec.ticker,
                side=signal.side,
                count=filled,
                entry_dc=signal.price_dc,
                entry_fee_dc=entry_fee,
                target_dc=config.target_dc(signal.price_dc),
                confidence=signal.confidence,
                reason=signal.reason,
                entered_at=rec.t,
            )
            continue

        # Position open: the resting exit fills once a bid reaches the target.
        bid = book.best_bid(trade.side)
        if bid is not None and bid >= trade.target_dc:
            trade.exit_dc = trade.target_dc
            trade.exit_fee_dc = fee_dc(trade.count, trade.target_dc)
            trade.exited_at = rec.t
            trade.outcome = "target"
            return trade

    if trade is None:
        return None

    # Never filled the exit: the window settles.
    if settlement in {"yes", "no"}:
        trade.exit_dc = 1000 if settlement == trade.side else 0
        trade.exit_fee_dc = 0  # settlement is free
        trade.outcome = "settled"
        trade.exited_at = records[-1].t
    else:
        trade.outcome = "unresolved"
    return trade


def replay(
    directory,
    config: ReplayConfig,
    *,
    since: str | None = None,
    until: str | None = None,
    coins: Iterable[str] | None = None,
) -> ReplayResult:
    """Replay every recorded window through `config`."""
    books, settled = load_session(directory, since, until)
    wanted = {c.upper() for c in coins} if coins else None

    result = ReplayResult(config=config)
    for ticker, records in sorted(books.items(), key=lambda kv: kv[1][0].t):
        if wanted and records[0].coin.upper() not in wanted:
            continue
        result.windows_seen += 1
        result.snapshots += len(records)
        trade = replay_market(records, settled.get(ticker), config)
        if trade is not None:
            result.trades.append(trade)
    return result
