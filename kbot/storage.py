"""SQLite persistence.

Everything the bot needs to survive a restart lives here: users, their trading
settings, encrypted Kalshi credentials, access keys, and the trade ledger.

All public methods are async and run the blocking sqlite3 work in a worker
thread, so a slow disk can never stall the Telegram poller or the trade engine.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id           INTEGER PRIMARY KEY,
    username        TEXT,
    created_at      REAL NOT NULL,
    access_until    REAL NOT NULL DEFAULT 0,
    lifetime        INTEGER NOT NULL DEFAULT 0,
    settings_json   TEXT NOT NULL DEFAULT '{}',
    kalshi_key_id   TEXT,
    kalshi_secret   BLOB,
    enabled         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS access_keys (
    key         TEXT PRIMARY KEY,
    tier        TEXT NOT NULL,
    duration_s  INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    redeemed_by INTEGER,
    redeemed_at REAL
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id           INTEGER NOT NULL,
    ticker          TEXT NOT NULL,
    coin            TEXT NOT NULL,
    side            TEXT NOT NULL,
    count           INTEGER NOT NULL,
    entry_price_dc  INTEGER NOT NULL,
    exit_price_dc   INTEGER,
    target_price_dc INTEGER,
    status          TEXT NOT NULL,
    paper           INTEGER NOT NULL,
    opened_at       REAL NOT NULL,
    closed_at       REAL,
    entry_fee_dc    INTEGER NOT NULL DEFAULT 0,
    exit_fee_dc     INTEGER,
    pnl_dc          INTEGER,
    gross_pnl_dc    INTEGER,
    entry_order_id  TEXT,
    exit_order_id   TEXT,
    reason          TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_user_time ON trades(tg_id, opened_at);
CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(tg_id, status);

CREATE TABLE IF NOT EXISTS invoices (
    order_id     TEXT PRIMARY KEY,
    tg_id        INTEGER NOT NULL,
    tier         TEXT NOT NULL,
    amount_usd   REAL NOT NULL,
    provider     TEXT NOT NULL,
    provider_id  TEXT,
    currency     TEXT,
    pay_address  TEXT,
    pay_amount   TEXT,
    checkout_url TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    key_issued   TEXT,
    created_at   REAL NOT NULL,
    settled_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_invoices_user ON invoices(tg_id, created_at);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,
    coin        TEXT NOT NULL,
    side        TEXT NOT NULL,
    confidence  REAL NOT NULL,
    price_dc    INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    detail      TEXT
);
"""

# Defaults for a brand new user. Deliberately conservative: paper mode on,
# manual signals only, one contract, tight caps.
DEFAULT_SETTINGS: dict[str, Any] = {
    "paper": True,
    "mode": "manual",  # "manual" | "auto"
    "coins": ["BTC", "ETH"],
    "strategy": "drift",
    "contracts": 1,
    "max_entry_price": 65,  # never pay more than this many cents
    "min_entry_price": 25,
    "min_confidence": 0.60,
    "exit_mode": "profit",  # "profit" | "target"
    "profit_cents": 8,  # exit at entry + N cents
    "target_price": 90,  # or exit at this absolute price
    "daily_loss_limit_cents": 2000,
    "max_exposure_cents": 5000,
    "balance_floor_cents": 1000,
    "max_trades_per_window": 1,
    "max_open_positions": 3,
    "share_results": False,
}


@dataclass
class User:
    tg_id: int
    username: str | None
    created_at: float
    access_until: float
    lifetime: bool
    settings: dict[str, Any] = field(default_factory=dict)
    kalshi_key_id: str | None = None
    kalshi_secret: str | None = None
    enabled: bool = False

    @property
    def has_access(self) -> bool:
        return self.lifetime or self.access_until > time.time()

    @property
    def has_credentials(self) -> bool:
        return bool(self.kalshi_key_id and self.kalshi_secret)

    def get(self, name: str) -> Any:
        return self.settings.get(name, DEFAULT_SETTINGS.get(name))


@dataclass
class Invoice:
    order_id: str
    tg_id: int
    tier: str
    amount_usd: float
    provider: str
    provider_id: str | None
    currency: str | None
    pay_address: str | None
    pay_amount: str | None
    checkout_url: str | None
    status: str  # "pending" | "paid" | "failed"
    key_issued: str | None
    created_at: float
    settled_at: float | None


@dataclass
class Trade:
    id: int
    tg_id: int
    ticker: str
    coin: str
    side: str
    count: int
    entry_price_dc: int
    exit_price_dc: int | None
    target_price_dc: int | None
    status: str  # "open" | "closed" | "expired"
    paper: bool
    opened_at: float
    closed_at: float | None
    entry_fee_dc: int
    exit_fee_dc: int | None
    pnl_dc: int | None  # net of fees
    gross_pnl_dc: int | None
    entry_order_id: str | None
    exit_order_id: str | None
    reason: str | None


TIERS: dict[str, int] = {
    "daily": 24 * 3600,
    "weekly": 7 * 24 * 3600,
    "monthly": 30 * 24 * 3600,
    "lifetime": 0,
}


class Storage:
    def __init__(self, path: Path, master_key: str) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(master_key.encode())
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    async def _run(self, fn, *args):
        # One writer at a time; sqlite handles the rest.
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    def close(self) -> None:
        self._conn.close()

    # ---------------- credentials ----------------

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, blob: bytes | None) -> str | None:
        if not blob:
            return None
        try:
            return self._fernet.decrypt(blob).decode()
        except InvalidToken:
            # Wrong MASTER_KEY, or a tampered row. Treat as "no credentials"
            # rather than crashing the engine for every other user.
            return None

    # ---------------- users ----------------

    def _row_to_user(self, row: sqlite3.Row) -> User:
        settings = dict(DEFAULT_SETTINGS)
        settings.update(json.loads(row["settings_json"] or "{}"))
        return User(
            tg_id=row["tg_id"],
            username=row["username"],
            created_at=row["created_at"],
            access_until=row["access_until"],
            lifetime=bool(row["lifetime"]),
            settings=settings,
            kalshi_key_id=row["kalshi_key_id"],
            kalshi_secret=self.decrypt(row["kalshi_secret"]),
            enabled=bool(row["enabled"]),
        )

    async def upsert_user(self, tg_id: int, username: str | None) -> User:
        def work() -> User:
            cur = self._conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
            row = cur.fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO users (tg_id, username, created_at, settings_json)"
                    " VALUES (?, ?, ?, '{}')",
                    (tg_id, username, time.time()),
                )
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM users WHERE tg_id = ?", (tg_id,)
                ).fetchone()
            elif username and row["username"] != username:
                self._conn.execute(
                    "UPDATE users SET username = ? WHERE tg_id = ?", (username, tg_id)
                )
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM users WHERE tg_id = ?", (tg_id,)
                ).fetchone()
            return self._row_to_user(row)

        return await self._run(work)

    async def get_user(self, tg_id: int) -> User | None:
        def work() -> User | None:
            row = self._conn.execute(
                "SELECT * FROM users WHERE tg_id = ?", (tg_id,)
            ).fetchone()
            return self._row_to_user(row) if row else None

        return await self._run(work)

    async def active_users(self, require_access: bool = True) -> list[User]:
        """Users who have switched trading on (and, by default, hold access)."""

        def work() -> list[User]:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE enabled = 1"
            ).fetchall()
            users = [self._row_to_user(r) for r in rows]
            return [u for u in users if u.has_access or not require_access]

        return await self._run(work)

    async def update_settings(self, tg_id: int, changes: dict[str, Any]) -> User:
        def work() -> User:
            row = self._conn.execute(
                "SELECT settings_json FROM users WHERE tg_id = ?", (tg_id,)
            ).fetchone()
            if row is None:
                raise KeyError(tg_id)
            settings = json.loads(row["settings_json"] or "{}")
            settings.update(changes)
            self._conn.execute(
                "UPDATE users SET settings_json = ? WHERE tg_id = ?",
                (json.dumps(settings), tg_id),
            )
            self._conn.commit()
            return self._row_to_user(
                self._conn.execute(
                    "SELECT * FROM users WHERE tg_id = ?", (tg_id,)
                ).fetchone()
            )

        return await self._run(work)

    async def set_enabled(self, tg_id: int, enabled: bool) -> None:
        await self._run(
            lambda: (
                self._conn.execute(
                    "UPDATE users SET enabled = ? WHERE tg_id = ?",
                    (1 if enabled else 0, tg_id),
                ),
                self._conn.commit(),
            )
        )

    async def set_credentials(self, tg_id: int, key_id: str, private_key: str) -> None:
        blob = self.encrypt(private_key)
        await self._run(
            lambda: (
                self._conn.execute(
                    "UPDATE users SET kalshi_key_id = ?, kalshi_secret = ?"
                    " WHERE tg_id = ?",
                    (key_id, blob, tg_id),
                ),
                self._conn.commit(),
            )
        )

    async def clear_credentials(self, tg_id: int) -> None:
        await self._run(
            lambda: (
                self._conn.execute(
                    "UPDATE users SET kalshi_key_id = NULL, kalshi_secret = NULL,"
                    " enabled = 0 WHERE tg_id = ?",
                    (tg_id,),
                ),
                self._conn.commit(),
            )
        )

    # ---------------- access keys ----------------

    async def mint_keys(self, tier: str, count: int) -> list[str]:
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}")
        duration = TIERS[tier]
        keys = [f"{tier[:1].upper()}-{secrets.token_hex(8).upper()}" for _ in range(count)]

        def work() -> list[str]:
            now = time.time()
            self._conn.executemany(
                "INSERT INTO access_keys (key, tier, duration_s, created_at)"
                " VALUES (?, ?, ?, ?)",
                [(k, tier, duration, now) for k in keys],
            )
            self._conn.commit()
            return keys

        return await self._run(work)

    async def redeem_key(self, tg_id: int, key: str) -> tuple[bool, str]:
        def work() -> tuple[bool, str]:
            row = self._conn.execute(
                "SELECT * FROM access_keys WHERE key = ?", (key.strip().upper(),)
            ).fetchone()
            if row is None:
                return False, "That key isn't valid."
            if row["redeemed_by"] is not None:
                return False, "That key has already been redeemed."

            now = time.time()
            user = self._conn.execute(
                "SELECT access_until, lifetime FROM users WHERE tg_id = ?", (tg_id,)
            ).fetchone()
            if user is None:
                return False, "Send /start first."

            if row["tier"] == "lifetime":
                self._conn.execute(
                    "UPDATE users SET lifetime = 1 WHERE tg_id = ?", (tg_id,)
                )
                label = "Lifetime access unlocked."
            else:
                # Stack onto any remaining time rather than overwriting it.
                base = max(now, user["access_until"])
                self._conn.execute(
                    "UPDATE users SET access_until = ? WHERE tg_id = ?",
                    (base + row["duration_s"], tg_id),
                )
                days = row["duration_s"] / 86400
                label = f"{row['tier'].title()} access added ({days:.0f} days)."

            self._conn.execute(
                "UPDATE access_keys SET redeemed_by = ?, redeemed_at = ? WHERE key = ?",
                (tg_id, now, row["key"]),
            )
            self._conn.commit()
            return True, label

        return await self._run(work)

    async def key_stats(self) -> dict[str, tuple[int, int]]:
        def work() -> dict[str, tuple[int, int]]:
            rows = self._conn.execute(
                "SELECT tier, COUNT(*) AS total,"
                " SUM(CASE WHEN redeemed_by IS NULL THEN 0 ELSE 1 END) AS used"
                " FROM access_keys GROUP BY tier"
            ).fetchall()
            return {r["tier"]: (r["total"], r["used"] or 0) for r in rows}

        return await self._run(work)

    # ---------------- trades ----------------

    async def record_entry(
        self,
        *,
        tg_id: int,
        ticker: str,
        coin: str,
        side: str,
        count: int,
        entry_price_dc: int,
        target_price_dc: int | None,
        entry_fee_dc: int,
        paper: bool,
        entry_order_id: str | None,
        reason: str | None,
    ) -> int:
        def work() -> int:
            cur = self._conn.execute(
                "INSERT INTO trades (tg_id, ticker, coin, side, count, entry_price_dc,"
                " target_price_dc, entry_fee_dc, status, paper, opened_at,"
                " entry_order_id, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
                (
                    tg_id,
                    ticker,
                    coin,
                    side,
                    count,
                    entry_price_dc,
                    target_price_dc,
                    entry_fee_dc,
                    1 if paper else 0,
                    time.time(),
                    entry_order_id,
                    reason,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

        return await self._run(work)

    async def close_trade(
        self,
        trade_id: int,
        *,
        exit_price_dc: int,
        exit_fee_dc: int = 0,
        status: str = "closed",
        exit_order_id: str | None = None,
    ) -> Trade | None:
        def work() -> Trade | None:
            row = self._conn.execute(
                "SELECT * FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()
            if row is None or row["status"] != "open":
                return None
            gross = (exit_price_dc - row["entry_price_dc"]) * row["count"]
            # P/L is booked net of both fees. Reporting gross would flatter
            # every result, and on a 15-minute market the round trip is a
            # meaningful share of the move.
            net = gross - (row["entry_fee_dc"] or 0) - int(exit_fee_dc)
            self._conn.execute(
                "UPDATE trades SET exit_price_dc = ?, exit_fee_dc = ?, status = ?,"
                " closed_at = ?, pnl_dc = ?, gross_pnl_dc = ?,"
                " exit_order_id = COALESCE(?, exit_order_id) WHERE id = ?",
                (
                    exit_price_dc,
                    int(exit_fee_dc),
                    status,
                    time.time(),
                    net,
                    gross,
                    exit_order_id,
                    trade_id,
                ),
            )
            self._conn.commit()
            return _row_to_trade(
                self._conn.execute(
                    "SELECT * FROM trades WHERE id = ?", (trade_id,)
                ).fetchone()
            )

        return await self._run(work)

    async def set_exit_order(self, trade_id: int, order_id: str) -> None:
        await self._run(
            lambda: (
                self._conn.execute(
                    "UPDATE trades SET exit_order_id = ? WHERE id = ?",
                    (order_id, trade_id),
                ),
                self._conn.commit(),
            )
        )

    async def open_trades(self, tg_id: int | None = None) -> list[Trade]:
        def work() -> list[Trade]:
            if tg_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM trades WHERE status = 'open' AND tg_id = ?"
                    " ORDER BY opened_at",
                    (tg_id,),
                ).fetchall()
            return [_row_to_trade(r) for r in rows]

        return await self._run(work)

    async def trades_since(self, tg_id: int, since: float) -> list[Trade]:
        def work() -> list[Trade]:
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE tg_id = ? AND opened_at >= ?"
                " ORDER BY opened_at DESC",
                (tg_id, since),
            ).fetchall()
            return [_row_to_trade(r) for r in rows]

        return await self._run(work)

    async def trades_on_ticker(self, tg_id: int, ticker: str) -> list[Trade]:
        def work() -> list[Trade]:
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE tg_id = ? AND ticker = ?", (tg_id, ticker)
            ).fetchall()
            return [_row_to_trade(r) for r in rows]

        return await self._run(work)

    # ---------------- invoices ----------------

    async def create_invoice(
        self,
        *,
        order_id: str,
        tg_id: int,
        tier: str,
        amount_usd: float,
        provider: str,
        provider_id: str | None,
        currency: str | None,
        pay_address: str | None,
        pay_amount: str | None,
        checkout_url: str | None,
    ) -> Invoice:
        def work() -> Invoice:
            self._conn.execute(
                "INSERT INTO invoices (order_id, tg_id, tier, amount_usd, provider,"
                " provider_id, currency, pay_address, pay_amount, checkout_url,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    tg_id,
                    tier,
                    amount_usd,
                    provider,
                    provider_id,
                    currency,
                    pay_address,
                    pay_amount,
                    checkout_url,
                    time.time(),
                ),
            )
            self._conn.commit()
            return _row_to_invoice(
                self._conn.execute(
                    "SELECT * FROM invoices WHERE order_id = ?", (order_id,)
                ).fetchone()
            )

        return await self._run(work)

    async def get_invoice(self, order_id: str) -> Invoice | None:
        def work() -> Invoice | None:
            row = self._conn.execute(
                "SELECT * FROM invoices WHERE order_id = ?", (order_id,)
            ).fetchone()
            return _row_to_invoice(row) if row else None

        return await self._run(work)

    async def user_invoices(self, tg_id: int, limit: int = 10) -> list[Invoice]:
        def work() -> list[Invoice]:
            rows = self._conn.execute(
                "SELECT * FROM invoices WHERE tg_id = ? ORDER BY created_at DESC"
                " LIMIT ?",
                (tg_id, limit),
            ).fetchall()
            return [_row_to_invoice(r) for r in rows]

        return await self._run(work)

    async def pending_invoices(self, older_than: float = 0.0) -> list[Invoice]:
        def work() -> list[Invoice]:
            rows = self._conn.execute(
                "SELECT * FROM invoices WHERE status = 'pending' AND created_at <= ?"
                " ORDER BY created_at",
                (time.time() - older_than,),
            ).fetchall()
            return [_row_to_invoice(r) for r in rows]

        return await self._run(work)

    async def settle_invoice(self, order_id: str) -> tuple[Invoice | None, str | None]:
        """Mark an invoice paid and mint its key, exactly once.

        Providers retry webhooks, so this is the single place that guards
        against issuing two keys for one payment: the status check and the key
        write happen in the same transaction.
        """

        def work() -> tuple[Invoice | None, str | None]:
            row = self._conn.execute(
                "SELECT * FROM invoices WHERE order_id = ?", (order_id,)
            ).fetchone()
            if row is None:
                return None, None
            if row["status"] == "paid":
                # Already settled — hand back the key we issued the first time.
                return _row_to_invoice(row), row["key_issued"]

            tier = row["tier"]
            duration = TIERS.get(tier)
            if duration is None:
                return _row_to_invoice(row), None

            key = f"{tier[:1].upper()}-{secrets.token_hex(8).upper()}"
            now = time.time()
            self._conn.execute(
                "INSERT INTO access_keys (key, tier, duration_s, created_at)"
                " VALUES (?, ?, ?, ?)",
                (key, tier, duration, now),
            )
            self._conn.execute(
                "UPDATE invoices SET status = 'paid', key_issued = ?, settled_at = ?"
                " WHERE order_id = ?",
                (key, now, order_id),
            )
            self._conn.commit()
            return (
                _row_to_invoice(
                    self._conn.execute(
                        "SELECT * FROM invoices WHERE order_id = ?", (order_id,)
                    ).fetchone()
                ),
                key,
            )

        return await self._run(work)

    async def fail_invoice(self, order_id: str) -> None:
        await self._run(
            lambda: (
                self._conn.execute(
                    "UPDATE invoices SET status = 'failed' WHERE order_id = ?"
                    " AND status = 'pending'",
                    (order_id,),
                ),
                self._conn.commit(),
            )
        )

    async def revenue(self) -> dict[str, tuple[int, float]]:
        def work() -> dict[str, tuple[int, float]]:
            rows = self._conn.execute(
                "SELECT tier, COUNT(*) AS n, SUM(amount_usd) AS total FROM invoices"
                " WHERE status = 'paid' GROUP BY tier"
            ).fetchall()
            return {r["tier"]: (r["n"], r["total"] or 0.0) for r in rows}

        return await self._run(work)

    async def all_closed_trades(
        self, paper: bool | None = None, since: float = 0.0
    ) -> list[Trade]:
        """Every resolved trade across all users — the results-channel tally."""

        def work() -> list[Trade]:
            sql = "SELECT * FROM trades WHERE status != 'open' AND opened_at >= ?"
            args: list = [since]
            if paper is not None:
                sql += " AND paper = ?"
                args.append(1 if paper else 0)
            sql += " ORDER BY closed_at"
            return [_row_to_trade(r) for r in self._conn.execute(sql, args).fetchall()]

        return await self._run(work)

    async def strategy_breakdown(
        self, tg_id: int, since: float
    ) -> list[tuple[str, str, int, int, int]]:
        """(coin, side, trades, wins, net_dc) per coin for one user."""

        def work():
            rows = self._conn.execute(
                "SELECT coin, side, COUNT(*) n,"
                " SUM(CASE WHEN pnl_dc > 0 THEN 1 ELSE 0 END) wins,"
                " SUM(pnl_dc) net FROM trades"
                " WHERE tg_id = ? AND status != 'open' AND opened_at >= ?"
                " GROUP BY coin, side ORDER BY net DESC",
                (tg_id, since),
            ).fetchall()
            return [
                (r["coin"], r["side"], r["n"], r["wins"] or 0, r["net"] or 0)
                for r in rows
            ]

        return await self._run(work)

    async def hourly_breakdown(self, tg_id: int, since: float) -> dict[int, tuple[int, int]]:
        """(trades, net_dc) per UTC hour, to expose time-of-day effects."""

        def work():
            rows = self._conn.execute(
                "SELECT CAST(strftime('%H', opened_at, 'unixepoch') AS INTEGER) h,"
                " COUNT(*) n, SUM(pnl_dc) net FROM trades"
                " WHERE tg_id = ? AND status != 'open' AND opened_at >= ?"
                " GROUP BY h ORDER BY h",
                (tg_id, since),
            ).fetchall()
            return {r["h"]: (r["n"], r["net"] or 0) for r in rows}

        return await self._run(work)

    async def outcome_counts(self, tg_id: int, since: float) -> dict[str, int]:
        def work():
            rows = self._conn.execute(
                "SELECT status, COUNT(*) n FROM trades"
                " WHERE tg_id = ? AND status != 'open' AND opened_at >= ?"
                " GROUP BY status",
                (tg_id, since),
            ).fetchall()
            return {r["status"]: r["n"] for r in rows}

        return await self._run(work)

    async def record_signal(
        self,
        ticker: str,
        coin: str,
        side: str,
        confidence: float,
        price_dc: int,
        detail: str,
    ) -> None:
        await self._run(
            lambda: (
                self._conn.execute(
                    "INSERT INTO signals (ticker, coin, side, confidence, price_dc,"
                    " created_at, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ticker, coin, side, confidence, price_dc, time.time(), detail),
                ),
                self._conn.commit(),
            )
        )


PRICES_USD: dict[str, float] = {
    "daily": 25.0,
    "weekly": 50.0,
    "monthly": 100.0,
    "lifetime": 1000.0,
}


def _row_to_invoice(row: sqlite3.Row) -> Invoice:
    return Invoice(
        order_id=row["order_id"],
        tg_id=row["tg_id"],
        tier=row["tier"],
        amount_usd=row["amount_usd"],
        provider=row["provider"],
        provider_id=row["provider_id"],
        currency=row["currency"],
        pay_address=row["pay_address"],
        pay_amount=row["pay_amount"],
        checkout_url=row["checkout_url"],
        status=row["status"],
        key_issued=row["key_issued"],
        created_at=row["created_at"],
        settled_at=row["settled_at"],
    )


def _row_to_trade(row: sqlite3.Row) -> Trade:
    return Trade(
        id=row["id"],
        tg_id=row["tg_id"],
        ticker=row["ticker"],
        coin=row["coin"],
        side=row["side"],
        count=row["count"],
        entry_price_dc=row["entry_price_dc"],
        exit_price_dc=row["exit_price_dc"],
        target_price_dc=row["target_price_dc"],
        status=row["status"],
        paper=bool(row["paper"]),
        opened_at=row["opened_at"],
        closed_at=row["closed_at"],
        entry_fee_dc=row["entry_fee_dc"] or 0,
        exit_fee_dc=row["exit_fee_dc"],
        pnl_dc=row["pnl_dc"],
        gross_pnl_dc=row["gross_pnl_dc"],
        entry_order_id=row["entry_order_id"],
        exit_order_id=row["exit_order_id"],
        reason=row["reason"],
    )
