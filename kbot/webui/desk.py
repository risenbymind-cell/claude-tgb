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
import hashlib
import json
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from ..config import Settings
from ..kalshi.auth import InvalidPrivateKey, Signer
from ..kalshi.fees import fee_dc
from ..kalshi.rest import KalshiClient
from ..kalshi.session import SessionStats
from ..kalshi.ws import BookHistory, MarketFeed
from ..safety import KillSwitch, TradingMode, measure_clock_drift
from ..strategy import MarketContext, Signal, get_strategy, strategy_names
from ..engine.broker import IntentRecorder, LiveBroker, PaperBroker
from ..engine.discovery import MarketDiscovery

log = logging.getLogger(__name__)

AUDIT_MAXLEN = 500
DECISIONS_MAXLEN = 300
#: A balance only moves when a fill happens; polling it every pass would spend
#: rate limit on an unchanged number.
BALANCE_INTERVAL_S = 15.0

#: Two distinct typed phrases so arming cannot happen by holding one key or
#: pasting one clipboard entry twice. Neither phrase alone can move production
#: money -- the real gate is still TRADING_MODE + ALLOW_PRODUCTION_ORDERS on
#: the process that started this desk (see kbot.safety.resolve_mode). This UI
#: flow exposes that gate; it cannot widen it.
PRODUCTION_ACK_A = "I UNDERSTAND THIS TRADES REAL MONEY"
PRODUCTION_ACK_B = "ENABLE PRODUCTION LIVE"
ARM_WINDOW_S = 300.0

SANDBOX_REFUSAL = (
    "This is a sandbox instance. It has no credentials, cannot connect any, "
    "and cannot place an order on any exchange."
)


def _mask(key_id: str) -> str:
    if len(key_id) <= 8:
        return "*" * len(key_id)
    return key_id[:4] + "…" + key_id[-4:]


@dataclass
class PaperPosition:
    """One position taken by the desk, simulated or real.

    The name is historical -- it now carries live positions too. `live` is the
    field that says which, and it is set from the broker that actually filled
    the entry rather than from the configured mode, so a position can never
    claim to be real because of what the settings said.
    """

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
    #: The id sent to the exchange (live) or generated locally (paper). Same
    #: shape either way so the ledger column reads the same.
    client_order_id: str = field(
        default_factory=lambda: f"paper-{secrets.token_hex(4)}"
    )
    #: True only when a broker that talks to an exchange filled this entry.
    live: bool = False
    #: Exchange-assigned ids, when there are any.
    entry_order_id: str | None = None
    exit_order_id: str | None = None
    #: Set when an exit was attempted and refused, so a position that could not
    #: be closed is visible rather than silently sitting open.
    exit_error: str | None = None

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
    #: Resting size on each side at the top levels, split out from `depth` so
    #: order-book imbalance can be shown rather than just total liquidity.
    yes_depth: float
    no_depth: float
    #: (yes_depth - no_depth) / total, in [-1, 1]. Positive means more resting
    #: size wants to buy YES than NO.
    imbalance: float | None
    vwap_dc: float | None
    range_dc: float | None
    extension: float | None
    velocity_dc: float | None
    #: Fair-value change over the last 20s, in deci-cents -- the same window
    #: the strategies themselves read as `fv_change_20s`.
    mom20_dc: float | None
    #: Round-trip fee (open + close) for the desk's *currently configured*
    #: size at the current ask, in deci-cents. What the mid would have to move
    #: past before a scalp at this size is worth anything.
    rt_fee_dc: int | None
    book_age_s: float
    #: (seconds_to_close, mid_dc, vwap_dc) sampled across the window, for the
    #: chart. Held here rather than recomputed in the browser so the line the
    #: operator sees is the same series the strategy was evaluated against.
    history: list[tuple[float, float, float | None]] = field(default_factory=list)
    #: Top of book both sides, for the depth ladder: [(price_dc, size), ...]
    yes_levels: list[tuple[int, float]] = field(default_factory=list)
    no_levels: list[tuple[int, float]] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)


class SandboxLocked(RuntimeError):
    """Attempted to reach past the sandbox boundary."""


class Desk:
    """One process-wide view of the live markets, refreshed on a timer.

    `sandbox=True` is the mode for a publicly reachable demo. It is not a
    softer setting than paper -- it is paper with the exits welded shut. The
    difference matters because paper mode is one POST away from being
    something else: the desk exposes routes that connect credentials and arm
    production, and a public instance is reachable by people who are not the
    operator. In sandbox, those routes refuse rather than being merely hidden
    in the UI, because hiding a control is not the same as removing it.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        size: int = 10,
        target_c: int = 15,
        sandbox: bool = False,
    ):
        self.settings = settings
        self.size = size
        self.target_c = target_c
        self.sandbox = sandbox
        if sandbox and settings.mode.places_real_orders:
            # Refused rather than silently downgraded: an operator who set
            # both has contradictory intentions, and guessing which one they
            # meant is how a demo ends up spending money.
            raise SandboxLocked(
                f"Sandbox mode cannot run with TRADING_MODE="
                f"{settings.mode.value}. A sandbox never places orders; set "
                "TRADING_MODE=paper or drop --sandbox."
            )
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

        #: When true, the selected strategy's signals are logged as decisions
        #: exactly as normal but never opened as a position -- for watching
        #: what a strategy *would* do without it acting.
        self.shadow = False
        #: Cleared by a restart. Arming here can only ever expose the real
        #: backend gate (mode + ALLOW_PRODUCTION_ORDERS at process start); it
        #: never changes what that gate allows.
        self.production_armed = False
        self._armed_a_at: float | None = None
        self.connected_key_id: str | None = None
        self._last_clock_check: dict | None = None

        #: Opened lazily, and only when something actually needs to be written
        #: down durably -- paper mode never touches a database.
        self._storage = None
        #: Rebuilt whenever the credentials or the mode change; see `broker()`.
        self._broker = None
        self._broker_key: tuple | None = None
        self.balance_dc: int | None = None
        self._last_balance_at: float = 0.0

        #: Every command, kill, arming step and boot -- newest first, capped.
        self.audit: deque[dict] = deque(maxlen=AUDIT_MAXLEN)
        #: One entry per market per pass for the *selected* strategy, fired or
        #: not -- this is what makes "why didn't it trade" answerable.
        self.decisions: deque[dict] = deque(maxlen=DECISIONS_MAXLEN)

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
        self.log_audit("system", "engine_boot", {
            "mode": self.settings.mode.value,
            "coins": self.settings.coins,
        })

    # ---------------- the broker ----------------

    def blocked_reason(self) -> str | None:
        """Why a real order cannot be sent right now, or None if one can.

        Checked immediately before every submission rather than cached. The
        order of these matters: the kill switch comes first because it is the
        one an operator reaches for in an emergency, and it must win over
        every other consideration.
        """
        if self.kill.engaged:
            return "kill switch engaged"
        if self.sandbox:
            return None  # sandbox: not blocked, just permanently simulated
        if not self.settings.mode.places_real_orders:
            return None  # paper: not blocked, just simulated
        if self._signer is None:
            return "no Kalshi credentials connected"
        if self.settings.mode.risks_real_money and not self.production_armed:
            # Demo deliberately does not require this. Demo funds are not
            # money, and requiring a ceremony to spend them would train the
            # operator to click through the ceremony that guards real money.
            return "production mode requires arming on the Risk tab"
        return None

    def broker(self):
        """The broker to trade through, rebuilt when the inputs change.

        Paper mode, or missing credentials, gets a `PaperBroker` -- the same
        object the Telegram bot uses, so simulated fills here are simulated the
        same way everywhere. Anything that reaches an exchange gets a
        `LiveBroker` with a durable intent log behind it, because the dangerous
        window is between the exchange accepting an order and this process
        writing down that it did.
        """
        # The last line of defence. Every route into a live broker is already
        # closed in sandbox; this makes it structurally impossible rather than
        # merely unreached, so a future route added without thinking about the
        # sandbox still cannot produce one.
        live = (
            not self.sandbox
            and self.settings.mode.places_real_orders
            and self._signer is not None
        )
        key = (live, self.connected_key_id, self.settings.mode.value)
        if self._broker is not None and self._broker_key == key:
            return self._broker

        if not live:
            self._broker = PaperBroker()
        else:
            from ..storage import Storage

            if self._storage is None:
                self._storage = Storage(self.settings.db_path, self.settings.master_key)
            self._broker = LiveBroker(
                self.rest,
                # tg_id 0 is the desk itself -- it has no Telegram user, and
                # the intent log is keyed by user elsewhere.
                intents=IntentRecorder(self._storage, 0, self.settings.mode.value),
            )
        self._broker_key = key
        self.log_audit("system", "broker_selected", {
            "live": live, "mode": self.settings.mode.value,
        })
        return self._broker

    # ---------------- audit ----------------

    def log_audit(self, actor: str, action: str, detail: dict | None = None) -> None:
        self.audit.appendleft({
            "ts": time.time(), "actor": actor, "action": action,
            "detail": detail or {},
        })

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.feed.stop()
        await self._http.aclose()
        if self._storage is not None:
            self._storage.close()

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

            yes_depth = book.depth("yes")
            no_depth = book.depth("no")
            total_depth = yes_depth + no_depth
            rt_ask = book.yes_ask if book.yes_ask is not None else book.no_ask
            view = MarketView(
                coin=coin,
                ticker=market.ticker,
                seconds_to_close=market.seconds_to_close(),
                mid_dc=mid,
                spread_dc=book.spread,
                yes_ask=book.yes_ask,
                no_ask=book.no_ask,
                depth=total_depth,
                yes_depth=yes_depth,
                no_depth=no_depth,
                imbalance=(
                    None if total_depth <= 0
                    else (yes_depth - no_depth) / total_depth
                ),
                vwap_dc=self.session.vwap(market.ticker),
                range_dc=self.session.range_dc(market.ticker),
                extension=(
                    None if mid is None
                    else self.session.extension(market.ticker, float(mid))
                ),
                velocity_dc=self.session.velocity_dc(market.ticker, now),
                mom20_dc=self.history.change_over(market.ticker, 20),
                rt_fee_dc=(
                    None if rt_ask is None
                    else fee_dc(self.size, rt_ask) * 2
                ),
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
                        await self._maybe_open(view, signal, now)
                # Only the selected strategy's non-fires are logged as
                # decisions -- logging every strategy's every pass would be
                # ~4x the volume for signals nobody is trading on.
                if name == self.strategy:
                    self.decisions.appendleft({
                        "ts": now, "coin": coin, "ticker": market.ticker,
                        "strategy": name,
                        "fired": signal is not None,
                        "side": None if signal is None else signal.side,
                        "confidence": (
                            None if signal is None else round(signal.confidence, 3)
                        ),
                        "reason": signal.reason if signal is not None else "no_signal",
                        "extension": view.extension,
                        "velocity_dc": view.velocity_dc,
                        "seconds_to_close": round(view.seconds_to_close),
                    })

            views[market.ticker] = view

        self.markets = views
        await self._manage(now)
        await self._refresh_balance(now)

    async def _refresh_balance(self, now: float) -> None:
        """Poll the account balance, but only when there is an account.

        Paper mode has no balance to read, and polling one every second on a
        live account spends rate limit on a number that changes only when a
        fill happens.
        """
        if not self.settings.mode.places_real_orders or self._signer is None:
            self.balance_dc = None
            return
        if now - self._last_balance_at < BALANCE_INTERVAL_S:
            return
        self._last_balance_at = now
        try:
            self.balance_dc = await self.broker().balance()
        except Exception as exc:  # noqa: BLE001 - a balance is not worth a stall
            log.warning("Balance lookup failed: %s", exc)

    # ---------------- paper trading ----------------

    async def _maybe_open(self, view: MarketView, signal: Signal, now: float) -> None:
        if not self.enabled:
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

        # Claimed before anything can fail, so a refusal does not re-fire on
        # every pass for the rest of the window.
        self._taken.add(key)

        if self.shadow:
            # Observe without acting: the signal still claims the window (so
            # it is not re-logged every pass) but no order is sent.
            self.log_audit("engine", "shadow_signal", {
                "coin": view.coin, "ticker": view.ticker, "side": signal.side,
                "confidence": round(signal.confidence, 3), "reason": signal.reason,
            })
            return

        blocked = self.blocked_reason()
        if blocked is not None:
            self.log_audit("engine", "order_blocked", {
                "coin": view.coin, "ticker": view.ticker, "side": signal.side,
                "reason": blocked,
            })
            return

        broker = self.broker()
        result = await broker.buy(
            view.ticker, signal.side, count, signal.price_dc, book
        )
        if not result.ok:
            self.log_audit("engine", "entry_rejected", {
                "coin": view.coin, "ticker": view.ticker, "side": signal.side,
                "count": count, "error": result.error,
            })
            return

        filled = result.filled or count
        entry_dc = result.price_dc if result.price_dc is not None else signal.price_dc
        self.positions.append(
            PaperPosition(
                coin=view.coin, ticker=view.ticker, side=signal.side,
                count=filled, entry_dc=entry_dc,
                entry_fee_dc=result.fee_dc or fee_dc(filled, entry_dc),
                target_dc=min(999, entry_dc + self.target_c * 10),
                strategy=self.strategy, reason=signal.reason, opened_at=now,
                live=not broker.paper,
                entry_order_id=result.order_id,
                client_order_id=result.order_id or f"paper-{secrets.token_hex(4)}",
            )
        )
        self.log_audit("engine", "entry_filled", {
            "coin": view.coin, "ticker": view.ticker, "side": signal.side,
            "count": filled, "price_dc": entry_dc, "live": not broker.paper,
            "order_id": result.order_id,
        })

    async def _manage(self, now: float) -> None:
        for pos in self.positions:
            if pos.outcome != "open":
                continue
            book = self.feed.book(pos.ticker)
            view = self.markets.get(pos.ticker)
            if book is None or book.is_stale:
                continue
            bid = book.best_bid(pos.side)
            if bid is not None and bid >= pos.target_dc:
                await self._close(pos, pos.target_dc, "target", now, book)
                continue
            # Flatten before expiry rather than carrying a scalp into a
            # settlement nobody chose. Measured on recorded books, the losing
            # side's bid disappears entirely in the last twenty seconds.
            if view is not None and view.seconds_to_close <= 45 and bid is not None:
                await self._close(pos, bid, "flattened", now, book)

    async def _close(
        self, pos: PaperPosition, price_dc: int, outcome: str, now: float, book
    ) -> None:
        """Close one position, sending a real sell when it is a real position.

        A live position must not be marked closed on the strength of a local
        decision -- the contracts are still on the exchange until it says
        otherwise. So a failed exit leaves the position open and visible, and
        is retried on the next pass, rather than being written off.
        """
        if not pos.live:
            pos.exit_dc = price_dc
            pos.exit_fee_dc = fee_dc(pos.count, price_dc) if price_dc > 0 else 0
            pos.outcome = outcome
            pos.closed_at = now
            return

        if self.kill.engaged:
            # Closing reduces exposure, but the kill switch means "send
            # nothing", and a half-honoured kill switch is not one. The
            # position stays open and flagged.
            pos.exit_error = "kill switch engaged; exit not sent"
            return

        result = await self.broker().sell(
            pos.ticker, pos.side, pos.count, price_dc, book
        )
        if not result.ok:
            pos.exit_error = result.error
            self.log_audit("engine", "exit_rejected", {
                "coin": pos.coin, "ticker": pos.ticker,
                "price_dc": price_dc, "error": result.error,
            })
            return

        pos.exit_error = None
        pos.exit_order_id = result.order_id
        pos.exit_dc = result.price_dc if result.price_dc is not None else price_dc
        pos.exit_fee_dc = result.fee_dc or (
            fee_dc(pos.count, pos.exit_dc) if pos.exit_dc > 0 else 0
        )
        pos.outcome = outcome
        pos.closed_at = now
        self.log_audit("engine", "exit_filled", {
            "coin": pos.coin, "ticker": pos.ticker, "outcome": outcome,
            "price_dc": pos.exit_dc, "order_id": result.order_id,
        })

    # ---------------- production arming ----------------
    #
    # Two independent typed phrases, mirroring the two independent
    # environment variables the process itself was started with
    # (TRADING_MODE=production-live and ALLOW_PRODUCTION_ORDERS=<ack>). This
    # UI flow can only *expose* that gate -- step B fails outright unless the
    # process is already running in production-live mode, because arming a
    # dashboard cannot change what host and credentials this process holds.

    def arm_step_a(self, phrase: str) -> dict:
        if self.sandbox:
            return {"ok": False, "error": SANDBOX_REFUSAL}
        if phrase != PRODUCTION_ACK_A:
            self.log_audit("operator", "arm_step_a_rejected", {})
            return {"ok": False, "error": "phrase does not match"}
        self._armed_a_at = time.time()
        self.log_audit("operator", "arm_step_a", {})
        return {"ok": True}

    def arm_step_b(self, phrase: str) -> dict:
        if self.sandbox:
            return {"ok": False, "error": SANDBOX_REFUSAL}
        if self._armed_a_at is None or time.time() - self._armed_a_at > ARM_WINDOW_S:
            self._armed_a_at = None
            return {"ok": False, "error": "step one has expired or was not completed"}
        if phrase != PRODUCTION_ACK_B:
            self.log_audit("operator", "arm_step_b_rejected", {})
            return {"ok": False, "error": "phrase does not match"}
        if not self.settings.mode.risks_real_money:
            self._armed_a_at = None
            self.log_audit("operator", "arm_step_b_rejected", {
                "reason": f"process is running in {self.settings.mode.value} mode",
            })
            return {
                "ok": False,
                "error": (
                    f"This process was started in {self.settings.mode.value} "
                    "mode. Arming the desk cannot change that -- restart it "
                    "with TRADING_MODE=production-live and "
                    "ALLOW_PRODUCTION_ORDERS set to place real orders."
                ),
            }
        self.production_armed = True
        self._armed_a_at = None
        self.log_audit("operator", "arm_step_b", {})
        return {"ok": True}

    def disarm(self) -> None:
        self.production_armed = False
        self._armed_a_at = None
        self.log_audit("operator", "disarm", {})

    # ---------------- credentials ----------------

    def connect_keys(self, key_id: str, private_key_pem: str) -> dict:
        if self.sandbox:
            # A public demo must not become a place people paste private keys.
            # Refusing before parsing means the key is never even loaded into
            # a cryptography object, let alone written anywhere.
            self.log_audit("operator", "connect_refused_sandbox", {})
            return {"ok": False, "error": SANDBOX_REFUSAL}
        key_id = key_id.strip()
        pem = private_key_pem.strip()
        if not key_id or not pem:
            return {"ok": False, "error": "key id and private key are both required"}
        try:
            signer = Signer.from_pem(key_id, pem)
        except InvalidPrivateKey as exc:
            self.log_audit("operator", "connect_rejected", {"error": str(exc)[:200]})
            return {"ok": False, "error": f"private key unusable: {exc}"}

        from cryptography.fernet import Fernet

        fernet = Fernet(self.settings.master_key.encode())
        encrypted = fernet.encrypt(
            json.dumps({"key_id": key_id, "pem": pem}).encode()
        )
        path = self.settings.db_path.parent / "desk_key.enc"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encrypted)

        self._signer = signer
        self.rest = KalshiClient(self.settings.rest_base, signer=signer, client=self._http)
        self.discovery = MarketDiscovery(self.rest, self.settings.series)
        self.feed = MarketFeed(
            self.settings.ws_url, self.rest, signer,
            poll_interval=self.settings.orderbook_poll_interval_s,
        )
        if self._task is not None:
            self.feed.start()

        # The broker is keyed on the credentials, so this forces a rebuild --
        # a cached broker holding the old key would keep signing with it.
        self._broker = None
        self._broker_key = None
        self._last_balance_at = 0.0

        fingerprint = hashlib.sha256(pem.encode()).hexdigest()[:12]
        self.connected_key_id = key_id
        self.log_audit("operator", "connect", {
            "key_id": _mask(key_id), "fingerprint": fingerprint,
            "host": self.settings.rest_base,
        })
        return {"ok": True, "key_id": _mask(key_id), "fingerprint": fingerprint}

    # ---------------- health / reconcile ----------------

    async def health(self) -> dict:
        check = await measure_clock_drift(self.settings.rest_base, client=self._http)
        self._last_clock_check = {
            "ok": check.ok, "drift_s": check.drift_s, "detail": check.detail,
        }
        self.log_audit("operator", "health_check", self._last_clock_check)
        return self._last_clock_check

    async def reconcile(self) -> dict:
        """Compare what this desk thinks it holds against what Kalshi says.

        The ledger is this process's memory; the exchange is the truth. In
        paper mode there is nothing to compare against, so it says so rather
        than reporting a clean reconciliation it did not perform.
        """
        self.last_refresh = 0.0

        if not self.settings.mode.places_real_orders or self._signer is None:
            self.log_audit("operator", "reconcile", {"live": False})
            return {
                "ok": True, "live": False,
                "detail": (
                    "Paper mode holds no exchange positions -- forced a fresh "
                    "discovery pass instead."
                ),
            }

        try:
            rows = await self.rest.get_positions()
        except Exception as exc:  # noqa: BLE001 - reported, not raised at a button
            self.log_audit("operator", "reconcile_failed", {"error": str(exc)[:200]})
            return {"ok": False, "error": f"could not read positions: {exc}"}

        from ..kalshi.prices import parse_count

        held: dict[str, float] = {}
        for row in rows:
            ticker = row.get("ticker")
            if not ticker:
                continue
            for field_name in ("position_fp", "position"):
                if row.get(field_name) is not None:
                    try:
                        held[ticker] = parse_count(row[field_name])
                    except Exception:  # noqa: BLE001
                        pass
                    break

        mine = {
            p.ticker: p.count
            for p in self.positions
            if p.outcome == "open" and p.live
        }
        divergences = []
        for ticker, count in mine.items():
            actual = held.get(ticker, 0.0)
            if abs(actual) < 0.01:
                divergences.append(
                    f"{ticker}: desk holds {count}, exchange holds none"
                )
            elif abs(abs(actual) - count) >= 0.01:
                divergences.append(
                    f"{ticker}: desk holds {count}, exchange holds {actual:g}"
                )
        for ticker, actual in held.items():
            if abs(actual) >= 0.01 and ticker not in mine:
                divergences.append(
                    f"{ticker}: exchange holds {actual:g}, desk is not tracking it"
                )

        self.log_audit("operator", "reconcile", {
            "live": True, "checked": len(mine), "divergences": len(divergences),
        })
        return {
            "ok": True, "live": True,
            "detail": (
                f"Checked {len(mine)} tracked position(s) against "
                f"{len(held)} on the exchange. "
                + ("Ledger matches." if not divergences
                   else " | ".join(divergences))
            ),
            "divergences": divergences,
        }

    # ---------------- serialisation ----------------

    def _posture_why(self) -> str:
        """One sentence on what would actually happen if a signal fired now."""
        blocked = self.blocked_reason()
        if self.sandbox:
            return (
                "Sandbox: real Kalshi market data, simulated fills. This "
                "instance holds no credentials and cannot place an order."
            )
        if self.shadow:
            return "Shadow mode: signals are logged, no orders are sent."
        if not self.settings.mode.places_real_orders:
            return (
                "Paper: fills are simulated against the real production book. "
                "Nothing is sent to Kalshi."
            )
        if blocked is not None:
            return f"No orders will be sent -- {blocked}."
        if self.settings.mode.risks_real_money:
            return "Armed. Signals place real orders with your own money."
        return (
            "Demo-live: signals place real orders on Kalshi's demo exchange, "
            "spending demo funds. The demo book is thin, so P/L here is "
            "evidence about the order path, not about the strategy."
        )

    def snapshot(self) -> dict:
        closed = [p for p in self.positions if p.outcome != "open"]
        net = sum(p.net_dc for p in closed)
        wins = sum(1 for p in closed if p.net_dc > 0)
        kill = self.kill.state()
        armed_a = (
            self._armed_a_at is not None
            and time.time() - self._armed_a_at <= ARM_WINDOW_S
        )
        return {
            "mode": self.settings.mode.value,
            "mode_label": self.settings.mode.label,
            "host": self.settings.rest_base,
            "places_real_orders": self.settings.places_real_orders,
            "risks_real_money": self.settings.risks_real_money,
            "enabled": self.enabled,
            "shadow": self.shadow,
            "sandbox": self.sandbox,
            "strategy": self.strategy,
            "strategies": strategy_names(),
            "size": self.size,
            "target_c": self.target_c,
            "kill": {"engaged": kill.engaged, "detail": kill.describe()},
            "feed": "websocket" if self.feed.connected else "REST polling",
            "error": self.error,
            "uptime_s": round(time.time() - self.started_at),
            "live_posture": {
                "mode": self.settings.mode.value,
                "mode_label": self.settings.mode.label,
                "places_real_orders": self.settings.places_real_orders,
                "risks_real_money": self.settings.risks_real_money,
                "why": self._posture_why(),
                "blocked": self.blocked_reason(),
                "balance_dc": self.balance_dc,
                "feed": "websocket" if self.feed.connected else "REST polling",
                "clock": self._last_clock_check,
                "keys_connected": self.connected_key_id is not None,
                "connected_key_id": (
                    _mask(self.connected_key_id) if self.connected_key_id else None
                ),
                "production_armed": self.production_armed,
                "armed_step_a": armed_a,
                "sandbox": self.sandbox,
                "master_key_is_ephemeral": self.settings.master_key_is_ephemeral,
            },
            "audit": list(self.audit)[:150],
            "decisions": list(self.decisions)[:150],
            "markets": [
                {
                    "coin": v.coin, "ticker": v.ticker,
                    "seconds_to_close": round(v.seconds_to_close),
                    "mid_dc": None if v.mid_dc is None else round(v.mid_dc, 1),
                    "spread_dc": v.spread_dc,
                    "yes_ask": v.yes_ask, "no_ask": v.no_ask,
                    "depth": round(v.depth),
                    "yes_depth": round(v.yes_depth),
                    "no_depth": round(v.no_depth),
                    "imbalance": None if v.imbalance is None else round(v.imbalance, 3),
                    "vwap_dc": None if v.vwap_dc is None else round(v.vwap_dc, 1),
                    "range_dc": None if v.range_dc is None else round(v.range_dc, 1),
                    "extension": None if v.extension is None else round(v.extension, 3),
                    "velocity_dc": (
                        None if v.velocity_dc is None else round(v.velocity_dc, 1)
                    ),
                    "mom20_dc": None if v.mom20_dc is None else round(v.mom20_dc, 1),
                    "rt_fee_dc": v.rt_fee_dc,
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
                    "reason": p.reason, "client_order_id": p.client_order_id,
                    "live": p.live,
                    "entry_order_id": p.entry_order_id,
                    "exit_order_id": p.exit_order_id,
                    "exit_error": p.exit_error,
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
