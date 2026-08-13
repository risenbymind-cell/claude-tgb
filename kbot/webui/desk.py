"""Live state for the local web desk.

Owns the market discovery, the order-book feed and the session statistics, and
re-evaluates every registered strategy on each pass. The web layer only
serialises what it finds here -- no trading logic lives in the HTTP handlers,
so the same decisions can be tested without a socket.

Why this exists at all: Kalshi answers **403 to any request carrying a browser
`Origin` header**, verified against the live API. A page cannot call it. So the
browser talks to this process over localhost, and this process -- which sends no
Origin -- talks to Kalshi. That indirection is not a workaround for a missing
feature; it is the only shape that works.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

from ..config import Settings
from ..kalshi.auth import InvalidPrivateKey, Signer
from ..kalshi.rest import KalshiClient
from ..kalshi.session import SessionStats
from ..kalshi.ws import BookHistory, MarketFeed
from ..safety import KillSwitch, TradingMode
from ..strategy import MarketContext, Signal, get_strategy, strategy_names
from ..engine.discovery import MarketDiscovery

log = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    coin: str
    ticker: str
    side: str
    count: int
    entry_dc: int
    entry_fee_dc: int
    target_dc: int
    strategy: str
    reason: str
    opened_at: float
    exit_dc: int | None = None
    exit_fee_dc: int = 0
    closed_at: float | None = None
    outcome: str = "open"

    @property
    def net_dc(self) -> int:
        if self.exit_dc is None:
            return 0
        gross = (self.exit_dc - self.entry_dc) * self.count
        return gross - self.entry_fee_dc - self.exit_fee_dc


@dataclass
class MarketView:
    coin: str
    ticker: str
    seconds_to_close: float
    mid_dc: float | None
    spread_dc: int | None
    yes_ask: int | None
    no_ask: int | None
    depth: float
    vwap_dc: float | None
    range_dc: float | None
    extension: float | None
    velocity_dc: float | None
    book_age_s: float
    #: (seconds_to_close, mid_dc, vwap_dc) sampled across the window, for the
    #: chart. Held here rather than recomputed in the browser so the line the
    #: operator sees is the same series the strategy was evaluated against.
    history: list[tuple[float, float, float | None]] = field(default_factory=list)
    #: Top of book both sides, for the depth ladder: [(price_dc, size), ...]
    yes_levels: list[tuple[int, float]] = field(default_factory=list)
    no_levels: list[tuple[int, float]] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)


class Desk:
    """One process-wide view of the live markets, refreshed on a timer."""

    def __init__(self, settings: Settings, *, size: int = 10, target_c: int = 15):
        self.settings = settings
        self.size = size
        self.target_c = target_c
        self.kill = KillSwitch(path=settings.kill_file)

        self._http = httpx.AsyncClient(timeout=10.0)
        signer: Signer | None = None
        if settings.has_market_data_creds:
            try:
                signer = Signer.from_pem(settings.md_key_id, settings.md_private_key)
            except InvalidPrivateKey as exc:
                log.warning("Platform key unusable (%s); polling via REST.", exc)
        self._signer = signer
        self.rest = KalshiClient(settings.rest_base, signer=signer, client=self._http)
        self.discovery = MarketDiscovery(self.rest, settings.series)
        self.feed = MarketFeed(
            settings.ws_url, self.rest, signer,
            poll_interval=settings.orderbook_poll_interval_s,
        )
        self.history = BookHistory()
        self.session = SessionStats()

        self.markets: dict[str, MarketView] = {}
        self.positions: list[PaperPosition] = []
        self.enabled = False
        self.strategy = "reversion"
        self.error: str | None = None
        self.last_refresh: float = 0.0
        self.started_at = time.time()

        #: One entry per market window, so a strategy cannot re-enter the same
        #: window every pass.
        self._taken: set[tuple[str, str]] = set()
        self._task: asyncio.Task | None = None
        #: ticker -> (open_time, [(seconds_to_close, mid, vwap)])
        #: Reset when the window rolls, because a chart spanning two contracts
        #: is two different markets drawn as one line.
        self._series: dict[str, tuple[float, list[tuple[float, float, float | None]]]] = {}
        self._last_sample: dict[str, float] = {}

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        self.feed.start()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.feed.stop()
        await self._http.aclose()

    async def _loop(self) -> None:
        while True:
            try:
                await self._refresh()
                self.error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad pass must not stop the desk
                log.exception("Desk refresh failed")
                self.error = str(exc)[:200]
            await asyncio.sleep(1.0)

    # ---------------- the pass ----------------

    async def _refresh(self) -> None:
        now = time.time()
        if now - self.last_refresh > self.settings.discovery_interval_s:
            markets = await self.discovery.refresh(self.settings.coins)
            self.feed.set_tickers(m.ticker for m in markets.values())
            self.last_refresh = now

        live = self.discovery.markets
        alive = {m.ticker for m in live.values()}
        self.session.prune(alive)
        for gone in [t for t in self._series if t not in alive]:
            del self._series[gone]
            self._last_sample.pop(gone, None)

        views: dict[str, MarketView] = {}
        for coin, market in live.items():
            book = self.feed.book(market.ticker)
            if book is None:
                continue
            mid = book.mid
            if mid is not None and not book.is_stale:
                fair = book.microprice() or mid
                self.history.observe(market.ticker, fair, now)
                self.session.observe(
                    market.ticker, fair,
                    book.depth("yes") + book.depth("no"),
                    market.open_time, now,
                )

            # One point every couple of seconds is plenty for a 15-minute
            # window and keeps the payload small enough to poll each second.
            if mid is not None and now - self._last_sample.get(market.ticker, 0) >= 2.0:
                self._last_sample[market.ticker] = now
                open_time, points = self._series.get(market.ticker, (None, []))
                if open_time != market.open_time:
                    points = []
                points.append(
                    (
                        market.seconds_to_close(),
                        float(mid),
                        self.session.vwap(market.ticker),
                    )
                )
                # A 15-minute window at one point per 2s is 450 points; the cap
                # only matters if a market runs long.
                self._series[market.ticker] = (market.open_time, points[-500:])

            view = MarketView(
                coin=coin,
                ticker=market.ticker,
                seconds_to_close=market.seconds_to_close(),
                mid_dc=mid,
                spread_dc=book.spread,
                yes_ask=book.yes_ask,
                no_ask=book.no_ask,
                depth=book.depth("yes") + book.depth("no"),
                vwap_dc=self.session.vwap(market.ticker),
                range_dc=self.session.range_dc(market.ticker),
                extension=(
                    None if mid is None
                    else self.session.extension(market.ticker, float(mid))
                ),
                velocity_dc=self.session.velocity_dc(market.ticker, now),
                book_age_s=book.age(),
                history=self._series.get(market.ticker, (0.0, []))[1],
                yes_levels=sorted(book.yes.items(), reverse=True)[:6],
                no_levels=sorted(book.no.items(), reverse=True)[:6],
            )

            ctx = MarketContext(
                coin=coin, ticker=market.ticker, book=book,
                seconds_to_close=view.seconds_to_close,
                window_seconds=market.window_seconds,
                fv_change_5s=self.history.change_over(market.ticker, 5),
                fv_change_20s=self.history.change_over(market.ticker, 20),
                fv_change_60s=self.history.change_over(market.ticker, 60),
                samples=self.history.samples(market.ticker),
                vwap_dc=view.vwap_dc,
                session_range_dc=view.range_dc,
                extension=view.extension,
                velocity_dc=view.velocity_dc,
                book_age_s=view.book_age_s,
                title=market.title,
            )

            # Every strategy is evaluated, not just the selected one -- seeing
            # what the others would have done is most of the value of watching.
            for name in strategy_names():
                signal = get_strategy(name).evaluate(ctx)
                if signal is not None:
                    view.signals.append(
                        {
                            "strategy": name,
                            "side": signal.side,
                            "confidence": round(signal.confidence, 3),
                            "price_dc": signal.price_dc,
                            "reason": signal.reason,
                            "detail": signal.detail,
                        }
                    )
                    if name == self.strategy:
                        self._maybe_open(view, signal, now)

            views[market.ticker] = view

        self.markets = views
        self._manage(now)

    # ---------------- paper trading ----------------

    def _maybe_open(self, view: MarketView, signal: Signal, now: float) -> None:
        from ..kalshi.fees import fee_dc

        if not self.enabled or self.kill.engaged:
            return
        key = (self.strategy, view.ticker)
        if key in self._taken:
            return
        if any(p.outcome == "open" and p.ticker == view.ticker for p in self.positions):
            return

        book = self.feed.book(view.ticker)
        if book is None or book.is_stale:
            return
        available = int(book.size_at_ask(signal.side))
        count = min(self.size, available)
        if count < 1:
            return

        self._taken.add(key)
        self.positions.append(
            PaperPosition(
                coin=view.coin, ticker=view.ticker, side=signal.side,
                count=count, entry_dc=signal.price_dc,
                entry_fee_dc=fee_dc(count, signal.price_dc),
                target_dc=min(999, signal.price_dc + self.target_c * 10),
                strategy=self.strategy, reason=signal.reason, opened_at=now,
            )
        )

    def _manage(self, now: float) -> None:
        from ..kalshi.fees import fee_dc

        for pos in self.positions:
            if pos.outcome != "open":
                continue
            book = self.feed.book(pos.ticker)
            view = self.markets.get(pos.ticker)
            if book is None or book.is_stale:
                continue
            bid = book.best_bid(pos.side)
            if bid is not None and bid >= pos.target_dc:
                pos.exit_dc = pos.target_dc
                pos.exit_fee_dc = fee_dc(pos.count, pos.target_dc)
                pos.outcome = "target"
                pos.closed_at = now
                continue
            # Flatten before expiry rather than carrying a scalp into a
            # settlement nobody chose. Measured on recorded books, the losing
            # side's bid disappears entirely in the last twenty seconds.
            if view is not None and view.seconds_to_close <= 45 and bid is not None:
                pos.exit_dc = bid
                pos.exit_fee_dc = fee_dc(pos.count, bid) if bid > 0 else 0
                pos.outcome = "flattened"
                pos.closed_at = now

    # ---------------- serialisation ----------------

    def snapshot(self) -> dict:
        closed = [p for p in self.positions if p.outcome != "open"]
        net = sum(p.net_dc for p in closed)
        wins = sum(1 for p in closed if p.net_dc > 0)
        kill = self.kill.state()
        return {
            "mode": self.settings.mode.value,
            "mode_label": self.settings.mode.label,
            "host": self.settings.rest_base,
            "places_real_orders": self.settings.places_real_orders,
            "risks_real_money": self.settings.risks_real_money,
            "enabled": self.enabled,
            "strategy": self.strategy,
            "strategies": strategy_names(),
            "size": self.size,
            "target_c": self.target_c,
            "kill": {"engaged": kill.engaged, "detail": kill.describe()},
            "feed": "websocket" if self.feed.connected else "REST polling",
            "error": self.error,
            "uptime_s": round(time.time() - self.started_at),
            "markets": [
                {
                    "coin": v.coin, "ticker": v.ticker,
                    "seconds_to_close": round(v.seconds_to_close),
                    "mid_dc": None if v.mid_dc is None else round(v.mid_dc, 1),
                    "spread_dc": v.spread_dc,
                    "yes_ask": v.yes_ask, "no_ask": v.no_ask,
                    "depth": round(v.depth),
                    "vwap_dc": None if v.vwap_dc is None else round(v.vwap_dc, 1),
                    "range_dc": None if v.range_dc is None else round(v.range_dc, 1),
                    "extension": None if v.extension is None else round(v.extension, 3),
                    "velocity_dc": (
                        None if v.velocity_dc is None else round(v.velocity_dc, 1)
                    ),
                    "book_age_s": round(v.book_age_s, 1),
                    "history": [
                        [round(s), round(m, 1), None if w is None else round(w, 1)]
                        for s, m, w in v.history
                    ],
                    "yes_levels": [[p, round(q, 1)] for p, q in v.yes_levels],
                    "no_levels": [[p, round(q, 1)] for p, q in v.no_levels],
                    "signals": v.signals,
                }
                for v in sorted(self.markets.values(), key=lambda m: m.coin)
            ],
            "positions": [
                {
                    "coin": p.coin, "ticker": p.ticker, "side": p.side,
                    "count": p.count, "entry_dc": p.entry_dc,
                    "target_dc": p.target_dc, "exit_dc": p.exit_dc,
                    "outcome": p.outcome, "strategy": p.strategy,
                    "reason": p.reason,
                    "fees_dc": p.entry_fee_dc + p.exit_fee_dc,
                    "net_dc": p.net_dc,
                    "age_s": round((p.closed_at or time.time()) - p.opened_at),
                }
                for p in sorted(self.positions, key=lambda p: -p.opened_at)
            ],
            "pnl": {
                "closed": len(closed),
                "open": sum(1 for p in self.positions if p.outcome == "open"),
                "wins": wins,
                "win_rate": (wins / len(closed)) if closed else None,
                "net_dc": net,
                "fees_dc": sum(p.entry_fee_dc + p.exit_fee_dc for p in closed),
            },
        }
