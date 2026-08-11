"""Risk gate.

Every prospective entry passes through `check()`. It returns either an approved
size or a human-readable reason the trade was blocked — the same reason string
the user sees in Telegram, so nothing is rejected invisibly.

The caps are deliberately checked against *realised state* (trades in the
ledger, live balance) rather than an in-memory tally, so a restart cannot reset
a user's daily loss limit.

User settings are stored in cents, because that is what the dashboard shows and
what traders think in. Everything is converted to deci-cents here, at the one
boundary, so the engine below never mixes units.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..kalshi.fees import fee_dc, min_profitable_exit_dc
from ..kalshi.prices import cents_to_dc, clamp_price, format_cents, format_dollars
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
    realised_today_dc: int
    open_exposure_dc: int
    open_positions: int
    trades_this_window: int


class RiskManager:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    async def snapshot(self, user: User, ticker: str | None = None) -> RiskSnapshot:
        today = await self.storage.trades_since(user.tg_id, start_of_utc_day())
        realised = sum(t.pnl_dc or 0 for t in today if t.status != "open")
        open_trades = [t for t in today if t.status == "open"]
        # Also count positions opened before midnight that are somehow still open.
        for t in await self.storage.open_trades(user.tg_id):
            if all(t.id != o.id for o in open_trades):
                open_trades.append(t)
        # Exposure is what leaving the position open has actually cost:
        # the contracts plus the fee already paid to get in.
        exposure = sum(t.entry_price_dc * t.count + t.entry_fee_dc for t in open_trades)
        in_window = len([t for t in today if t.ticker == ticker]) if ticker else 0
        return RiskSnapshot(
            realised_today_dc=realised,
            open_exposure_dc=exposure,
            open_positions=len(open_trades),
            trades_this_window=in_window,
        )

    async def check(
        self,
        user: User,
        *,
        ticker: str,
        entry_price_dc: int,
        balance_dc: int | None,
    ) -> RiskDecision:
        """Decide whether `user` may open a position at `entry_price_dc`."""
        requested = max(1, int(user.get("contracts")))
        max_price_dc = cents_to_dc(user.get("max_entry_price"))
        min_price_dc = cents_to_dc(user.get("min_entry_price"))

        if entry_price_dc > max_price_dc:
            return RiskDecision.block(
                f"ask {format_cents(entry_price_dc)} above your "
                f"{format_cents(max_price_dc)} cap"
            )
        if entry_price_dc < min_price_dc:
            return RiskDecision.block(
                f"ask {format_cents(entry_price_dc)} below your "
                f"{format_cents(min_price_dc)} floor"
            )

        snap = await self.snapshot(user, ticker)

        loss_limit_dc = cents_to_dc(user.get("daily_loss_limit_cents"))
        if loss_limit_dc > 0 and snap.realised_today_dc <= -loss_limit_dc:
            return RiskDecision.block(
                f"daily loss limit hit ({format_dollars(snap.realised_today_dc)})"
            )

        if snap.open_positions >= int(user.get("max_open_positions")):
            return RiskDecision.block(
                f"already holding {snap.open_positions} open position(s)"
            )

        if snap.trades_this_window >= int(user.get("max_trades_per_window")):
            return RiskDecision.block("per-window trade limit reached")

        cost = entry_price_dc * requested + fee_dc(requested, entry_price_dc)
        exposure_cap_dc = cents_to_dc(user.get("max_exposure_cents"))
        room = exposure_cap_dc - snap.open_exposure_dc
        if room <= 0:
            return RiskDecision.block("exposure cap reached")
        if cost > room:
            requested = room // entry_price_dc
            if requested < 1:
                return RiskDecision.block("exposure cap leaves no room for 1 contract")
            cost = entry_price_dc * requested + fee_dc(requested, entry_price_dc)

        # Balance checks only apply to live trading; paper mode has no balance.
        if balance_dc is not None:
            floor_dc = cents_to_dc(user.get("balance_floor_cents"))
            spendable = balance_dc - floor_dc
            if spendable <= 0:
                return RiskDecision.block(
                    f"balance {format_dollars(balance_dc)} at or below your "
                    f"{format_dollars(floor_dc)} floor"
                )
            if cost > spendable:
                requested = int(spendable // entry_price_dc)
                if requested < 1:
                    return RiskDecision.block("balance floor leaves no room for 1 contract")

        return RiskDecision.allow(int(requested))


def exit_price_for(user: User, entry_price_dc: int, count: int = 1) -> int:
    """The price at which a position should be closed, per the user's settings.

    Raised to the first price that actually clears both trading fees when the
    user's own target would not. A "+2c" exit sounds like a small win but is a
    guaranteed loss once the round trip is paid for, and silently booking those
    is the difference between a bot that looks profitable and one that is.
    """
    if user.get("exit_mode") == "target":
        target_dc = cents_to_dc(user.get("target_price"))
    else:
        target_dc = entry_price_dc + cents_to_dc(user.get("profit_cents"))

    breakeven = min_profitable_exit_dc(count, entry_price_dc)
    if breakeven is not None:
        target_dc = max(target_dc, breakeven)
    # An exit must beat the entry, and must stay inside the settlement bounds.
    return clamp_price(max(entry_price_dc + 1, target_dc))


def realised_pnl(trades: list[Trade]) -> int:
    """Realised P/L in deci-cents."""
    return sum(t.pnl_dc or 0 for t in trades if t.status != "open")
