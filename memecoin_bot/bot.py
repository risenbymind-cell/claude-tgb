"""Simple rules-based memecoin PAPER trader for Solana.

Reads public DexScreener data, applies a fixed checklist before entering, and
enforces sizing / exit / circuit-breaker rules. It never touches a wallet and
cannot spend real money. Educational only, not financial advice.
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import requests

API = "https://api.dexscreener.com"


@dataclass
class Config:
    start_sol: float = 10.0
    # --- vetting (don't buy what you haven't checked)
    min_liquidity_usd: float = 15_000
    min_age_min: float = 10        # skip the sniper window
    max_age_min: float = 360
    min_buys_5m: int = 10
    min_buy_ratio_5m: float = 0.55  # buys / (buys + sells)
    max_5m_change_pct: float = 40   # don't chase a pump
    max_1h_change_pct: float = 300
    min_vol_to_liq_1h: float = 0.3  # someone is actually trading
    max_vol_to_liq_1h: float = 30   # wash-trade smell
    max_fdv_usd: float = 5_000_000
    # --- sizing / risk
    risk_per_trade: float = 0.02    # fraction of bankroll per position
    max_positions: int = 3
    max_exposure: float = 0.5       # of bankroll
    max_slip_pct: float = 5.0       # refuse fills that cost more than this
    fee_pct: float = 1.0
    # --- exits (written before the buy)
    stop_pct: float = 25
    tp1_pct: float = 100            # sell half
    tp1_frac: float = 0.5
    trail_pct: float = 30           # trailing stop on the runner after tp1
    time_stop_min: float = 30       # flat and going nowhere -> out
    time_stop_band_pct: float = 10
    # --- discipline
    daily_loss_limit: float = 0.10  # stop opening trades after -10% on the day
    cooldown_min: float = 20        # no revenge re-entry on a coin you stopped on
    poll_s: int = 30


@dataclass
class Position:
    mint: str
    sym: str
    entry: float          # price in USD at fill (slippage included)
    qty_cost_sol: float   # SOL remaining at cost
    opened: float
    peak: float
    tp1_done: bool = False


@dataclass
class Book:
    sol: float
    positions: dict = field(default_factory=dict)
    cooldown: dict = field(default_factory=dict)
    day: str = ""
    day_start_equity: float = 0.0


# ---------------------------------------------------------------- pure logic

def slippage_pct(size_sol: float, liq_usd: float, sol_usd: float) -> float:
    """Constant-product estimate: buying x into a pool with quote-side y costs ~x/y."""
    pool_sol = (liq_usd / 2) / sol_usd
    return 100 * size_sol / pool_sol if pool_sol > 0 else 100.0


def vet(p: dict, c: Config, now_ms: float | None = None) -> tuple[bool, str]:
    """The checklist. Returns (ok, reason)."""
    now_ms = now_ms or time.time() * 1000
    liq = (p.get("liquidity") or {}).get("usd") or 0
    if liq < c.min_liquidity_usd:
        return False, f"liquidity ${liq:,.0f}"
    created = p.get("pairCreatedAt")
    if not created:
        return False, "unknown age"
    age = (now_ms - created) / 60_000
    if not c.min_age_min <= age <= c.max_age_min:
        return False, f"age {age:.0f}m"
    tx = (p.get("txns") or {}).get("m5") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    if buys < c.min_buys_5m:
        return False, f"only {buys} buys/5m"
    if buys / max(buys + sells, 1) < c.min_buy_ratio_5m:
        return False, "sellers winning"
    pc = p.get("priceChange") or {}
    if pc.get("m5", 0) > c.max_5m_change_pct:
        return False, f"chasing +{pc.get('m5')}% in 5m"
    if pc.get("h1", 0) > c.max_1h_change_pct:
        return False, f"already ran +{pc.get('h1')}% in 1h"
    v1 = (p.get("volume") or {}).get("h1", 0)
    ratio = v1 / liq
    if not c.min_vol_to_liq_1h <= ratio <= c.max_vol_to_liq_1h:
        return False, f"vol/liq {ratio:.1f}"
    if (p.get("fdv") or 0) > c.max_fdv_usd:
        return False, "fdv too high"
    return True, "ok"


def equity(b: Book, prices: dict) -> float:
    v = b.sol
    for m, pos in b.positions.items():
        px = prices.get(m, pos.entry)
        v += pos.qty_cost_sol * px / pos.entry
    return v


def size_for(b: Book, c: Config, prices: dict) -> float:
    eq = equity(b, prices)
    exposed = eq - b.sol
    room = c.max_exposure * eq - exposed
    return max(0.0, min(c.risk_per_trade * eq, room, b.sol))


def exit_action(pos: Position, price: float, c: Config, now: float):
    """Return ('sell_all'|'sell_part', frac, reason) or None."""
    pos.peak = max(pos.peak, price)
    chg = 100 * (price / pos.entry - 1)
    if chg <= -c.stop_pct:
        return "sell_all", 1.0, f"stop {chg:.0f}%"
    if not pos.tp1_done and chg >= c.tp1_pct:
        return "sell_part", c.tp1_frac, f"tp1 +{chg:.0f}%"
    if pos.tp1_done and price <= pos.peak * (1 - c.trail_pct / 100):
        return "sell_all", 1.0, f"trail from peak (+{chg:.0f}%)"
    held = (now - pos.opened) / 60
    if not pos.tp1_done and held >= c.time_stop_min and abs(chg) <= c.time_stop_band_pct:
        return "sell_all", 1.0, f"time stop {held:.0f}m ({chg:+.0f}%)"
    return None


# ------------------------------------------------------------------ execution

def buy(b: Book, c: Config, p: dict, sol_usd: float, now: float, log) -> None:
    mint = p["baseToken"]["address"]
    prices = {mint: float(p["priceUsd"])}
    size = size_for(b, c, {m: pos.entry for m, pos in b.positions.items()} | prices)
    if size <= 0.01:
        return
    slip = slippage_pct(size, p["liquidity"]["usd"], sol_usd)
    if slip > c.max_slip_pct:
        return log("skip", p["baseToken"]["symbol"], f"slip {slip:.1f}% too high")
    fill = float(p["priceUsd"]) * (1 + slip / 100)
    cost = size * (1 + c.fee_pct / 100)
    if cost > b.sol:
        return
    b.sol -= cost
    b.positions[mint] = Position(mint, p["baseToken"]["symbol"], fill, size, now, fill)
    log("buy", p["baseToken"]["symbol"], f"{size:.3f} SOL @ {fill:.8g} slip {slip:.1f}%")


def sell(b: Book, c: Config, pos: Position, price: float, frac: float,
         reason: str, now: float, log) -> None:
    part = pos.qty_cost_sol * frac
    proceeds = part * (price / pos.entry) * (1 - c.fee_pct / 100)
    b.sol += proceeds
    pos.qty_cost_sol -= part
    log("sell", pos.sym, f"{frac:.0%} -> {proceeds:.3f} SOL ({reason})")
    if frac >= 1.0 or pos.qty_cost_sol < 0.005:
        b.positions.pop(pos.mint, None)
        if "stop" in reason or "time" in reason:
            b.cooldown[pos.mint] = now + c.cooldown_min * 60
    else:
        pos.tp1_done = True


# ------------------------------------------------------------------- data I/O

def get(path: str):
    r = requests.get(API + path, timeout=15)
    r.raise_for_status()
    return r.json()


def sol_price() -> float:
    d = get("/latest/dex/tokens/So11111111111111111111111111111111111111112")
    best = max(d["pairs"], key=lambda p: (p.get("liquidity") or {}).get("usd", 0))
    return float(best["priceUsd"])


def candidates() -> list[dict]:
    mints = {x["tokenAddress"] for x in get("/token-profiles/latest/v1") if x["chainId"] == "solana"}
    mints |= {x["tokenAddress"] for x in get("/token-boosts/latest/v1") if x["chainId"] == "solana"}
    out, mints = [], list(mints)
    for i in range(0, len(mints), 30):
        pairs = get("/tokens/v1/solana/" + ",".join(mints[i:i + 30]))
        out += pairs if isinstance(pairs, list) else pairs.get("pairs", [])
    best = {}
    for p in out:  # one pair per token: the deepest
        m = p["baseToken"]["address"]
        if m not in best or (p.get("liquidity") or {}).get("usd", 0) > (best[m].get("liquidity") or {}).get("usd", 0):
            best[m] = p
    return list(best.values())


def prices_for(mints: list[str]) -> dict:
    out = {}
    for i in range(0, len(mints), 30):
        for p in get("/tokens/v1/solana/" + ",".join(mints[i:i + 30])):
            out.setdefault(p["baseToken"]["address"], float(p["priceUsd"]))
    return out


# ----------------------------------------------------------------------- loop

def run(c: Config, state_file: Path, journal: Path, once: bool) -> None:
    b = Book(c.start_sol)
    if state_file.exists():
        d = json.loads(state_file.read_text())
        b = Book(d["sol"], {k: Position(**v) for k, v in d["positions"].items()},
                 d["cooldown"], d["day"], d["day_start_equity"])
    new = not journal.exists()
    jf = journal.open("a", newline="")
    jw = csv.writer(jf)
    if new:
        jw.writerow(["time", "action", "symbol", "detail"])

    def log(action, sym, detail):
        line = (time.strftime("%H:%M:%S"), action, sym, detail)
        print(*line)
        jw.writerow(line)
        jf.flush()

    try:
        while True:
            now = time.time()
            sol_usd = sol_price()
            held = prices_for(list(b.positions))
            today = time.strftime("%Y-%m-%d")
            if b.day != today:
                b.day, b.day_start_equity = today, equity(b, held)

            for m, pos in list(b.positions.items()):
                if m in held and (act := exit_action(pos, held[m], c, now)):
                    sell(b, c, pos, held[m], act[1], act[2], now, log)

            eq = equity(b, held)
            halted = eq <= b.day_start_equity * (1 - c.daily_loss_limit)
            if halted:
                log("halt", "-", f"daily loss limit hit (equity {eq:.2f} SOL); no new entries")
            elif len(b.positions) < c.max_positions:
                for p in candidates():
                    m = p["baseToken"]["address"]
                    if m in b.positions or b.cooldown.get(m, 0) > now:
                        continue
                    ok, why = vet(p, c)
                    if ok:
                        buy(b, c, p, sol_usd, now, log)
                    if len(b.positions) >= c.max_positions:
                        break

            log("status", "-", f"cash {b.sol:.3f} | equity {equity(b, held):.3f} SOL | open {len(b.positions)}")
            state_file.write_text(json.dumps({
                "sol": b.sol, "positions": {k: asdict(v) for k, v in b.positions.items()},
                "cooldown": b.cooldown, "day": b.day, "day_start_equity": b.day_start_equity,
                # read-only extras for the dashboard
                "equity": equity(b, held), "prices": held, "sol_usd": sol_usd,
                "updated": now, "config": asdict(c)}))
            if once:
                return
            time.sleep(c.poll_s)
    finally:
        jf.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="single pass then exit")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--journal", default="journal.csv")
    a = ap.parse_args()
    run(Config(), Path(a.state), Path(a.journal), a.once)


if __name__ == "__main__":
    main()
