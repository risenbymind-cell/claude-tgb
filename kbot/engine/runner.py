"""The trading engine.

One engine serves every user. It keeps a single shared view of the market —
discovery, order books, fair-value history, spot — and then, per tick, asks each
user's chosen strategy whether their configured coins qualify. Strategy
evaluation is cached per (strategy, market) within a tick, because a strategy is
a pure function of market state: two users on the same strategy and coin see the
identical signal, and it is only their own risk settings and mode that differ.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from ..config import Settings
from ..kalshi.auth import InvalidPrivateKey, Signer
from ..kalshi.prices import format_cents, format_dollars
from ..kalshi.rest import KalshiClient, KalshiError
from ..kalshi.ws import BookHistory, MarketFeed
from ..storage import Storage, Trade, User
from ..strategy import MarketContext, Signal, get_strategy
from .broker import Broker, LiveBroker, PaperBroker
from .discovery import LiveMarket, MarketDiscovery
from .risk import RiskManager, exit_price_for
from .spot import SpotFeed

log = logging.getLogger(__name__)

Notifier = Callable[[int, str], Awaitable[None]]


@dataclass
class _UserBroker:
    key_id: str | None
    broker: Broker
    client: KalshiClient | None


class Engine:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        notify: Notifier,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.notify = notify

        self._http = httpx.AsyncClient(timeout=10.0)
        signer: Signer | None = None
        if settings.has_market_data_creds:
            try:
                signer = Signer.from_pem(settings.md_key_id, settings.md_private_key)
            except InvalidPrivateKey as exc:
                log.error("Platform Kalshi key is unusable (%s); using REST polling.", exc)

        self.public = KalshiClient(settings.rest_base, signer=signer, client=self._http)
        self.feed = MarketFeed(
            settings.ws_url,
            self.public,
            signer,
            poll_interval=settings.orderbook_poll_interval_s,
        )
        self.discovery = MarketDiscovery(self.public, settings.series)
        self.spot = SpotFeed(settings.spot_products)
        self.risk = RiskManager(storage)
        self.history = BookHistory()

        self._brokers: dict[tuple[int, bool], _UserBroker] = {}
        self._signalled: dict[tuple[int, str], float] = {}
        self._tasks: list[asyncio.Task] = []
        self._started_at = 0.0

    # ---------------- lifecycle ----------------

    async def start(self) -> None:
        self._started_at = time.time()
        self.feed.start()
        self.spot.start()
        self._tasks = [
            asyncio.create_task(self._discovery_loop(), name="discovery"),
            asyncio.create_task(self._tick_loop(), name="tick"),
            asyncio.create_task(self._position_loop(), name="positions"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        await self.feed.stop()
        await self.spot.stop()
        for entry in self._brokers.values():
            if entry.client is not None:
                await entry.client.aclose()
        await self._http.aclose()

    @property
    def uptime_s(self) -> float:
        return time.time() - self._started_at if self._started_at else 0.0

    # ---------------- shared market state ----------------

    async def _discovery_loop(self) -> None:
        while True:
            try:
                users = await self.storage.active_users(
                    self.settings.require_access_key
                )
                coins = sorted({c.upper() for u in users for c in (u.get("coins") or [])})
                if coins:
                    markets = await self.discovery.refresh(coins)
                    self.feed.set_tickers(m.ticker for m in markets.values())
                else:
                    self.feed.set_tickers([])
                self._prune_signal_cache()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad pass must not kill the loop
                log.exception("Discovery pass failed")
            await asyncio.sleep(self.settings.discovery_interval_s)

    def _prune_signal_cache(self) -> None:
        live = {m.ticker for m in self.discovery.markets.values()}
        for key in [k for k in self._signalled if k[1] not in live]:
            self._signalled.pop(key, None)
        for ticker in self.history.tickers():
            if ticker not in live:
                self.history.forget(ticker)

    def _observe_books(self) -> None:
        now = time.time()
        for market in self.discovery.markets.values():
            book = self.feed.book(market.ticker)
            if book is None or book.is_stale:
                continue
            fair = book.microprice()
            if fair is not None:
                self.history.observe(market.ticker, fair, now)

    def _context(self, market: LiveMarket) -> MarketContext | None:
        book = self.feed.book(market.ticker)
        if book is None:
            return None
        return MarketContext(
            coin=market.coin,
            ticker=market.ticker,
            book=book,
            seconds_to_close=market.seconds_to_close(),
            window_seconds=market.window_seconds,
            fv_change_5s=self.history.change_over(market.ticker, 5),
            fv_change_20s=self.history.change_over(market.ticker, 20),
            fv_change_60s=self.history.change_over(market.ticker, 60),
            samples=self.history.samples(market.ticker),
            spot=self.spot.price(market.coin),
            spot_change_pct=self.spot.change_pct(market.coin),
            title=market.title,
        )

    # ---------------- the trading tick ----------------

    async def _tick_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Tick failed")
            await asyncio.sleep(self.settings.scan_interval_s)

    async def _tick(self) -> None:
        self._observe_books()
        users = await self.storage.active_users(self.settings.require_access_key)
        if not users:
            return

        contexts: dict[str, MarketContext] = {}
        for coin, market in self.discovery.markets.items():
            ctx = self._context(market)
            if ctx is not None:
                contexts[coin] = ctx

        # Evaluate each (strategy, market) pair at most once per tick.
        cache: dict[tuple[str, str], Signal | None] = {}
        for user in users:
            for coin in user.get("coins") or []:
                ctx = contexts.get(coin.upper())
                if ctx is None:
                    continue
                strategy_name = str(user.get("strategy"))
                key = (strategy_name, ctx.ticker)
                if key not in cache:
                    strategy = get_strategy(strategy_name)
                    try:
                        cache[key] = strategy.evaluate(ctx)
                    except Exception:  # noqa: BLE001 - never trust a plugin
                        log.exception("Strategy %s raised", strategy_name)
                        cache[key] = None
                signal = cache[key]
                if signal is None:
                    continue
                await self._handle_signal(user, signal, ctx)

    async def _handle_signal(
        self, user: User, signal: Signal, ctx: MarketContext
    ) -> None:
        if signal.confidence < float(user.get("min_confidence")):
            return
        # One signal per user per market window.
        key = (user.tg_id, signal.ticker)
        if key in self._signalled:
            return
        self._signalled[key] = time.time()

        await self.storage.record_signal(
            signal.ticker,
            signal.coin,
            signal.side,
            signal.confidence,
            signal.price_dc,
            signal.reason,
        )

        if user.get("mode") == "auto":
            await self._execute(user, signal, ctx)
        else:
            await self.notify(user.tg_id, _format_signal(signal, ctx))

    async def _execute(self, user: User, signal: Signal, ctx: MarketContext) -> None:
        broker = await self._broker_for(user)
        if broker is None:
            return

        balance_dc = await broker.balance()
        decision = await self.risk.check(
            user,
            ticker=signal.ticker,
            entry_price_dc=signal.price_dc,
            balance_dc=balance_dc,
        )
        if not decision.allowed:
            await self.notify(
                user.tg_id,
                f"{_signal_header(signal, ctx)}\n\n⛔ Skipped — {decision.reason}",
            )
            return

        result = await broker.buy(
            signal.ticker, signal.side, decision.contracts, signal.price_dc, ctx.book
        )
        if not result.ok:
            await self.notify(
                user.tg_id,
                f"{_signal_header(signal, ctx)}\n\n⚠️ Entry not filled — {result.error}",
            )
            return

        entry_dc = int(result.price_dc or signal.price_dc)
        target_dc = exit_price_for(user, entry_dc)
        trade_id = await self.storage.record_entry(
            tg_id=user.tg_id,
            ticker=signal.ticker,
            coin=signal.coin,
            side=signal.side,
            count=result.filled,
            entry_price_dc=entry_dc,
            target_price_dc=target_dc,
            paper=broker.paper,
            entry_order_id=result.order_id,
            reason=signal.reason,
        )

        # Place the exit immediately, so the position is never unmanaged.
        exit_order = await broker.sell(
            signal.ticker, signal.side, result.filled, target_dc, ctx.book
        )
        if exit_order.ok and exit_order.order_id:
            await self.storage.set_exit_order(trade_id, exit_order.order_id)

        tag = "📝 PAPER" if broker.paper else "⚡ LIVE"
        cost_dc = entry_dc * result.filled
        await self.notify(
            user.tg_id,
            f"{tag} · <b>{signal.coin} {signal.direction}</b>\n"
            f"Bought <b>{result.filled}</b> × {signal.side.upper()} @ "
            f"<b>{format_cents(entry_dc)}</b> (${format_dollars(cost_dc)})\n"
            f"Exit resting at <b>{format_cents(target_dc)}</b>\n"
            f"<i>{signal.reason}</i>\n"
            f"<code>{signal.ticker}</code>",
        )

    async def _broker_for(self, user: User, paper: bool | None = None) -> Broker | None:
        """Get the user's broker.

        `paper` overrides the user's current setting, which matters when
        managing an already-open live position for someone who has since
        switched back to paper — that position still needs a live broker.
        """
        want_paper = user.get("paper") if paper is None else paper
        # Paper and live brokers are cached separately, so toggling the mode
        # does not tear down and rebuild a broker on every pass.
        cache_key = (user.tg_id, want_paper)

        if want_paper:
            entry = self._brokers.get(cache_key)
            if entry is None:
                entry = _UserBroker(key_id=None, broker=PaperBroker(), client=None)
                self._brokers[cache_key] = entry
            return entry.broker

        if not user.has_credentials:
            await self.storage.set_enabled(user.tg_id, False)
            await self.notify(
                user.tg_id,
                "⛔ Trading stopped — live mode needs Kalshi API credentials. "
                "Use /connect, or switch back to paper mode.",
            )
            return None

        entry = self._brokers.get(cache_key)
        if entry is None or entry.key_id != user.kalshi_key_id:
            if entry and entry.client:
                await entry.client.aclose()
            try:
                signer = Signer.from_pem(user.kalshi_key_id, user.kalshi_secret)
            except InvalidPrivateKey as exc:
                await self.storage.set_enabled(user.tg_id, False)
                await self.notify(
                    user.tg_id, f"⛔ Trading stopped — your Kalshi key is invalid: {exc}"
                )
                return None
            client = KalshiClient(
                self.settings.rest_base, signer=signer, client=self._http
            )
            entry = _UserBroker(
                key_id=user.kalshi_key_id, broker=LiveBroker(client), client=client
            )
            self._brokers[cache_key] = entry
        return entry.broker

    # ---------------- position management ----------------

    async def _position_loop(self) -> None:
        while True:
            try:
                await self._manage_positions()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Position management failed")
            await asyncio.sleep(2.0)

    async def _manage_positions(self) -> None:
        open_trades = await self.storage.open_trades()
        for trade in open_trades:
            user = await self.storage.get_user(trade.tg_id)
            if user is None:
                continue
            if trade.paper:
                await self._manage_paper(trade)
            else:
                await self._manage_live(trade, user)

    async def _manage_paper(self, trade: Trade) -> None:
        book = self.feed.book(trade.ticker)
        target_dc = trade.target_price_dc or 999
        if book is not None and not book.is_stale:
            bid = book.best_bid(trade.side)
            # A resting sell fills once someone bids at or above our ask.
            if bid is not None and bid >= target_dc:
                await self._close(trade, target_dc, "target hit")
                return
        await self._settle_if_expired(trade)

    async def _manage_live(self, trade: Trade, user: User) -> None:
        broker = await self._broker_for(user, paper=False)
        if not isinstance(broker, LiveBroker):
            return
        if trade.exit_order_id:
            try:
                order = await broker.client.get_order(trade.exit_order_id)
            except KalshiError as exc:
                log.debug("Exit order lookup failed for %s: %s", trade.id, exc)
                order = {}
            status = (order.get("status") or "").lower()
            remaining = order.get("remaining_count")
            if status in {"executed", "filled"} or (
                remaining is not None and int(remaining) == 0 and status != "canceled"
            ):
                await self._close(trade, trade.target_price_dc or 999, "target hit")
                return
        await self._settle_if_expired(trade)

    async def _settle_if_expired(self, trade: Trade) -> None:
        market = self.discovery.markets.get(trade.coin)
        # Still the live window: nothing to settle.
        if market is not None and market.ticker == trade.ticker:
            return
        try:
            data = await self.public.get_market(trade.ticker)
        except KalshiError as exc:
            log.debug("Settlement lookup failed for %s: %s", trade.ticker, exc)
            return

        status = (data.get("status") or "").lower()
        result = (data.get("result") or "").lower()
        if status in {"open", "active"} or result not in {"yes", "no"}:
            return  # not settled yet; check again next pass

        # A settled contract is worth $1.00 on the winning side, nothing on the
        # losing one.
        settle_dc = 1000 if result == trade.side else 0
        await self._close(trade, settle_dc, f"settled {result.upper()}", "expired")

    async def _close(
        self, trade: Trade, exit_price_dc: int, reason: str, status: str = "closed"
    ) -> None:
        closed = await self.storage.close_trade(
            trade.id, exit_price_dc=exit_price_dc, status=status
        )
        if closed is None:
            return  # someone else closed it first
        pnl_dc = closed.pnl_dc or 0
        icon = "✅" if pnl_dc > 0 else ("➖" if pnl_dc == 0 else "❌")
        tag = "📝 PAPER" if closed.paper else "⚡ LIVE"
        sign = "+" if pnl_dc >= 0 else "-"
        await self.notify(
            closed.tg_id,
            f"{icon} {tag} · <b>{closed.coin} "
            f"{'UP' if closed.side == 'yes' else 'DOWN'}</b> closed\n"
            f"{closed.count} × {format_cents(closed.entry_price_dc)} → "
            f"<b>{format_cents(exit_price_dc)}</b> ({reason})\n"
            f"P/L <b>{sign}${format_dollars(abs(pnl_dc))}</b>",
        )


def _signal_header(signal: Signal, ctx: MarketContext) -> str:
    arrow = "▲" if signal.side == "yes" else "▼"
    return (
        f"{arrow} <b>{signal.coin} {signal.direction}</b> · "
        f"{signal.confidence*100:.0f}% confidence"
    )


def _format_signal(signal: Signal, ctx: MarketContext) -> str:
    mins, secs = divmod(max(0, int(ctx.seconds_to_close)), 60)
    lines = [
        _signal_header(signal, ctx),
        "",
        f"Buy <b>{signal.side.upper()}</b> at <b>{format_cents(signal.price_dc)}</b>",
        f"Window closes in {mins}m {secs:02d}s",
        f"<i>{signal.reason}</i>",
        f"<code>{signal.ticker}</code>",
    ]
    if ctx.spot is not None:
        lines.insert(4, f"Spot {ctx.spot:,.2f}")
    return "\n".join(lines)
