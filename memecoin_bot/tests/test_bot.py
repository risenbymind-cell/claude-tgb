import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import *

C = Config()
NOW = 1_000_000_000_000


def pair(**kw):
    p = {"liquidity": {"usd": 50_000}, "pairCreatedAt": NOW - 60 * 60_000,
         "txns": {"m5": {"buys": 30, "sells": 15}}, "priceChange": {"m5": 5, "h1": 40},
         "volume": {"h1": 40_000}, "fdv": 500_000}
    p.update(kw)
    return p


def test_vet_passes_clean_pair():
    assert vet(pair(), C, NOW)[0]


def test_vet_rejects_each_red_flag():
    assert not vet(pair(liquidity={"usd": 1000}), C, NOW)[0]
    assert not vet(pair(pairCreatedAt=NOW - 2 * 60_000), C, NOW)[0]      # sniper window
    assert not vet(pair(priceChange={"m5": 80, "h1": 90}), C, NOW)[0]    # chasing
    assert not vet(pair(volume={"h1": 5_000_000}), C, NOW)[0]            # fake volume
    assert not vet(pair(txns={"m5": {"buys": 12, "sells": 40}}), C, NOW)[0]


def test_slippage_matches_site_example():
    # 1 SOL into a 30 SOL pool is ~3%
    assert 3.0 < slippage_pct(1, 30 * 2 * 100, 100) < 3.5


def test_sizing_respects_risk_and_exposure():
    b = Book(10.0)
    assert abs(size_for(b, C, {}) - 0.2) < 1e-9


def test_stop_tp_trail_time():
    pos = Position("m", "X", 1.0, 0.2, 0, 1.0)
    assert exit_action(pos, 0.7, C, 60)[0] == "sell_all"
    assert exit_action(pos, 2.1, C, 60)[0] == "sell_part"
    pos.tp1_done, pos.peak = True, 3.0
    assert exit_action(pos, 2.0, C, 60)[2].startswith("trail")
    flat = Position("m", "X", 1.0, 0.2, 0, 1.0)
    assert exit_action(flat, 1.02, C, 31 * 60)[2].startswith("time stop")


def test_stop_sets_cooldown_and_cash_back():
    b = Book(9.8)
    pos = Position("m", "X", 1.0, 0.2, 0, 1.0)
    b.positions["m"] = pos
    sell(b, C, pos, 0.7, 1.0, "stop -30%", 100, lambda *a: None)
    assert "m" not in b.positions and b.cooldown["m"] > 100 and b.sol > 9.8
