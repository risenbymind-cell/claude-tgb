"""Risk gate.

Every prospective entry passes through `check()`. It returns either an approved
size or a human-readable reason the trade was blocked — the same reason string
the user sees in Telegram, so nothing is rejected invisibly.

The caps are deliberately checked against *realised state* (trades in the
ledger, live balance) rather than an in-memory tally, so a restart cannot reset
a user's daily loss limit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..storage import Storage, Trade, User


def start_of_utc_day(now: float | None = None) -> float:
    now = now if now is not None else time.time()
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


@dataclass
class RiskDecision:
    allowed: bool
    contracts: int = 0
    reason: str = ""

    @classmethod
    def block(cls, reason: str) -> "RiskDecision":
        return cls(allowed=False, reason=reason)

    @classmethod
    def allow(cls, contracts: int) -> "RiskDecision":
        return cls(allowed=True, contracts=contracts)


@dataclass
class RiskSnapshot:
    realised_today_cents: int
    open_exposure_cents: int
    open_positions: int
    trades_this_window: int


class RiskManager:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    async def snapshot(self, user: User, ticker: str | None = None) -> RiskSnapshot:
        today = await self.storage.trades_since(user.tg_id, start_of_utc_day())
        realised = sum(t.pnl_cents or 0 for t in today if t.status != "open")
        open_trades = [t for t in today if t.status == "open"]
        # Also count positions opened before midnight that are somehow still open.
        for t in await self.storage.open_trades(user.tg_id):
            if all(t.id != o.id for o in open_trades):
                open_trades.append(t)
        exposure = sum(t.entry_price * t.count for t in open_trades)
        in_window = (
            len([t for t in today if t.ticker == ticker]) if ticker else 0
        )
        return RiskSnapshot(
            realised_today_cents=realised,
            open_exposure_cents=exposure,
            open_positions=len(open_trades),
            trades_this_window=in_window,
        )

    async def check(
        self,
        user: User,
        *,
        ticker: str,
        entry_price: int,
        balance_cents: int | None,
    ) -> RiskDecision:
        """Decide whether `user` may open a position at `entry_price`."""
        requested = max(1, int(user.get("contracts")))
        max_price = int(user.get("max_entry_price"))
        min_price = int(user.get("min_entry_price"))

        if entry_price > max_price:
            return RiskDecision.block(f"ask {entry_price}c above your {max_price}c cap")
        if entry_price < min_price:
            return RiskDecision.block(f"ask {entry_price}c below your {min_price}c floor")

        snap = await self.snapshot(user, ticker)

        loss_limit = int(user.get("daily_loss_limit_cents"))
        if loss_limit > 0 and snap.realised_today_cents <= -loss_limit:
            return RiskDecision.block(
                f"daily loss limit hit ({snap.realised_today_cents/100:+.2f})"
            )

        if snap.open_positions >= int(user.get("max_open_positions")):
            return RiskDecision.block(
                f"already holding {snap.open_positions} open position(s)"
            )

        if snap.trades_this_window >= int(user.get("max_trades_per_window")):
            return RiskDecision.block("per-window trade limit reached")

        cost = entry_price * requested
        exposure_cap = int(user.get("max_exposure_cents"))
        room = exposure_cap - snap.open_exposure_cents
        if room <= 0:
            return RiskDecision.block("exposure cap reached")
        if cost > room:
            requested = room // entry_price
            if requested < 1:
                return RiskDecision.block("exposure cap leaves no room for 1 contract")
            cost = entry_price * requested

        # Balance checks only apply to live trading; paper mode has no balance.
        if balance_cents is not None:
            floor = int(user.get("balance_floor_cents"))
            spendable = balance_cents - floor
            if spendable <= 0:
                return RiskDecision.block(
                    f"balance {balance_cents/100:.2f} at or below your "
                    f"{floor/100:.2f} floor"
                )
            if cost > spendable:
                requested = int(spendable // entry_price)
                if requested < 1:
                    return RiskDecision.block("balance floor leaves no room for 1 contract")

        return RiskDecision.allow(requested)


def exit_price_for(user: User, entry_price: int) -> int:
    """The price at which a position should be closed, per the user's settings."""
    if user.get("exit_mode") == "target":
        target = int(user.get("target_price"))
    else:
        target = entry_price + int(user.get("profit_cents"))
    # Kalshi prices live in 1..99; 99 is the best a resting sell can ask.
    return max(entry_price + 1, min(99, target))


def realised_pnl(trades: list[Trade]) -> int:
    return sum(t.pnl_cents or 0 for t in trades if t.status != "open")
