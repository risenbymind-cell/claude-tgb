"""Reconcile the bot's ledger against Kalshi's actual account state.

The ledger is the bot's memory, but the exchange is the truth. They can diverge
for ordinary reasons: the process died between placing an entry and recording
it, an exit filled while the bot was restarting, someone closed a position by
hand in the Kalshi app, or an exit filled only partially.

Left alone, each divergence is a position nobody is managing. This runs at
startup and periodically, and reports what it found rather than silently
"fixing" things — an automatic correction to a money-moving system should be
narrow and explainable, and anything outside that is escalated to the user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..kalshi.prices import dollars_to_dc, format_cents, parse_count
from ..kalshi.rest import KalshiClient, KalshiError
from ..storage import Storage, Trade

log = logging.getLogger(__name__)


@dataclass
class Divergence:
    """One difference between the ledger and the exchange."""

    kind: str  # "closed_elsewhere" | "partial_exit" | "untracked" | "orphan_order"
    detail: str
    trade_id: int | None = None
    ticker: str | None = None


@dataclass
class ReconcileReport:
    checked: int = 0
    closed: int = 0
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.divergences

    def summary(self) -> str:
        if self.clean:
            return f"✅ Reconciled {self.checked} position(s) — ledger matches Kalshi."
        lines = [f"⚠️ Reconciled {self.checked} position(s), found {len(self.divergences)}:"]
        for d in self.divergences:
            lines.append(f"• {d.detail}")
        return "\n".join(lines)


def _position_count(entry: dict) -> float:
    """Signed contract count from a Kalshi position row, across field spellings."""
    for key in ("position_fp", "position"):
        if key in entry and entry[key] is not None:
            try:
                return parse_count(entry[key])
            except Exception:  # noqa: BLE001
                continue
    return 0.0


class Reconciler:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    async def run(
        self, tg_id: int, client: KalshiClient, *, close_missing: bool = True
    ) -> ReconcileReport:
        """Compare this user's open live trades against their Kalshi account."""
        report = ReconcileReport()
        open_trades = [t for t in await self.storage.open_trades(tg_id) if not t.paper]
        if not open_trades:
            return report

        try:
            positions = await client.get_positions()
        except KalshiError as exc:
            log.warning("Reconcile skipped for %s: %s", tg_id, exc)
            return report

        held = {
            row.get("ticker"): _position_count(row)
            for row in positions
            if row.get("ticker")
        }

        for trade in open_trades:
            report.checked += 1
            actual = abs(held.get(trade.ticker, 0.0))

            if actual == 0:
                # The exchange says we hold nothing. The exit filled, or the
                # position was closed by hand, while the bot was not watching.
                if close_missing:
                    await self._close_from_exchange(trade, client, report)
                else:
                    report.divergences.append(
                        Divergence(
                            "closed_elsewhere",
                            f"{trade.coin}: ledger says {trade.count} open, "
                            f"Kalshi holds none",
                            trade.id,
                            trade.ticker,
                        )
                    )
                continue

            if actual < trade.count:
                # A partially filled exit: part of the position is gone.
                report.divergences.append(
                    Divergence(
                        "partial_exit",
                        f"{trade.coin}: exit partially filled — "
                        f"{trade.count - int(actual):.0f} of {trade.count} closed, "
                        f"{actual:.0f} still open",
                        trade.id,
                        trade.ticker,
                    )
                )

        # Positions on the exchange the ledger knows nothing about.
        tracked = {t.ticker for t in open_trades}
        for ticker, count in held.items():
            if count and ticker not in tracked:
                report.divergences.append(
                    Divergence(
                        "untracked",
                        f"{ticker}: Kalshi holds {abs(count):.0f} contract(s) the "
                        f"bot is not tracking — it will not be managed",
                        ticker=ticker,
                    )
                )
        return report

    async def _close_from_exchange(
        self, trade: Trade, client: KalshiClient, report: ReconcileReport
    ) -> None:
        """Book a trade the exchange says is already closed, at its real price."""
        exit_dc, fee_dc = await self._exit_from_fills(trade, client)
        if exit_dc is None:
            # No fill found: the market probably settled. Read the result.
            try:
                market = await client.get_market(trade.ticker)
            except KalshiError:
                return
            result = (market.get("result") or "").lower()
            if result not in {"yes", "no"}:
                return  # genuinely unknown; leave it open and look again later
            exit_dc, fee_dc = (1000 if result == trade.side else 0), 0

        await self.storage.close_trade(
            trade.id, exit_price_dc=exit_dc, exit_fee_dc=fee_dc, status="closed"
        )
        report.closed += 1
        report.divergences.append(
            Divergence(
                "closed_elsewhere",
                f"{trade.coin} closed off-bot at {format_cents(exit_dc)} — "
                f"booked from Kalshi's own record",
                trade.id,
                trade.ticker,
            )
        )

    async def _exit_from_fills(
        self, trade: Trade, client: KalshiClient
    ) -> tuple[int | None, int]:
        """Find the closing fill for a trade, in our own side's terms."""
        try:
            fills = await client.get_fills(ticker=trade.ticker, limit=100)
        except KalshiError:
            return None, 0

        total_count = 0.0
        total_value = 0.0
        total_fee = 0.0
        for fill in fills:
            # Only fills after we opened, and only the closing direction.
            ts = fill.get("created_time") or fill.get("ts")
            action = (fill.get("action") or "").lower()
            if action and action != "sell":
                continue
            count = parse_count(fill.get("count_fp", fill.get("count", 0)))
            if count <= 0:
                continue
            price_field = fill.get("price_dollars") or fill.get("yes_price_dollars")
            if price_field is None:
                continue
            book_price = dollars_to_dc(price_field)
            # Fills are quoted YES-side; mirror onto ours.
            price = book_price if trade.side == "yes" else 1000 - book_price
            total_count += count
            total_value += price * count
            fee_field = fill.get("fee_dollars") or fill.get("fee_paid_dollars")
            if fee_field is not None:
                total_fee += dollars_to_dc(fee_field) * count
            _ = ts

        if total_count <= 0:
            return None, 0
        return int(round(total_value / total_count)), int(round(total_fee))


async def cancel_orphan_orders(
    client: KalshiClient, live_tickers: set[str]
) -> list[str]:
    """Cancel resting orders on markets that are no longer live.

    An exit left resting on an expired market is harmless but untidy; one left
    resting on a market that reopened would be a trade nobody asked for.
    """
    try:
        resting = await client.get_resting_orders()
    except KalshiError:
        return []

    cancelled: list[str] = []
    for order in resting:
        ticker = order.get("ticker")
        order_id = order.get("order_id")
        if not ticker or not order_id or ticker in live_tickers:
            continue
        try:
            await client.cancel_order(order_id)
        except KalshiError as exc:
            log.debug("Could not cancel %s: %s", order_id, exc)
            continue
        cancelled.append(order_id)
    return cancelled
