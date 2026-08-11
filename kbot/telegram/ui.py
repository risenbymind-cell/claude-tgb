"""Dashboard rendering: inline keyboards and the text that goes with them.

Kept separate from the handlers so the wording and layout of the bot can change
without touching any control flow.
"""

from __future__ import annotations

import html
import time

from ..config import Settings
from ..storage import Trade, User
from ..strategy import REGISTRY

CB = "cb"  # callback-data namespace separator is ":"


def button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def url_button(text: str, url: str) -> dict:
    return {"text": text, "url": url}


def keyboard(*rows: list[dict]) -> dict:
    return {"inline_keyboard": [row for row in rows if row]}


def _on(flag: bool) -> str:
    return "🟢" if flag else "⚪️"


# ---------------- main dashboard ----------------


def dashboard_text(user: User, settings: Settings, engine_note: str = "") -> str:
    mode = "Auto-execute" if user.get("mode") == "auto" else "Manual signals"
    account = "Paper simulation" if user.get("paper") else "Live Kalshi account"
    coins = ", ".join(user.get("coins") or []) or "none selected"
    strategy = str(user.get("strategy"))
    strat_desc = REGISTRY[strategy].description if strategy in REGISTRY else ""

    if user.get("exit_mode") == "target":
        exit_line = f"sell at {user.get('target_price')}c"
    else:
        exit_line = f"sell at entry +{user.get('profit_cents')}c"

    running = "🟢 RUNNING" if user.enabled else "⚪️ STOPPED"
    access = access_line(user)

    lines = [
        f"<b>DirectionalBot</b> · {running}",
        "",
        f"<b>Mode</b> — {mode}",
        f"<b>Account</b> — {account}",
        f"<b>Coins</b> — {coins}",
        f"<b>Strategy</b> — {strategy} ({html.escape(strat_desc)})",
        f"<b>Size</b> — {user.get('contracts')} contract(s) per signal",
        f"<b>Entry band</b> — {user.get('min_entry_price')}c to "
        f"{user.get('max_entry_price')}c",
        f"<b>Exit</b> — {exit_line}",
        f"<b>Min confidence</b> — {int(float(user.get('min_confidence')) * 100)}%",
        "",
        f"<b>Access</b> — {access}",
    ]
    if not user.get("paper"):
        creds = "connected" if user.has_credentials else "⚠️ not connected"
        lines.append(f"<b>Kalshi key</b> — {creds}")
    if settings.demo:
        lines.append("<i>Bot is pointed at Kalshi's demo environment.</i>")
    if engine_note:
        lines += ["", engine_note]
    return "\n".join(lines)


def dashboard_keyboard(user: User) -> dict:
    running = user.enabled
    return keyboard(
        [
            button(
                "⏹ Stop trading" if running else "▶️ Start trading",
                "run:stop" if running else "run:start",
            )
        ],
        [
            button(
                f"{_on(user.get('mode') == 'auto')} Auto-execute", "set:mode:auto"
            ),
            button(
                f"{_on(user.get('mode') == 'manual')} Manual", "set:mode:manual"
            ),
        ],
        [
            button(f"{_on(bool(user.get('paper')))} Paper", "set:paper:1"),
            button(f"{_on(not user.get('paper'))} Live", "set:paper:0"),
        ],
        [
            button("◆ Coins", "nav:coins"),
            button("📐 Strategy", "nav:strategy"),
        ],
        [
            button("⚖️ Size & entry", "nav:size"),
            button("◪ Exit rules", "nav:exit"),
        ],
        [
            button("🛡 Risk caps", "nav:risk"),
            button("📊 Positions", "nav:positions"),
        ],
        [button("🔄 Refresh", "nav:main")],
    )


def access_line(user: User) -> str:
    if user.lifetime:
        return "lifetime ✅"
    if user.access_until > time.time():
        remaining = user.access_until - time.time()
        days, rem = divmod(int(remaining), 86400)
        hours = rem // 3600
        if days:
            return f"active — {days}d {hours}h left"
        minutes = (rem % 3600) // 60
        return f"active — {hours}h {minutes}m left"
    return "none — redeem a key with /redeem"


# ---------------- sub-menus ----------------


def coins_keyboard(user: User, available: list[str]) -> dict:
    selected = {c.upper() for c in (user.get("coins") or [])}
    rows: list[list[dict]] = []
    for i in range(0, len(available), 3):
        rows.append(
            [
                button(f"{_on(c in selected)} {c}", f"coin:{c}")
                for c in available[i : i + 3]
            ]
        )
    rows.append([button("‹ Back", "nav:main")])
    return keyboard(*rows)


def strategy_keyboard(user: User) -> dict:
    current = str(user.get("strategy"))
    rows = [
        [button(f"{_on(name == current)} {name}", f"set:strategy:{name}")]
        for name in REGISTRY
    ]
    rows.append([button("‹ Back", "nav:main")])
    return keyboard(*rows)


def strategy_text(user: User) -> str:
    lines = ["<b>Strategy</b>", ""]
    for name, strat in REGISTRY.items():
        marker = "▸" if name == str(user.get("strategy")) else " "
        lines.append(f"{marker} <b>{name}</b> — {html.escape(strat.description)}")
    lines += [
        "",
        "<i>Both presets are reference implementations. Validate them in paper "
        "mode against your own numbers before trading live.</i>",
    ]
    return "\n".join(lines)


SIZE_CHOICES = [1, 2, 5, 10, 25]
CONFIDENCE_CHOICES = [50, 60, 70, 80]
MAX_PRICE_CHOICES = [50, 60, 65, 75, 85]


def size_keyboard(user: User) -> dict:
    contracts = int(user.get("contracts"))
    confidence = int(float(user.get("min_confidence")) * 100)
    max_price = int(user.get("max_entry_price"))
    return keyboard(
        [button("Contracts per signal", "noop:_")],
        [
            button(f"{_on(n == contracts)} {n}", f"set:contracts:{n}")
            for n in SIZE_CHOICES
        ],
        [button("Minimum confidence", "noop:_")],
        [
            button(f"{_on(c == confidence)} {c}%", f"set:min_confidence:{c}")
            for c in CONFIDENCE_CHOICES
        ],
        [button("Never pay more than", "noop:_")],
        [
            button(f"{_on(p == max_price)} {p}c", f"set:max_entry_price:{p}")
            for p in MAX_PRICE_CHOICES
        ],
        [button("‹ Back", "nav:main")],
    )


PROFIT_CHOICES = [3, 5, 8, 12, 20]
TARGET_CHOICES = [70, 80, 90, 95, 99]


def exit_keyboard(user: User) -> dict:
    mode = str(user.get("exit_mode"))
    profit = int(user.get("profit_cents"))
    target = int(user.get("target_price"))
    return keyboard(
        [
            button(f"{_on(mode == 'profit')} Entry + N cents", "set:exit_mode:profit"),
            button(f"{_on(mode == 'target')} Target price", "set:exit_mode:target"),
        ],
        [button("Cents above entry", "noop:_")],
        [
            button(f"{_on(n == profit)} +{n}c", f"set:profit_cents:{n}")
            for n in PROFIT_CHOICES
        ],
        [button("Absolute target", "noop:_")],
        [
            button(f"{_on(n == target)} {n}c", f"set:target_price:{n}")
            for n in TARGET_CHOICES
        ],
        [button("‹ Back", "nav:main")],
    )


def exit_text(user: User) -> str:
    return (
        "<b>Exit rules</b>\n\n"
        "A sell order is placed the moment an entry fills, so a position is "
        "never left unmanaged.\n\n"
        "• <b>Entry + N cents</b> — locks in a fixed number of cents per "
        "contract.\n"
        "• <b>Target price</b> — sells at one absolute price regardless of "
        "entry.\n\n"
        "If neither fills before the window closes, the market settles at "
        "100c or 0c and the bot books the result."
    )


LOSS_CHOICES = [500, 1000, 2000, 5000]
EXPOSURE_CHOICES = [1000, 2500, 5000, 10000]
FLOOR_CHOICES = [0, 500, 1000, 5000]


def risk_keyboard(user: User) -> dict:
    loss = int(user.get("daily_loss_limit_cents"))
    exposure = int(user.get("max_exposure_cents"))
    floor = int(user.get("balance_floor_cents"))
    return keyboard(
        [button("Daily loss limit", "noop:_")],
        [
            button(f"{_on(n == loss)} ${n/100:.0f}", f"set:daily_loss_limit_cents:{n}")
            for n in LOSS_CHOICES
        ],
        [button("Max open exposure", "noop:_")],
        [
            button(f"{_on(n == exposure)} ${n/100:.0f}", f"set:max_exposure_cents:{n}")
            for n in EXPOSURE_CHOICES
        ],
        [button("Balance floor (live only)", "noop:_")],
        [
            button(f"{_on(n == floor)} ${n/100:.0f}", f"set:balance_floor_cents:{n}")
            for n in FLOOR_CHOICES
        ],
        [button("‹ Back", "nav:main")],
    )


def risk_text(user: User, realised_today: int, exposure: int, open_count: int) -> str:
    return (
        "<b>Risk caps</b>\n\n"
        f"Today's realised P/L: <b>{realised_today/100:+.2f}</b>\n"
        f"Open exposure: <b>${exposure/100:.2f}</b> across {open_count} position(s)\n\n"
        f"• Daily loss limit — <b>${int(user.get('daily_loss_limit_cents'))/100:.2f}</b>. "
        "Trading pauses for the rest of the UTC day when realised losses reach it.\n"
        f"• Max exposure — <b>${int(user.get('max_exposure_cents'))/100:.2f}</b> of "
        "open cost at once.\n"
        f"• Balance floor — <b>${int(user.get('balance_floor_cents'))/100:.2f}</b> that "
        "the bot will not spend below.\n"
        f"• Max {user.get('max_trades_per_window')} trade(s) per market window, "
        f"{user.get('max_open_positions')} open position(s) total."
    )


# ---------------- positions & P/L ----------------


def positions_text(open_trades: list[Trade], recent: list[Trade]) -> str:
    lines = ["<b>Positions</b>", ""]
    if not open_trades:
        lines.append("<i>No open positions.</i>")
    for t in open_trades:
        tag = "📝" if t.paper else "⚡"
        direction = "UP" if t.side == "yes" else "DOWN"
        lines.append(
            f"{tag} <b>{t.coin} {direction}</b> — {t.count} × {t.entry_price}c → "
            f"target {t.target_price}c\n<code>{t.ticker}</code>"
        )

    closed = [t for t in recent if t.status != "open"]
    if closed:
        lines += ["", "<b>Recent</b>"]
        for t in closed[:8]:
            icon = "✅" if (t.pnl_cents or 0) > 0 else ("➖" if not t.pnl_cents else "❌")
            direction = "UP" if t.side == "yes" else "DOWN"
            lines.append(
                f"{icon} {t.coin} {direction} {t.entry_price}c→{t.exit_price}c "
                f"<b>{(t.pnl_cents or 0)/100:+.2f}</b>"
            )
    return "\n".join(lines)


def positions_keyboard() -> dict:
    return keyboard(
        [button("🔄 Refresh", "nav:positions")], [button("‹ Back", "nav:main")]
    )


def pnl_text(trades: list[Trade], label: str) -> str:
    closed = [t for t in trades if t.status != "open"]
    if not closed:
        return f"<b>P/L — {label}</b>\n\n<i>No closed trades yet.</i>"

    paper = [t for t in closed if t.paper]
    live = [t for t in closed if not t.paper]

    def block(name: str, rows: list[Trade]) -> list[str]:
        if not rows:
            return []
        total = sum(t.pnl_cents or 0 for t in rows)
        wins = len([t for t in rows if (t.pnl_cents or 0) > 0])
        rate = wins / len(rows) * 100
        return [
            f"<b>{name}</b>",
            f"Trades {len(rows)} · Wins {wins} ({rate:.0f}%)",
            f"Net <b>{total/100:+.2f}</b>",
            "",
        ]

    lines = [f"<b>P/L — {label}</b>", ""]
    lines += block("Live", live)
    lines += block("Paper", paper)
    return "\n".join(lines).strip()
