"""The Telegram front end: commands, dashboard callbacks, and notifications."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..engine.risk import RiskManager, start_of_utc_day
from ..engine.runner import Engine
from ..kalshi.auth import InvalidPrivateKey, Signer
from ..kalshi.prices import format_cents, format_dollars
from ..kalshi.rest import KalshiClient, KalshiError
from ..payments import PaymentService
from ..storage import DEFAULT_SETTINGS, PRICES_USD, TIERS, Storage, User
from ..strategy import REGISTRY
from . import ui
from .api import TelegramClient, TelegramError
from .channel import ResultsChannel

log = logging.getLogger(__name__)

COMMANDS = [
    ("start", "Open the dashboard"),
    ("dashboard", "Open the dashboard"),
    ("buy", "Buy access with crypto"),
    ("redeem", "Redeem an access key"),
    ("connect", "Connect your Kalshi API key"),
    ("disconnect", "Remove your Kalshi API key"),
    ("positions", "Open positions and recent trades"),
    ("pnl", "Profit and loss summary"),
    ("status", "Bot and market status"),
    ("data", "Recording progress toward a verdict"),
    ("stats", "Performance breakdown"),
    ("stop", "Stop trading"),
    ("help", "How the bot works"),
]

WELCOME = """<b>DirectionalBot</b>

Signals on Kalshi's 15-minute crypto markets, read straight from the live order book.

<b>How it works</b>
1. Redeem an access key — /redeem YOUR-KEY
2. Pick your coins, size and risk caps on the dashboard
3. Start in <b>paper mode</b> — a live simulation at real prices, no money at risk
4. When you're satisfied, connect your own Kalshi API key and switch to live

You keep your funds in your own Kalshi account. The bot places orders through your API key and never holds or moves your money.

<i>Trading involves real risk of loss. Nothing here is financial advice, and no strategy is guaranteed to be profitable.</i>"""

HELP = """<b>Modes</b>
• <b>Manual</b> — you get every signal with a confidence read and trade it yourself.
• <b>Auto</b> — the bot places the order on your account the moment a signal fires.

<b>Paper vs live</b>
Paper mode runs the identical code path against the real production order book and simulates the fill. Nothing reaches Kalshi, and no account is needed. Because the liquidity is genuine, paper results are the ones worth measuring.

The bot as a whole runs in one of three modes, set by whoever operates it: <b>paper</b> (nothing sent), <b>demo-live</b> (real orders, Kalshi's demo balance, thin book — tests the plumbing, not the strategy), or <b>production-live</b> (real orders, real money). If the operator runs it in paper, your live toggle cannot override that. /health shows which one you are in.

<b>Exits</b>
A sell order goes in as soon as an entry fills — either a fixed number of cents above entry or an absolute target price. If neither fills, the window settles at 100c or 0c and the bot books the result.

<b>Risk caps</b>
Daily loss limit, max open exposure, a balance floor the bot won't spend below, and per-window trade limits. All of them are checked against your trade ledger, so restarting the bot does not reset them.

<b>Connecting Kalshi</b>
Create an API key at kalshi.com under Account → API Keys. You'll get a key ID and an RSA private key file. Send both with /connect. The private key is encrypted before it is stored, and your message is deleted from the chat straight after.

<b>Commands</b>
/dashboard · /positions · /pnl · /stats · /status · /health · /stop · /disconnect

<b>Admin</b>
/kill stops every order immediately, for everyone, and survives a restart. /resume clears it. /genkeys mints access keys.

<b>Results channel</b>
<code>/share on</code> posts your closed trades to the public results channel — anonymously, no username or account detail, and losses are posted alongside wins. Off by default.

<i>Signals are not advice. Losing windows happen. Only trade money you can afford to lose.</i>"""


# Which dashboard view each setting belongs to, so tapping a button re-renders
# the menu the user is standing in.
def STRATEGY_LABEL_FALLBACK(name: str) -> str:
    """Display name for a strategy, used by the results channel."""
    from ..engine.runner import STRATEGY_LABELS

    return STRATEGY_LABELS.get(name, name.title() if name else "—")


SETTING_VIEWS = {
    "strategy": "strategy",
    "contracts": "size",
    "min_confidence": "size",
    "max_entry_price": "size",
    "min_entry_price": "size",
    "exit_mode": "exit",
    "profit_cents": "exit",
    "target_price": "exit",
    "daily_loss_limit_cents": "risk",
    "max_exposure_cents": "risk",
    "balance_floor_cents": "risk",
    "max_trades_per_window": "risk",
    "max_open_positions": "risk",
}


@dataclass
class PendingInput:
    state: str
    data: dict[str, Any] = field(default_factory=dict)


class Bot:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage
        self.tg = TelegramClient(settings.telegram_token)
        self.engine = Engine(settings, storage, self.notify)
        self.payments = PaymentService(settings, storage, self.notify)
        self.results = ResultsChannel(
            self.tg,
            storage,
            settings.results_chat_id,
            post_losses=settings.results_post_losses,
            min_net_dc=settings.results_min_net_cents * 10,
        )
        # The engine publishes every resolved trade through this hook.
        self.engine.on_trade_closed = self._on_trade_closed
        self.risk = RiskManager(storage)
        self._pending: dict[int, PendingInput] = {}

    # ---------------- lifecycle ----------------

    async def run(self) -> None:
        me = await self.tg.get_me()
        log.info("Connected to Telegram as @%s", me.get("username"))
        await self.tg.set_my_commands(COMMANDS)
        await self.engine.start()
        await self.payments.start()
        try:
            async for update in self.tg.poll():
                # One slow handler must not stall the update stream.
                asyncio.create_task(self._safe_handle(update))
        finally:
            await self.payments.stop()
            await self.engine.stop()
            await self.tg.aclose()

    async def _safe_handle(self, update: dict) -> None:
        try:
            await self._handle(update)
        except Exception:  # noqa: BLE001 - a bad update must not kill the bot
            log.exception("Update handling failed: %s", update.get("update_id"))

    async def notify(self, tg_id: int, text: str) -> None:
        try:
            await self.tg.send_message(tg_id, text)
        except TelegramError as exc:
            if exc.is_blocked:
                # They removed the bot; stop trading for them rather than
                # continuing to trade an account nobody is watching.
                log.info("User %s blocked the bot; disabling trading.", tg_id)
                await self.storage.set_enabled(tg_id, False)
            else:
                log.warning("Notify failed for %s: %s", tg_id, exc)

    # ---------------- dispatch ----------------

    async def _handle(self, update: dict) -> None:
        if "message" in update:
            await self._on_message(update["message"])
        elif "callback_query" in update:
            await self._on_callback(update["callback_query"])

    async def _on_message(self, message: dict) -> None:
        chat = message.get("chat", {})
        if chat.get("type") != "private":
            return  # the dashboard is per-user; groups are not supported
        text = (message.get("text") or "").strip()
        if not text:
            return
        from_user = message.get("from", {})
        tg_id = from_user.get("id")
        if tg_id is None:
            return
        user = await self.storage.upsert_user(tg_id, from_user.get("username"))

        if text.startswith("/"):
            self._pending.pop(tg_id, None)
            await self._on_command(user, text, message)
            return

        pending = self._pending.get(tg_id)
        if pending is not None:
            await self._on_pending_input(user, pending, text, message)
            return

        await self._send_dashboard(user)

    async def _on_command(self, user: User, text: str, message: dict) -> None:
        parts = text.split()
        command = parts[0].lstrip("/").split("@")[0].lower()
        args = parts[1:]

        handlers = {
            "start": self._cmd_start,
            "dashboard": self._cmd_dashboard,
            "settings": self._cmd_dashboard,
            "help": self._cmd_help,
            "buy": self._cmd_buy,
            "redeem": self._cmd_redeem,
            "connect": self._cmd_connect,
            "disconnect": self._cmd_disconnect,
            "positions": self._cmd_positions,
            "pnl": self._cmd_pnl,
            "stats": self._cmd_stats,
            "share": self._cmd_share,
            "status": self._cmd_status,
            "stop": self._cmd_stop,
            "genkeys": self._cmd_genkeys,
            "keystats": self._cmd_keystats,
            "grant": self._cmd_grant,
            "sales": self._cmd_sales,
            "confirm": self._cmd_confirm,
            "kill": self._cmd_kill,
            "resume": self._cmd_resume,
            "health": self._cmd_health,
            "data": self._cmd_data,
        }
        handler = handlers.get(command)
        if handler is None:
            await self.tg.send_message(
                user.tg_id, "Unknown command. Try /help or /dashboard."
            )
            return
        await handler(user, args, message)

    # ---------------- commands ----------------

    async def _cmd_start(self, user: User, args: list[str], message: dict) -> None:
        await self.tg.send_message(user.tg_id, WELCOME)
        # Deep-link payloads: `/start buy_monthly` from the website's pricing
        # buttons, or `/start SOME-KEY` to redeem directly.
        if args:
            payload = args[0]
            if payload.lower().startswith("buy_"):
                await self._cmd_buy(user, [payload[4:]], message)
                return
            await self._cmd_redeem(user, args, message)
            return
        await self._send_dashboard(await self._reload(user))

    async def _cmd_help(self, user: User, args: list[str], message: dict) -> None:
        await self.tg.send_message(user.tg_id, HELP)

    async def _cmd_dashboard(self, user: User, args: list[str], message: dict) -> None:
        await self._send_dashboard(user)

    async def _cmd_buy(self, user: User, args: list[str], message: dict) -> None:
        if not self.payments.enabled:
            await self.tg.send_message(
                user.tg_id,
                "Payments aren't set up on this bot. Ask an admin for a key, "
                "then redeem it with <code>/redeem YOUR-KEY</code>.",
            )
            return

        tier = args[0].lower() if args else ""
        if tier not in TIERS:
            await self.tg.send_message(
                user.tg_id, ui.buy_text(self.settings), reply_markup=ui.buy_keyboard(self.settings)
            )
            return
        currency = args[1].upper() if len(args) > 1 else None
        await self._start_purchase(user, tier, currency)

    async def _start_purchase(
        self, user: User, tier: str, currency: str | None = None
    ) -> None:
        try:
            invoice = await self.payments.create_invoice(user.tg_id, tier, currency)
        except Exception:  # noqa: BLE001 - provider outage must not look like a bug
            log.exception("Invoice creation failed for %s", user.tg_id)
            await self.tg.send_message(
                user.tg_id,
                "⚠️ Couldn't reach the payment provider. Try again in a minute.",
            )
            return

        await self.tg.send_message(
            user.tg_id,
            ui.invoice_text(
                tier,
                self.settings.prices.get(tier, PRICES_USD[tier]),
                invoice,
                automatic=self.payments.automatic,
            ),
            reply_markup=ui.invoice_keyboard(invoice),
        )

    async def _cmd_sales(self, user: User, args: list[str], message: dict) -> None:
        if not self._is_admin(user):
            return
        revenue = await self.storage.revenue()
        if not revenue:
            await self.tg.send_message(user.tg_id, "No sales yet.")
            return
        lines = ["<b>Sales</b>", ""]
        total = 0.0
        for tier, (count, amount) in sorted(revenue.items()):
            total += amount
            lines.append(f"{tier}: {count} × = ${amount:,.2f}")
        lines += ["", f"<b>Total: ${total:,.2f}</b>"]
        await self.tg.send_message(user.tg_id, "\n".join(lines))

    async def _cmd_confirm(self, user: User, args: list[str], message: dict) -> None:
        """Manually settle an invoice — the admin side of manual payments."""
        if not self._is_admin(user):
            return
        if not args:
            pending = await self.storage.pending_invoices()
            if not pending:
                await self.tg.send_message(user.tg_id, "No pending invoices.")
                return
            lines = ["<b>Pending invoices</b>", ""]
            for inv in pending[:20]:
                lines.append(
                    f"<code>{inv.order_id}</code>\n"
                    f"  {inv.tier} ${inv.amount_usd:.2f} · user {inv.tg_id}"
                )
            lines += ["", "Confirm with <code>/confirm ORDER_ID</code>"]
            await self.tg.send_message(user.tg_id, "\n".join(lines))
            return

        key = await self.payments.settle(args[0])
        if key is None:
            await self.tg.send_message(user.tg_id, "❌ No such pending order.")
        else:
            await self.tg.send_message(
                user.tg_id, f"✅ Settled. Key <code>{key}</code> delivered."
            )

    async def _cmd_redeem(self, user: User, args: list[str], message: dict) -> None:
        if not args:
            await self.tg.send_message(
                user.tg_id, "Send it as <code>/redeem YOUR-KEY</code>."
            )
            return
        ok, detail = await self.storage.redeem_key(user.tg_id, args[0])
        if not ok:
            await self.tg.send_message(user.tg_id, f"❌ {detail}")
            return
        user = await self._reload(user)
        await self.tg.send_message(user.tg_id, f"✅ {detail}")
        await self._send_dashboard(user)

    async def _cmd_connect(self, user: User, args: list[str], message: dict) -> None:
        if not self._has_access(user):
            await self._deny(user)
            return
        self._pending[user.tg_id] = PendingInput("await_key_id")
        await self.tg.send_message(
            user.tg_id,
            "<b>Connect your Kalshi account</b>\n\n"
            "Create an API key at kalshi.com → Account → API Keys. You'll get a "
            "<b>key ID</b> and download an <b>RSA private key</b>.\n\n"
            "Send me the <b>key ID</b> now (send /cancel to stop).",
        )

    async def _cmd_disconnect(self, user: User, args: list[str], message: dict) -> None:
        await self.storage.clear_credentials(user.tg_id)
        await self.tg.send_message(
            user.tg_id,
            "🔓 Kalshi credentials removed and live trading stopped. "
            "Paper mode still works.",
        )

    async def _cmd_positions(self, user: User, args: list[str], message: dict) -> None:
        open_trades = await self.storage.open_trades(user.tg_id)
        recent = await self.storage.trades_since(user.tg_id, time.time() - 86400)
        await self.tg.send_message(
            user.tg_id,
            ui.positions_text(open_trades, recent),
            reply_markup=ui.positions_keyboard(),
        )

    async def _cmd_pnl(self, user: User, args: list[str], message: dict) -> None:
        days = 1
        if args and args[0].isdigit():
            days = max(1, min(90, int(args[0])))
        since = time.time() - days * 86400
        trades = await self.storage.trades_since(user.tg_id, since)
        label = "last 24h" if days == 1 else f"last {days} days"
        await self.tg.send_message(user.tg_id, ui.pnl_text(trades, label))

    async def _on_trade_closed(self, trade, strategy: str) -> None:
        """Publish a resolved trade if its owner has opted in."""
        if not self.results.enabled:
            return
        user = await self.storage.get_user(trade.tg_id)
        is_house = trade.tg_id in self.settings.admin_ids
        if not is_house and not (user and user.get("share_results")):
            return
        await self.results.post_trade(trade, strategy)

    async def _cmd_share(self, user: User, args: list[str], message: dict) -> None:
        """Opt in or out of having your results posted anonymously."""
        if not self.results.enabled:
            await self.tg.send_message(
                user.tg_id, "No results channel is configured on this bot."
            )
            return
        current = bool(user.get("share_results"))
        if args and args[0].lower() in {"on", "off"}:
            current = args[0].lower() == "on"
            await self.storage.update_settings(user.tg_id, {"share_results": current})
        else:
            current = not current
            await self.storage.update_settings(user.tg_id, {"share_results": current})
        await self.tg.send_message(
            user.tg_id,
            (
                "📡 Your closed trades will be posted to the results channel — "
                "anonymously, with no username, size or account detail. "
                "Losses are posted too.\nTurn it off with <code>/share off</code>."
                if current
                else "🔕 Your trades will not be posted to the results channel."
            ),
        )

    async def _cmd_stats(self, user: User, args: list[str], message: dict) -> None:
        days = 7
        if args and args[0].isdigit():
            days = max(1, min(90, int(args[0])))
        since = time.time() - days * 86400
        breakdown = await self.storage.strategy_breakdown(user.tg_id, since)
        hourly = await self.storage.hourly_breakdown(user.tg_id, since)
        outcomes = await self.storage.outcome_counts(user.tg_id, since)
        trades = await self.storage.trades_since(user.tg_id, since)
        await self.tg.send_message(
            user.tg_id, ui.stats_text(trades, breakdown, hourly, outcomes, days)
        )

    async def _cmd_status(self, user: User, args: list[str], message: dict) -> None:
        markets = self.engine.discovery.markets
        lines = [
            "<b>Status</b>",
            "",
            f"Order book feed: {'🟢 live websocket' if self.engine.feed.connected else '🟡 REST polling'}",
            f"Engine uptime: {int(self.engine.uptime_s // 60)} min",
            f"Your bot: {'🟢 running' if user.enabled else '⚪️ stopped'}",
            "",
            "<b>Live 15-minute markets</b>",
        ]
        if not markets:
            lines.append("<i>None discovered yet — the engine scans continuously.</i>")
        for coin in sorted(markets):
            market = markets[coin]
            book = self.engine.feed.book(market.ticker)
            secs = max(0, int(market.seconds_to_close()))
            mid = format_cents(round(book.mid)) if book and book.mid is not None else "—"
            lines.append(
                f"{coin}: YES mid {mid} · closes in {secs // 60}m {secs % 60:02d}s"
            )
        await self.tg.send_message(user.tg_id, "\n".join(lines))

    async def _cmd_stop(self, user: User, args: list[str], message: dict) -> None:
        await self.storage.set_enabled(user.tg_id, False)
        await self.tg.send_message(user.tg_id, "⏹ Trading stopped.")
        await self._send_dashboard(await self._reload(user))

    # ---------------- admin ----------------

    async def _cmd_kill(self, user: User, args: list[str], message: dict) -> None:
        """Stop every order immediately, for everyone.

        Admin-only and deliberately unconditional: there is no confirmation
        step, because the whole value of a kill switch is that it works on the
        first press by someone who is already alarmed. Turning it back on is
        the step that should be difficult, and that is /resume.
        """
        if not self._is_admin(user):
            await self.tg.send_message(user.tg_id, "Admins only.")
            return
        reason = " ".join(args).strip() or f"/kill by {user.tg_id}"
        state = self.engine.kill.engage(reason, source="telegram")
        await self.tg.send_message(
            user.tg_id,
            "🛑 <b>KILL SWITCH ENGAGED</b>\n\n"
            "No further orders will be placed by anyone. Open positions are "
            "left alone — close them yourself if you need to.\n\n"
            f"Reason: {html.escape(reason)}\n"
            f"Persisted to disk, so it survives a restart.\n\n"
            "Clear it with /resume.",
        )
        log.warning("Kill switch engaged from Telegram by %s", user.tg_id)
        _ = state

    async def _cmd_resume(self, user: User, args: list[str], message: dict) -> None:
        if not self._is_admin(user):
            await self.tg.send_message(user.tg_id, "Admins only.")
            return
        before = self.engine.kill.state()
        if not before.engaged:
            await self.tg.send_message(user.tg_id, "The kill switch is already clear.")
            return

        self.engine.kill.release()
        after = self.engine.kill.state()
        if after.engaged:
            # An environment-variable switch cannot be cleared from inside the
            # process, and saying "resumed" when it is still engaged would be
            # the most dangerous possible lie here.
            await self.tg.send_message(
                user.tg_id,
                "⚠️ Still engaged — "
                f"{html.escape(after.describe())}.\n\n"
                "This one is set outside the bot. Unset KILL_SWITCH in the "
                "environment and restart.",
            )
            return
        await self.tg.send_message(
            user.tg_id, "✅ Kill switch cleared. Trading resumes on the next signal."
        )

    async def _cmd_health(self, user: User, args: list[str], message: dict) -> None:
        """One screen answering: is it safe, is it fed, is it alive."""
        kill = self.engine.kill.state()
        mode = self.settings.mode
        feed = self.engine.feed

        books = getattr(feed, "books", {}) or {}
        fresh = sum(1 for b in books.values() if not b.is_stale)
        desynced = sum(1 for b in books.values() if getattr(b, "desynced", False))
        connected = getattr(feed, "_connected", False)

        lines = [
            "<b>Health</b>",
            "",
            f"Mode: <b>{html.escape(mode.label)}</b>",
            f"Kill switch: <b>{html.escape(kill.describe())}</b>",
            "",
            f"Feed: {'websocket' if connected else 'REST polling'}",
            f"Books: {fresh}/{len(books)} usable",
        ]
        if desynced:
            lines.append(f"⚠️ {desynced} book(s) desynced, awaiting re-snapshot")
        await self.tg.send_message(user.tg_id, "\n".join(lines))

    async def _cmd_data(self, user: User, args: list[str], message: dict) -> None:
        """Whether the recorder is running, and how far the data is from
        supporting a conclusion.

        On a phone deliberately. The recorder has to run for days before any
        strategy question can be answered, and a thing that needs watching for
        days is a thing worth being able to check from a bus stop -- the last
        run stopped after two hours and nobody noticed for a day and a half.
        """
        import os
        from pathlib import Path

        directory = Path(
            os.getenv(
                "RECORDINGS_DIR", self.settings.db_path.parent / "recordings"
            )
        )
        # Cached and shared. Reading it parses every recording on disk, so an
        # uncached command lets any subscriber pull the whole corpus into
        # memory as fast as they can send messages.
        cached = getattr(self, "_inventory_cache", None)
        if cached and time.time() - cached[0] < 60:
            inv = cached[1]
        else:
            try:
                from ..research.inventory import take_inventory

                inv = await asyncio.to_thread(take_inventory, directory)
            except Exception as exc:  # noqa: BLE001 - a status command must answer
                await self.tg.send_message(
                    user.tg_id, f"Could not read {html.escape(str(directory))}: "
                    f"{html.escape(str(exc)[:200])}"
                )
                return
            self._inventory_cache = (time.time(), inv)

        from ..research.signals import MIN_WINDOWS

        if inv.settled == 0:
            await self.tg.send_message(
                user.tg_id,
                "<b>Recordings</b>\n\nNothing recorded yet.\n\n"
                "Start with <code>python -m kbot.research record</code> — it "
                "needs no API keys.",
            )
            return

        age = None if inv.last_sample is None else time.time() - inv.last_sample
        running = age is not None and age < 300
        if age is None:
            seen = "never"
        elif age < 120:
            seen = f"{age:.0f}s ago"
        elif age < 7200:
            seen = f"{age / 60:.0f} minutes ago"
        else:
            seen = f"{age / 3600:.1f} hours ago"

        filled = int(round(min(1.0, inv.progress) * 20))
        bar = "█" * filled + "░" * (20 - filled)

        lines = [
            "<b>Recordings</b>",
            "",
            f"{'🟢 recording' if running else '🔴 not running'} · last sample {seen}",
            "",
            f"<code>{bar}</code> {100 * inv.progress:.0f}%",
            f"{inv.settled}/{MIN_WINDOWS} settled markets",
            f"{inv.yes} yes / {inv.no} no",
        ]

        if inv.is_one_sided:
            lines += [
                "",
                f"⚠️ {100 * inv.one_sided_share:.0f}% settled the same way — "
                "one directional move counted many times, not many "
                "independent observations.",
            ]
        if inv.gaps:
            lines += [
                "",
                f"⚠️ {inv.downtime_hours:.1f}h with nothing recorded, across "
                f"{len(inv.gaps)} gap(s).",
            ]
        remaining = inv.days_remaining_observed()
        if remaining:
            lines += ["", f"About {remaining:.0f} more day(s) at the rate so far."]

        await self.tg.send_message(user.tg_id, "\n".join(lines))

    def _is_admin(self, user: User) -> bool:
        return user.tg_id in self.settings.admin_ids

    async def _cmd_genkeys(self, user: User, args: list[str], message: dict) -> None:
        if not self._is_admin(user):
            return
        tier = args[0].lower() if args else ""
        if tier not in TIERS:
            await self.tg.send_message(
                user.tg_id,
                f"Usage: <code>/genkeys {'|'.join(TIERS)} [count]</code>",
            )
            return
        count = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
        count = max(1, min(50, count))
        keys = await self.storage.mint_keys(tier, count)
        body = "\n".join(f"<code>{k}</code>" for k in keys)
        await self.tg.send_message(user.tg_id, f"<b>{tier} × {count}</b>\n{body}")

    async def _cmd_keystats(self, user: User, args: list[str], message: dict) -> None:
        if not self._is_admin(user):
            return
        stats = await self.storage.key_stats()
        if not stats:
            await self.tg.send_message(user.tg_id, "No keys minted yet.")
            return
        lines = ["<b>Access keys</b>", ""]
        for tier, (total, used) in sorted(stats.items()):
            lines.append(f"{tier}: {used}/{total} redeemed")
        await self.tg.send_message(user.tg_id, "\n".join(lines))

    async def _cmd_grant(self, user: User, args: list[str], message: dict) -> None:
        """Give a user access directly, without minting a key first."""
        if not self._is_admin(user):
            return
        if len(args) < 2 or not args[0].isdigit() or args[1].lower() not in TIERS:
            await self.tg.send_message(
                user.tg_id, f"Usage: <code>/grant TG_ID {'|'.join(TIERS)}</code>"
            )
            return
        target_id, tier = int(args[0]), args[1].lower()
        target = await self.storage.get_user(target_id)
        if target is None:
            await self.tg.send_message(
                user.tg_id, "That user has not started the bot yet."
            )
            return
        key = (await self.storage.mint_keys(tier, 1))[0]
        ok, detail = await self.storage.redeem_key(target_id, key)
        await self.tg.send_message(user.tg_id, f"{'✅' if ok else '❌'} {detail}")
        if ok:
            await self.notify(target_id, f"✅ {detail}")

    # ---------------- guided input ----------------

    async def _on_pending_input(
        self, user: User, pending: PendingInput, text: str, message: dict
    ) -> None:
        if text.lower() in {"/cancel", "cancel"}:
            self._pending.pop(user.tg_id, None)
            await self.tg.send_message(user.tg_id, "Cancelled.")
            return

        if pending.state == "await_key_id":
            self._pending[user.tg_id] = PendingInput(
                "await_private_key", {"key_id": text.strip()}
            )
            await self.tg.send_message(
                user.tg_id,
                "Got it. Now paste the <b>whole private key file</b>, including the "
                "<code>-----BEGIN ... PRIVATE KEY-----</code> and "
                "<code>-----END ...-----</code> lines.\n\n"
                "I'll delete your message as soon as I've read it.",
            )
            return

        if pending.state == "await_private_key":
            self._pending.pop(user.tg_id, None)
            # Remove the key from the chat history immediately, whether or not
            # it turns out to be valid.
            if message.get("message_id"):
                await self.tg.delete_message(user.tg_id, message["message_id"])
            key_id = pending.data.get("key_id", "")
            await self._store_credentials(user, key_id, text)

    async def _store_credentials(self, user: User, key_id: str, pem: str) -> None:
        try:
            signer = Signer.from_pem(key_id, pem)
        except InvalidPrivateKey as exc:
            await self.tg.send_message(
                user.tg_id,
                f"❌ That doesn't look like an RSA private key ({html.escape(str(exc))}). "
                "Run /connect to try again.",
            )
            return

        # Verify against Kalshi before saving, so a bad key is caught here and
        # not in the middle of a trade.
        client = KalshiClient(self.settings.rest_base, signer=signer)
        try:
            balance = await client.get_balance()
        except KalshiError as exc:
            hint = (
                "Kalshi rejected the signature — check the key ID matches this key."
                if exc.is_auth_error
                else html.escape(str(exc))
            )
            await self.tg.send_message(user.tg_id, f"❌ {hint}\n\nRun /connect to retry.")
            return
        finally:
            await client.aclose()

        await self.storage.set_credentials(user.tg_id, key_id, pem)
        await self.tg.send_message(
            user.tg_id,
            f"✅ Connected. Kalshi balance: <b>${format_dollars(balance)}</b>\n\n"
            "Your private key is encrypted at rest. Switch to <b>Live</b> on the "
            "dashboard when you're ready — or stay in paper mode as long as you like.",
        )
        await self._send_dashboard(await self._reload(user))

    # ---------------- callbacks ----------------

    async def _on_callback(self, query: dict) -> None:
        data = query.get("data") or ""
        callback_id = query["id"]
        from_user = query.get("from", {})
        tg_id = from_user.get("id")
        message = query.get("message") or {}
        message_id = message.get("message_id")
        if tg_id is None or message_id is None:
            return

        user = await self.storage.upsert_user(tg_id, from_user.get("username"))
        namespace, _, rest = data.partition(":")

        if namespace == "noop":
            await self.tg.answer_callback_query(callback_id)
            return

        toast: str | None = None
        if namespace == "set":
            field_name, _, raw = rest.partition(":")
            toast = await self._apply_setting(user, field_name, raw)
            user = await self._reload(user)
        elif namespace == "coin":
            toast = await self._toggle_coin(user, rest)
            user = await self._reload(user)
        elif namespace == "run":
            toast = await self._toggle_running(user, rest == "start")
            user = await self._reload(user)
        elif namespace == "buy":
            await self.tg.answer_callback_query(callback_id)
            await self._start_purchase(user, rest)
            return

        view = "main"
        if namespace == "nav":
            view = rest
        elif namespace == "coin":
            view = "coins"
        elif namespace == "set":
            # Stay on the sub-menu the user is tapping in, rather than bouncing
            # them back to the dashboard after every adjustment.
            view = SETTING_VIEWS.get(rest.partition(":")[0], "main")

        await self.tg.answer_callback_query(callback_id, toast)
        await self._render(user, view, message_id=message_id)

    async def _apply_setting(self, user: User, field_name: str, raw: str) -> str | None:
        if field_name not in DEFAULT_SETTINGS:
            return None

        if field_name == "paper":
            going_live = raw == "0"
            if going_live and not user.has_credentials:
                return "Connect a Kalshi key first — /connect"
            await self.storage.update_settings(user.tg_id, {"paper": not going_live})
            return "Live mode" if going_live else "Paper mode"

        if field_name == "mode":
            await self.storage.update_settings(user.tg_id, {"mode": raw})
            return "Auto-execute" if raw == "auto" else "Manual signals"

        if field_name == "strategy":
            if raw not in REGISTRY:
                return None
            await self.storage.update_settings(user.tg_id, {"strategy": raw})
            return f"Strategy: {raw}"

        if field_name == "exit_mode":
            await self.storage.update_settings(user.tg_id, {"exit_mode": raw})
            return "Exit: target price" if raw == "target" else "Exit: entry + cents"

        if field_name == "min_confidence":
            await self.storage.update_settings(
                user.tg_id, {"min_confidence": int(raw) / 100}
            )
            return f"Min confidence {raw}%"

        if not raw.lstrip("-").isdigit():
            return None
        value = int(raw)
        if field_name == "max_entry_price":
            # Keep the band coherent rather than silently accepting min > max.
            floor = int(user.get("min_entry_price"))
            if value <= floor:
                return f"Must stay above your {floor}c floor"
        await self.storage.update_settings(user.tg_id, {field_name: value})
        return "Saved"

    async def _toggle_coin(self, user: User, coin: str) -> str:
        coin = coin.upper()
        if coin not in self.settings.series:
            return "Not available"
        selected = [c.upper() for c in (user.get("coins") or [])]
        if coin in selected:
            selected.remove(coin)
        else:
            selected.append(coin)
        await self.storage.update_settings(
            user.tg_id, {"coins": sorted(set(selected))}
        )
        return f"{coin} {'off' if coin not in selected else 'on'}"

    async def _toggle_running(self, user: User, start: bool) -> str:
        if not start:
            await self.storage.set_enabled(user.tg_id, False)
            return "Stopped"
        if not self._has_access(user):
            return "No active access — use /buy or /redeem"
        if not (user.get("coins") or []):
            return "Pick at least one coin first"
        if not user.get("paper") and not user.has_credentials:
            return "Connect a Kalshi key first — /connect"
        await self.storage.set_enabled(user.tg_id, True)
        return "Running — paper" if user.get("paper") else "Running — LIVE"

    # ---------------- rendering ----------------

    async def _render(self, user: User, view: str, message_id: int | None = None) -> None:
        text, markup = await self._view(user, view)
        if message_id is None:
            await self.tg.send_message(user.tg_id, text, reply_markup=markup)
        else:
            await self.tg.edit_message_text(
                user.tg_id, message_id, text, reply_markup=markup
            )

    async def _view(self, user: User, view: str) -> tuple[str, dict]:
        if view == "coins":
            return (
                "<b>Coins</b>\n\nTap to select which 15-minute markets the bot "
                "watches for you.",
                ui.coins_keyboard(user, self.settings.coins),
            )
        if view == "strategy":
            return ui.strategy_text(user), ui.strategy_keyboard(user)
        if view == "size":
            return (
                "<b>Size & entry</b>\n\nHow many contracts to buy per signal, the "
                "confidence a signal needs before it counts, and the most you're "
                "willing to pay for a contract.",
                ui.size_keyboard(user),
            )
        if view == "exit":
            return ui.exit_text(user), ui.exit_keyboard(user)
        if view == "risk":
            snap = await self.risk.snapshot(user)
            return (
                ui.risk_text(
                    user,
                    snap.realised_today_dc,
                    snap.open_exposure_dc,
                    snap.open_positions,
                ),
                ui.risk_keyboard(user),
            )
        if view == "positions":
            open_trades = await self.storage.open_trades(user.tg_id)
            recent = await self.storage.trades_since(user.tg_id, start_of_utc_day())
            return ui.positions_text(open_trades, recent), ui.positions_keyboard()

        note = ""
        if not self._has_access(user):
            note = "⚠️ No active access. "
            note += (
                "Buy with /buy, or redeem a key with <code>/redeem KEY</code>."
                if self.payments.enabled
                else "Redeem a key with <code>/redeem KEY</code>."
            )
        return (
            ui.dashboard_text(user, self.settings, note),
            ui.dashboard_keyboard(user),
        )

    async def _send_dashboard(self, user: User) -> None:
        await self._render(user, "main")

    async def _reload(self, user: User) -> User:
        refreshed = await self.storage.get_user(user.tg_id)
        return refreshed or user

    def _has_access(self, user: User) -> bool:
        return user.has_access or not self.settings.require_access_key

    async def _deny(self, user: User) -> None:
        await self.tg.send_message(
            user.tg_id,
            "🔒 You need active access. "
            + (
                "Buy one with /buy, or redeem a key with <code>/redeem YOUR-KEY</code>."
                if self.payments.enabled
                else "Redeem a key with <code>/redeem YOUR-KEY</code>."
            ),
        )
