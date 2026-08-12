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
from ..kalshi.fees import fee_dc as estimate_fee_dc
from ..kalshi.prices import format_cents, format_dollars
from ..kalshi.rest import KalshiClient, KalshiError
from ..kalshi.ws import BookHistory, MarketFeed
from ..storage import Storage, Trade, User
from ..strategy import MarketContext, Signal, get_strategy
from ..safety import KillSwitch
from .broker import Broker, LiveBroker, PaperBroker
from .discovery import LiveMarket, MarketDiscovery
from .reconcile import Reconciler, cancel_orphan_orders
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
        self.kill = KillSwitch(path=settings.kill_file)
        #: Users already told their live selection is being ignored, so the
        #: notice does not repeat on every pass.
        self._downgraded: set[int] = set()

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
        self.reconciler = Reconciler(storage)
        self.history = BookHistory()

        self._brokers: dict[tuple[int, bool], _UserBroker] = {}
        self._signalled: dict[tuple[int, str], float] = {}
        #: Optional hook: called with (trade, strategy) after a trade resolves.
        #: The Telegram layer uses it to publish to the results channel.
        self.on_trade_closed: Callable[[Trade, str], Awaitable[None]] | None = None
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
            asyncio.create_task(self._reconcile_loop(), name="reconcile"),
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
            book_age_s=book.age(),
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

        # Checked here rather than once per pass: the switch has to be read as
        # late as possible, or it cannot stop an order already being prepared.
        kill = self.kill.state()
        if kill.engaged:
            # Re-arm the market. The one-signal-per-window key is claimed
            # before this point so a repeating signal cannot spam the chat,
            # but nothing was traded here -- leaving it claimed would kill the
            # market for the rest of the window even after the switch is
            # released, turning a pause into an outage.
            self._signalled.pop((user.tg_id, signal.ticker), None)
            await self.notify(
                user.tg_id,
                f"{_signal_header(signal, ctx)}\n\n"
                f"\u26d4 Skipped \u2014 kill switch {kill.describe()}",
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
        target_dc = exit_price_for(user, entry_dc, result.filled)
        trade_id = await self.storage.record_entry(
            tg_id=user.tg_id,
            ticker=signal.ticker,
            coin=signal.coin,
            side=signal.side,
            count=result.filled,
            entry_price_dc=entry_dc,
            target_price_dc=target_dc,
            entry_fee_dc=result.fee_dc,
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

        await self.notify(
            user.tg_id,
            _format_entry(
                signal,
                strategy=str(user.get("strategy")),
                paper=broker.paper,
                count=result.filled,
                entry_dc=entry_dc,
                fee_dc=result.fee_dc,
                target_dc=target_dc,
            ),
        )

    async def _broker_for(self, user: User, paper: bool | None = None) -> Broker | None:
        """Get the user's broker.

        `paper` overrides the user's current setting, which matters when
        managing an already-open live position for someone who has since
        switched back to paper — that position still needs a live broker.
        """
        want_paper = user.get("paper") if paper is None else paper

        # The process mode outranks the per-user toggle. Previously this
        # boolean was the only thing between a connected account and a real
        # order, so a stray database write -- or a mis-tapped button -- was
        # enough. A user can now ask for live all they like; if the process was
        # not started in a live mode, they get a paper broker.
        #
        # Downgrading silently would be its own hazard -- someone watching a
        # dashboard that says LIVE while fills are simulated will draw exactly
        # the wrong conclusion from the results -- so say it once per user.
        if not want_paper and not self.settings.places_real_orders:
            want_paper = True
            if user.tg_id not in self._downgraded:
                self._downgraded.add(user.tg_id)
                await self.notify(
                    user.tg_id,
                    "\u2139\ufe0f You have live mode selected, but this bot is "
                    f"running in {self.settings.mode.label}. Your trades are "
                    "simulated. Nothing has been sent to Kalshi.",
                )
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

    # ---------------- reconciliation ----------------

    async def _reconcile_loop(self) -> None:
        """Check the ledger against Kalshi at startup, then periodically.

        Runs first thing because the most likely moment for the two to diverge
        is a restart — the process may have died between placing an order and
        recording it.
        """
        first = True
        while True:
            try:
                await self.reconcile_all(announce_clean=False)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Reconcile pass failed")
            # Startup check happens immediately; after that it is a slow safety
            # net, since the position loop handles the normal cases.
            await asyncio.sleep(30 if first else 300)
            first = False

    async def reconcile_all(self, announce_clean: bool = True) -> None:
        users = await self.storage.active_users(self.settings.require_access_key)
        live_tickers = {m.ticker for m in self.discovery.markets.values()}
        for user in users:
            if user.get("paper") and not await self.storage.open_trades(user.tg_id):
                continue
            if not user.has_credentials:
                continue
            broker = await self._broker_for(user, paper=False)
            if not isinstance(broker, LiveBroker):
                continue
            report = await self.reconciler.run(user.tg_id, broker.client)
            if report.checked and (not report.clean or announce_clean):
                await self.notify(user.tg_id, report.summary())
            if live_tickers:
                cancelled = await cancel_orphan_orders(broker.client, live_tickers)
                if cancelled:
                    log.info(
                        "Cancelled %d orphaned order(s) for %s",
                        len(cancelled),
                        user.tg_id,
                    )

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
                await self._close(
                    trade,
                    target_dc,
                    "target hit",
                    exit_fee_dc=estimate_fee_dc(trade.count, target_dc),
                )
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
            remaining = _remaining_count(order)
            if remaining is not None and 0 < remaining < trade.count:
                # A partial fill. Reconciliation books it against the exchange's
                # own record rather than guessing here, because the untouched
                # remainder still needs managing.
                log.info(
                    "Exit for trade %s partially filled (%s of %s left)",
                    trade.id,
                    remaining,
                    trade.count,
                )
            if status in {"executed", "filled"} or (
                remaining is not None and remaining == 0 and status != "canceled"
            ):
                target_dc = trade.target_price_dc or 999
                await self._close(
                    trade,
                    target_dc,
                    "target hit",
                    exit_fee_dc=estimate_fee_dc(trade.count, target_dc),
                )
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
        # losing one. Settlement itself is free — Kalshi charges on trades only,
        # so a position held to expiry pays the entry fee and nothing more.
        settle_dc = 1000 if result == trade.side else 0
        await self._close(
            trade,
            settle_dc,
            f"settled {result.upper()}",
            status="expired",
            exit_fee_dc=0,
        )

    async def _close(
        self,
        trade: Trade,
        exit_price_dc: int,
        reason: str,
        status: str = "closed",
        exit_fee_dc: int = 0,
    ) -> None:
        closed = await self.storage.close_trade(
            trade.id,
            exit_price_dc=exit_price_dc,
            exit_fee_dc=exit_fee_dc,
            status=status,
        )
        if closed is None:
            return  # someone else closed it first
        user = await self.storage.get_user(trade.tg_id)
        strategy = str(user.get("strategy")) if user else ""
        await self.notify(closed.tg_id, _format_exit(closed, reason, strategy))
        if self.on_trade_closed is not None:
            try:
                await self.on_trade_closed(closed, strategy)
            except Exception:  # noqa: BLE001 - publishing must never break trading
                log.exception("Trade-closed hook failed")


def _remaining_count(order: dict) -> float | None:
    """Contracts still resting on an order, across the API's field spellings."""
    for key in ("remaining_count_fp", "remaining_count"):
        value = order.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


STRATEGY_LABELS = {"drift": "Drift", "fade": "Fade", "hammer": "Hammer"}


def _label(strategy: str) -> str:
    return STRATEGY_LABELS.get(strategy, strategy.title())


def _format_entry(
    signal: Signal,
    *,
    strategy: str,
    paper: bool,
    count: int,
    entry_dc: int,
    fee_dc: int,
    target_dc: int,
) -> str:
    """Entry confirmation, itemised the way a fill actually costs.

    Cost, fee and total are shown separately because the fee is a real part of
    the trade — on these markets it is a meaningful share of the move.
    """
    cost_dc = entry_dc * count
    tag = "📝 Paper" if paper else "⚡ Live"
    return (
        "🟢 <b>Trade Taken!</b>\n"
        f"{tag} · {_label(strategy)} · <b>{signal.coin} {signal.direction}</b>\n"
        f"{count} sh @ {format_cents(entry_dc)} · cost ${format_dollars(cost_dc)}\n"
        f"est. fee ${format_dollars(fee_dc)} · "
        f"total ${format_dollars(cost_dc + fee_dc)}\n"
        f"target {format_cents(target_dc)} · <i>{signal.reason}</i>"
    )


def _format_exit(trade, reason: str, strategy: str) -> str:
    """Exit confirmation. The headline number is net of both fees."""
    pnl_dc = trade.pnl_dc or 0
    gross_dc = trade.gross_pnl_dc or 0
    fees_dc = (trade.entry_fee_dc or 0) + (trade.exit_fee_dc or 0)
    up = trade.side == "yes"
    arrow = "📈" if up else "📉"
    direction = "UP" if up else "DOWN"
    tag = "📝" if trade.paper else "⚡"

    if pnl_dc > 0:
        head, verdict = "💰 <b>CASHED OUT</b>", f"locked <b>+${format_dollars(pnl_dc)}</b> 🔒"
    elif pnl_dc == 0:
        head, verdict = "➖ <b>CLOSED</b>", "flat"
    else:
        head, verdict = "🔻 <b>CLOSED</b>", f"lost <b>-${format_dollars(abs(pnl_dc))}</b>"

    return (
        f"{head} · <b>{trade.coin} {direction}</b>\n"
        f"{tag} {_label(strategy)} · {trade.coin} {arrow} {direction} · "
        f"{trade.count} sh @ {format_cents(trade.entry_price_dc)}\n"
        f"🏷 Exit @ {format_cents(trade.exit_price_dc or 0)} ({reason})\n"
        f"gross ${format_dollars(gross_dc)} · fees ${format_dollars(fees_dc)}\n"
        f"{verdict}"
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
