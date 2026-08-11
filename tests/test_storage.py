import time

import pytest
from cryptography.fernet import Fernet

from kbot.storage import Storage


@pytest.fixture()
def storage(tmp_path):
    s = Storage(tmp_path / "test.sqlite3", Fernet.generate_key().decode())
    yield s
    s.close()


@pytest.mark.asyncio
async def test_new_user_gets_conservative_defaults(storage):
    user = await storage.upsert_user(1, "trader")
    assert user.get("paper") is True
    assert user.get("mode") == "manual"
    assert user.enabled is False
    assert user.has_access is False


@pytest.mark.asyncio
async def test_settings_merge_rather_than_replace(storage):
    await storage.upsert_user(1, "trader")
    await storage.update_settings(1, {"contracts": 5})
    user = await storage.update_settings(1, {"mode": "auto"})
    assert user.get("contracts") == 5
    assert user.get("mode") == "auto"
    assert user.get("paper") is True  # untouched default survives


@pytest.mark.asyncio
async def test_credentials_round_trip_encrypted(storage):
    await storage.upsert_user(1, "trader")
    await storage.set_credentials(1, "key-id", "PEM-BODY")
    user = await storage.get_user(1)
    assert user.kalshi_key_id == "key-id"
    assert user.kalshi_secret == "PEM-BODY"

    # The stored blob must not contain the plaintext.
    row = storage._conn.execute(
        "SELECT kalshi_secret FROM users WHERE tg_id = 1"
    ).fetchone()
    assert b"PEM-BODY" not in row["kalshi_secret"]


@pytest.mark.asyncio
async def test_credentials_from_a_different_master_key_read_as_absent(tmp_path):
    path = tmp_path / "test.sqlite3"
    first = Storage(path, Fernet.generate_key().decode())
    await first.upsert_user(1, "trader")
    await first.set_credentials(1, "key-id", "PEM-BODY")
    first.close()

    second = Storage(path, Fernet.generate_key().decode())
    user = await second.get_user(1)
    assert user.kalshi_secret is None
    assert user.has_credentials is False
    second.close()


@pytest.mark.asyncio
async def test_key_redemption_grants_access_once(storage):
    await storage.upsert_user(1, "trader")
    await storage.upsert_user(2, "other")
    key = (await storage.mint_keys("weekly", 1))[0]

    ok, _ = await storage.redeem_key(1, key)
    assert ok
    user = await storage.get_user(1)
    assert user.has_access

    ok, message = await storage.redeem_key(2, key)
    assert not ok and "already" in message.lower()


@pytest.mark.asyncio
async def test_redeeming_stacks_onto_remaining_time(storage):
    await storage.upsert_user(1, "trader")
    keys = await storage.mint_keys("daily", 2)
    await storage.redeem_key(1, keys[0])
    first = (await storage.get_user(1)).access_until
    await storage.redeem_key(1, keys[1])
    second = (await storage.get_user(1)).access_until
    assert second - first == pytest.approx(86400, abs=5)


@pytest.mark.asyncio
async def test_lifetime_key_never_expires(storage):
    await storage.upsert_user(1, "trader")
    key = (await storage.mint_keys("lifetime", 1))[0]
    await storage.redeem_key(1, key)
    user = await storage.get_user(1)
    assert user.lifetime and user.has_access


@pytest.mark.asyncio
async def test_unknown_key_is_rejected(storage):
    await storage.upsert_user(1, "trader")
    ok, _ = await storage.redeem_key(1, "NOT-A-KEY")
    assert not ok


@pytest.mark.asyncio
async def test_active_users_excludes_expired_access(storage):
    await storage.upsert_user(1, "trader")
    await storage.set_enabled(1, True)
    assert await storage.active_users() == []
    assert len(await storage.active_users(require_access=False)) == 1

    key = (await storage.mint_keys("daily", 1))[0]
    await storage.redeem_key(1, key)
    assert len(await storage.active_users()) == 1


@pytest.mark.asyncio
async def test_trade_lifecycle_computes_pnl(storage):
    await storage.upsert_user(1, "trader")
    trade_id = await storage.record_entry(
        tg_id=1,
        ticker="KXBTC15M-1",
        coin="BTC",
        side="yes",
        count=3,
        entry_price_dc=450,
        target_price_dc=530,
        paper=True,
        entry_order_id="o1",
        reason="test",
    )
    assert len(await storage.open_trades(1)) == 1

    closed = await storage.close_trade(trade_id, exit_price_dc=530)
    assert closed is not None
    assert closed.pnl_dc == (530 - 450) * 3
    assert closed.status == "closed"
    assert await storage.open_trades(1) == []


@pytest.mark.asyncio
async def test_closing_twice_is_a_no_op(storage):
    await storage.upsert_user(1, "trader")
    trade_id = await storage.record_entry(
        tg_id=1,
        ticker="KXBTC15M-1",
        coin="BTC",
        side="yes",
        count=1,
        entry_price_dc=400,
        target_price_dc=500,
        paper=True,
        entry_order_id=None,
        reason=None,
    )
    assert await storage.close_trade(trade_id, exit_price_dc=500) is not None
    assert await storage.close_trade(trade_id, exit_price_dc=100) is None


@pytest.mark.asyncio
async def test_trades_since_filters_by_time(storage):
    await storage.upsert_user(1, "trader")
    await storage.record_entry(
        tg_id=1,
        ticker="KXBTC15M-1",
        coin="BTC",
        side="yes",
        count=1,
        entry_price_dc=400,
        target_price_dc=500,
        paper=True,
        entry_order_id=None,
        reason=None,
    )
    assert len(await storage.trades_since(1, time.time() - 60)) == 1
    assert await storage.trades_since(1, time.time() + 60) == []
