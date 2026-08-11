import pytest
from cryptography.fernet import Fernet

from kbot.engine.risk import RiskManager, exit_price_for, start_of_utc_day
from kbot.storage import Storage


@pytest.fixture()
async def user_and_risk(tmp_path):
    storage = Storage(tmp_path / "risk.sqlite3", Fernet.generate_key().decode())
    await storage.upsert_user(1, "trader")
    key = (await storage.mint_keys("lifetime", 1))[0]
    await storage.redeem_key(1, key)
    yield storage, RiskManager(storage)
    storage.close()


async def open_trade(storage, **overrides):
    payload = dict(
        tg_id=1,
        ticker="KXBTCD-1",
        coin="BTC",
        side="yes",
        count=1,
        entry_price=50,
        target_price=58,
        paper=True,
        entry_order_id=None,
        reason=None,
    )
    payload.update(overrides)
    return await storage.record_entry(**payload)


@pytest.mark.asyncio
async def test_allows_a_clean_trade(user_and_risk):
    storage, risk = user_and_risk
    user = await storage.get_user(1)
    decision = await risk.check(user, ticker="KXBTCD-1", entry_price=45, balance_cents=10000)
    assert decision.allowed
    assert decision.contracts == 1


@pytest.mark.asyncio
async def test_blocks_price_above_the_cap(user_and_risk):
    storage, risk = user_and_risk
    user = await storage.update_settings(1, {"max_entry_price": 60})
    decision = await risk.check(user, ticker="KXBTCD-1", entry_price=70, balance_cents=10000)
    assert not decision.allowed and "cap" in decision.reason


@pytest.mark.asyncio
async def test_blocks_price_below_the_floor(user_and_risk):
    storage, risk = user_and_risk
    user = await storage.get_user(1)
    decision = await risk.check(user, ticker="KXBTCD-1", entry_price=5, balance_cents=10000)
    assert not decision.allowed and "floor" in decision.reason


@pytest.mark.asyncio
async def test_daily_loss_limit_stops_trading(user_and_risk):
    storage, risk = user_and_risk
    user = await storage.update_settings(1, {"daily_loss_limit_cents": 100})
    trade_id = await open_trade(storage, entry_price=60)
    await storage.close_trade(trade_id, exit_price=10)  # -50c

    decision = await risk.check(user, ticker="KXBTCD-2", entry_price=45, balance_cents=10000)
    assert decision.allowed  # one loss is not yet the limit

    trade_id = await open_trade(storage, entry_price=60, ticker="KXBTCD-3")
    await storage.close_trade(trade_id, exit_price=0)  # another -60c
    decision = await risk.check(user, ticker="KXBTCD-4", entry_price=45, balance_cents=10000)
    assert not decision.allowed and "loss limit" in decision.reason


@pytest.mark.asyncio
async def test_per_window_limit_blocks_a_second_entry(user_and_risk):
    storage, risk = user_and_risk
    user = await storage.get_user(1)
    await open_trade(storage, ticker="KXBTCD-1")
    decision = await risk.check(user, ticker="KXBTCD-1", entry_price=45, balance_cents=10000)
    assert not decision.allowed
    assert "window" in decision.reason or "open position" in decision.reason


@pytest.mark.asyncio
async def test_open_position_limit(user_and_risk):
    storage, risk = user_and_risk
    user = await storage.update_settings(1, {"max_open_positions": 1})
    await open_trade(storage, ticker="KXBTCD-1")
    decision = await risk.check(user, ticker="KXBTCD-9", entry_price=45, balance_cents=10000)
    assert not decision.allowed and "open position" in decision.reason


@pytest.mark.asyncio
async def test_exposure_cap_scales_size_down(user_and_risk):
    storage, risk = user_and_risk
    user = await storage.update_settings(
        1, {"contracts": 10, "max_exposure_cents": 200, "max_trades_per_window": 5}
    )
    decision = await risk.check(user, ticker="KXBTCD-1", entry_price=50, balance_cents=100000)
    assert decision.allowed
    assert decision.contracts == 4  # 200c of room at 50c each


@pytest.mark.asyncio
async def test_balance_floor_scales_size_down(user_and_risk):
    storage, risk = user_and_risk
    user = await storage.update_settings(
        1, {"contracts": 10, "balance_floor_cents": 1000, "max_exposure_cents": 100000}
    )
    decision = await risk.check(user, ticker="KXBTCD-1", entry_price=50, balance_cents=1150)
    assert decision.allowed
    assert decision.contracts == 3  # only 150c spendable above the floor


@pytest.mark.asyncio
async def test_balance_at_the_floor_blocks_entirely(user_and_risk):
    storage, risk = user_and_risk
    user = await storage.update_settings(1, {"balance_floor_cents": 1000})
    decision = await risk.check(user, ticker="KXBTCD-1", entry_price=50, balance_cents=900)
    assert not decision.allowed and "floor" in decision.reason


@pytest.mark.asyncio
async def test_paper_mode_ignores_balance(user_and_risk):
    storage, risk = user_and_risk
    user = await storage.update_settings(1, {"balance_floor_cents": 100000})
    decision = await risk.check(user, ticker="KXBTCD-1", entry_price=50, balance_cents=None)
    assert decision.allowed


# ---------------- exit pricing ----------------


class FakeUser:
    def __init__(self, **settings):
        self.settings = settings

    def get(self, name):
        return self.settings[name]


def test_exit_price_profit_mode():
    user = FakeUser(exit_mode="profit", profit_cents=8, target_price=90)
    assert exit_price_for(user, 45) == 53


def test_exit_price_target_mode():
    user = FakeUser(exit_mode="target", profit_cents=8, target_price=90)
    assert exit_price_for(user, 45) == 90


def test_exit_price_is_clamped_below_100():
    user = FakeUser(exit_mode="profit", profit_cents=20, target_price=90)
    assert exit_price_for(user, 95) == 99


def test_exit_price_always_beats_entry():
    user = FakeUser(exit_mode="target", profit_cents=5, target_price=30)
    assert exit_price_for(user, 60) == 61


def test_start_of_utc_day_is_midnight():
    import datetime

    ts = start_of_utc_day()
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    assert (dt.hour, dt.minute, dt.second) == (0, 0, 0)
