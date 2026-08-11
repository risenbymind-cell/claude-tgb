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
from ..kalshi.rest import KalshiClient, KalshiError
from ..storage import DEFAULT_SETTINGS, TIERS, Storage, User
from ..strategy import REGISTRY
from . import ui
from .api import TelegramClient, TelegramError

log = logging.getLogger(__name__)

COMMANDS = [
    ("start", "Open the dashboard"),
    ("dashboard", "Open the dashboard"),
    ("redeem", "Redeem an access key"),
    ("connect", "Connect your Kalshi API key"),
    ("disconnect", "Remove your Kalshi API key"),
    ("positions", "Open positions and recent trades"),
    ("pnl", "Profit and loss summary"),
    ("status", "Bot and market status"),
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
Paper mode runs the identical code path against real live prices and simulates the fill. Nothing reaches Kalshi. Switch to live only after you've watched it trade.

<b>Exits</b>
A sell order goes in as soon as an entry fills — either a fixed number of cents above entry or an absolute target price. If neither fills, the window settles at 100c or 0c and the bot books the result.

<b>Risk caps</b>
Daily loss limit, max open exposure, a balance floor the bot won't spend below, and per-window trade limits. All of them are checked against your trade ledger, so restarting the bot does not reset them.

<b>Connecting Kalshi</b>
Create an API key at kalshi.com under Account → API Keys. You'll get a key ID and an RSA private key file. Send both with /connect. The private key is encrypted before it is stored, and your message is deleted from the chat straight after.

<b>Commands</b>
/dashboard · /positions · /pnl · /status · /stop · /disconnect

<i>Signals are not advice. Losing windows happen. Only trade money you can afford to lose.</i>"""


# Which dashboard view each setting belongs to, so tapping a button re-renders
# the menu the user is standing in.
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
        self.risk = RiskManager(storage)
        self._pending: dict[int, PendingInput] = {}

    # ---------------- lifecycle ----------------

    async def run(self) -> None:
        me = await self.tg.get_me()
        log.info("Connected to Telegram as @%s", me.get("username"))
        await self.tg.set_my_commands(COMMANDS)
        await self.engine.start()
        try:
            async for update in self.tg.poll():
                # One slow handler must not stall the update stream.
                asyncio.create_task(self._safe_handle(update))
        finally:
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
            "redeem": self._cmd_redeem,
            "connect": self._cmd_connect,
            "disconnect": self._cmd_disconnect,
            "positions": self._cmd_positions,
            "pnl": self._cmd_pnl,
            "status": self._cmd_status,
            "stop": self._cmd_stop,
            "genkeys": self._cmd_genkeys,
            "keystats": self._cmd_keystats,
            "grant": self._cmd_grant,
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
        # A key can be passed as a deep link payload: /start KEY
        if args:
            await self._cmd_redeem(user, args, message)
            return
        await self._send_dashboard(await self._reload(user))

    async def _cmd_help(self, user: User, args: list[str], message: dict) -> None:
        await self.tg.send_message(user.tg_id, HELP)

    async def _cmd_dashboard(self, user: User, args: list[str], message: dict) -> None:
        await self._send_dashboard(user)

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
            mid = f"{book.mid:.1f}c" if book and book.mid is not None else "—"
            lines.append(
                f"{coin}: YES mid {mid} · closes in {secs // 60}m {secs % 60:02d}s"
            )
        await self.tg.send_message(user.tg_id, "\n".join(lines))

    async def _cmd_stop(self, user: User, args: list[str], message: dict) -> None:
        await self.storage.set_enabled(user.tg_id, False)
        await self.tg.send_message(user.tg_id, "⏹ Trading stopped.")
        await self._send_dashboard(await self._reload(user))

    # ---------------- admin ----------------

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
            f"✅ Connected. Kalshi balance: <b>${balance/100:.2f}</b>\n\n"
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
            return "No active access — redeem a key with /redeem"
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
                    snap.realised_today_cents,
                    snap.open_exposure_cents,
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
            note = "⚠️ No active access. Redeem a key with <code>/redeem KEY</code>."
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
            "🔒 You need an active access key. Redeem one with "
            "<code>/redeem YOUR-KEY</code>.",
        )
