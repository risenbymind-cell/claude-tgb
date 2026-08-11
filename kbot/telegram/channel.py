"""Publish trade results to a public Telegram channel.

The landing page promises a results feed. This is it — with one rule that makes
the difference between a results channel and a marketing reel:

**It posts losses too.**

A feed of nothing but wins is not evidence, and anyone who trades will work that
out in a week. The running tally attached to each post is the real one: every
resolved trade, net of fees, wins and losses alike. That is a stronger claim
than a highlight reel precisely because it can be checked.

Posting is opt-in per source: platform results (the house account's own paper or
live trades) and, separately, members who choose to share. Nothing identifiable
about a member is ever posted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..kalshi.prices import format_cents, format_dollars
from ..storage import Storage, Trade
from .api import TelegramClient, TelegramError

log = logging.getLogger(__name__)


@dataclass
class ChannelStats:
    trades: int
    wins: int
    net_dc: int

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0


class ResultsChannel:
    """Posts resolved trades to a channel, wins and losses alike."""

    def __init__(
        self,
        tg: TelegramClient,
        storage: Storage,
        chat_id: str | None,
        *,
        post_losses: bool = True,
        min_net_dc: int = 0,
    ) -> None:
        self.tg = tg
        self.storage = storage
        self.chat_id = chat_id
        self.post_losses = post_losses
        # Suppresses noise from trades that resolved at essentially nothing.
        self.min_net_dc = min_net_dc

    @property
    def enabled(self) -> bool:
        return bool(self.chat_id)

    async def post_trade(self, trade: Trade, strategy: str) -> None:
        """Publish one resolved trade. Anonymous — no user is identified."""
        if not self.enabled or trade.pnl_dc is None:
            return
        if abs(trade.pnl_dc) < self.min_net_dc:
            return
        if trade.pnl_dc <= 0 and not self.post_losses:
            return

        try:
            stats = await self.running_stats(paper=trade.paper)
            await self.tg.send_message(
                self.chat_id, format_post(trade, strategy, stats), reply_markup=None
            )
        except TelegramError as exc:
            # A channel misconfiguration must never interfere with trading.
            log.warning("Results channel post failed: %s", exc)

    async def running_stats(self, paper: bool | None = None) -> ChannelStats:
        """The honest tally: every resolved trade, wins and losses."""
        rows = await self.storage.all_closed_trades(paper=paper)
        nets = [t.pnl_dc or 0 for t in rows]
        return ChannelStats(
            trades=len(nets),
            wins=len([n for n in nets if n > 0]),
            net_dc=sum(nets),
        )


def _signed(dc: int) -> str:
    return f"{'+' if dc >= 0 else '-'}${format_dollars(abs(dc))}"


def format_post(trade: Trade, strategy: str, stats: ChannelStats) -> str:
    from .bot import STRATEGY_LABEL_FALLBACK  # local import avoids a cycle

    label = STRATEGY_LABEL_FALLBACK(strategy)
    up = trade.side == "yes"
    direction = "UP" if up else "DOWN"
    arrow = "📈" if up else "📉"
    won = (trade.pnl_dc or 0) > 0
    head = "💰 <b>CASHED OUT</b>" if won else "🔻 <b>CLOSED RED</b>"
    tag = "📝 Paper" if trade.paper else "⚡ Live"
    gross = trade.gross_pnl_dc or 0
    fees = (trade.entry_fee_dc or 0) + (trade.exit_fee_dc or 0)

    lines = [
        f"{head} · <b>{trade.coin} {direction}</b>",
        f"{tag} · {label} · {trade.coin} {arrow} {direction}",
        f"{trade.count} sh @ {format_cents(trade.entry_price_dc)} → "
        f"{format_cents(trade.exit_price_dc or 0)}",
        f"gross {_signed(gross)} · fees ${format_dollars(fees)}",
        f"<b>{_signed(trade.pnl_dc or 0)}</b> net",
        "",
        f"<i>Running: {stats.trades} trades · {stats.wins} wins "
        f"({stats.win_rate:.0f}%) · {_signed(stats.net_dc)} net</i>",
    ]
    return "\n".join(lines)


def format_daily_summary(stats: ChannelStats, day_stats: ChannelStats) -> str:
    """An end-of-day post, so the record is legible without scrolling."""
    return (
        "📊 <b>Today</b>\n"
        f"{day_stats.trades} trades · {day_stats.wins} wins "
        f"({day_stats.win_rate:.0f}%)\n"
        f"Net <b>{_signed(day_stats.net_dc)}</b>\n\n"
        f"<i>All time: {stats.trades} trades · {stats.win_rate:.0f}% wins · "
        f"{_signed(stats.net_dc)} net. Losses included — this is the whole "
        f"record, not the highlights.</i>"
    )
