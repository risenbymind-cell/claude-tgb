"""The two ways one signal becomes two positions.

Both share a property that makes them the worst kind of bug in a system that
spends money: every component behaves correctly and every ledger balances. A
second process trades the same signal and its own books are perfect. A second
submission of an unresolved intent is a valid order. Nothing downstream has any
reason to complain, and the account quietly holds double.

Neither is caught by watching P&L either -- twice the position is twice the
win as often as twice the loss.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from kbot.engine.broker import IntentRecorder, LiveBroker
from kbot.lock import AlreadyRunning, InstanceLock
from kbot.storage import Storage


# ---------------- the instance lock ----------------


def test_one_process_takes_the_lock(tmp_path):
    lock = InstanceLock(tmp_path / ".live.lock")
    lock.acquire()
    lock.release()


def test_a_second_process_is_refused(tmp_path):
    """The whole point. Two instances against one account double every
    position and neither notices."""
    first = InstanceLock(tmp_path / ".live.lock")
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning) as exc:
            InstanceLock(tmp_path / ".live.lock").acquire()
        assert "double every position" in str(exc.value)
    finally:
        first.release()


def test_the_refusal_names_the_holder(tmp_path):
    """So the operator can find and stop the other process rather than
    guessing."""
    first = InstanceLock(tmp_path / ".live.lock")
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning) as exc:
            InstanceLock(tmp_path / ".live.lock").acquire()
        assert str(os.getpid()) in str(exc.value)
    finally:
        first.release()


def test_releasing_frees_it(tmp_path):
    path = tmp_path / ".live.lock"
    InstanceLock(path).acquire()  # not held -- released by gc? No: explicit.
    first = InstanceLock(path)
    first.acquire()
    first.release()
    second = InstanceLock(path)
    second.acquire()
    second.release()


def test_the_lock_file_survives_release(tmp_path):
    """Unlinking races a process that already opened it and is waiting: it
    would hold a lock on a path nothing points at, and a third process could
    take the new file. An empty file costs nothing."""
    path = tmp_path / ".live.lock"
    lock = InstanceLock(path)
    lock.acquire()
    lock.release()
    assert path.exists()


def test_it_works_as_a_context_manager(tmp_path):
    path = tmp_path / ".live.lock"
    with InstanceLock(path):
        with pytest.raises(AlreadyRunning):
            InstanceLock(path).acquire()
    InstanceLock(path).acquire()   # free again once the block exits


def test_live_and_paper_take_different_locks(tmp_path):
    """Two simulations racing cost nothing, and blocking a paper desk because
    a live bot is running would be friction with no safety value."""
    db = tmp_path / "data" / "k.sqlite3"
    live = InstanceLock.for_data_dir(db, places_real_orders=True)
    paper = InstanceLock.for_data_dir(db, places_real_orders=False)
    assert live.path != paper.path

    live.acquire()
    try:
        paper.acquire()      # must not raise
        paper.release()
    finally:
        live.release()


def test_separate_data_directories_are_separate_deployments(tmp_path):
    a = InstanceLock.for_data_dir(tmp_path / "a" / "k.sqlite3", places_real_orders=True)
    b = InstanceLock.for_data_dir(tmp_path / "b" / "k.sqlite3", places_real_orders=True)
    a.acquire()
    try:
        b.acquire()
        b.release()
    finally:
        a.release()


def test_the_lock_is_taken_before_anything_trades():
    """Acquired before the storage or the bot exists -- a lock taken after
    the first order is not a lock."""
    import inspect

    from kbot import __main__ as entry

    src = inspect.getsource(entry.amain)
    assert src.index("lock.acquire()") < src.index("Storage(")

    from kbot.webui import __main__ as desk_entry

    src = inspect.getsource(desk_entry.run)
    assert src.index("lock.acquire()") < src.index("desk.start()")


# ---------------- duplicate in-flight intents ----------------


@pytest.fixture()
def storage(tmp_path):
    s = Storage(tmp_path / "k.sqlite3", Fernet.generate_key().decode())
    yield s
    s.close()


class _Client:
    """An exchange that accepts everything, and counts."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_order(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "order_id": f"o{len(self.calls)}", "fill_count": 1,
            "remaining_count": 0, "avg_price_dc": kwargs["price_dc"], "fee_dc": 5,
        }


async def test_an_unresolved_intent_blocks_a_second_submission(storage):
    """One signal, two submissions, double the position -- and both orders
    are individually valid, so nothing downstream objects."""
    client = _Client()
    broker = LiveBroker(client, intents=IntentRecorder(storage, 1, "demo-live"))

    # Write an intent and never resolve it: a submission in flight, or a
    # process that died between recording and reporting.
    await storage.record_intent(
        client_order_id="stuck", tg_id=1, ticker="KXBTC-A", action="buy",
        side="yes", count=10, price_dc=500, mode="demo-live",
    )

    result = await broker.buy("KXBTC-A", "yes", 10, 500, None)
    assert result.ok is False
    assert "already in flight" in result.error
    assert client.calls == [], "no order may reach the exchange"


async def test_a_resolved_intent_does_not_block(storage):
    client = _Client()
    broker = LiveBroker(client, intents=IntentRecorder(storage, 1, "demo-live"))
    await storage.record_intent(
        client_order_id="done", tg_id=1, ticker="KXBTC-A", action="buy",
        side="yes", count=10, price_dc=500, mode="demo-live",
    )
    await storage.resolve_intent("done", status="filled", filled=10)

    result = await broker.buy("KXBTC-A", "yes", 10, 500, None)
    assert result.ok is True
    assert len(client.calls) == 1


async def test_a_stale_intent_does_not_block_forever(storage):
    """An intent pending for an hour is wreckage from a crash, not a
    submission in flight. Blocking all future trading on that market until
    someone reconciles by hand turns a recoverable state into an outage."""
    client = _Client()
    broker = LiveBroker(client, intents=IntentRecorder(storage, 1, "demo-live"))
    await storage.record_intent(
        client_order_id="ancient", tg_id=1, ticker="KXBTC-A", action="buy",
        side="yes", count=10, price_dc=500, mode="demo-live",
    )
    storage._conn.execute(
        "UPDATE order_intents SET created_at=? WHERE client_order_id='ancient'",
        (time.time() - 3600,),
    )
    storage._conn.commit()

    result = await broker.buy("KXBTC-A", "yes", 10, 500, None)
    assert result.ok is True


async def test_a_different_market_is_not_blocked(storage):
    client = _Client()
    broker = LiveBroker(client, intents=IntentRecorder(storage, 1, "demo-live"))
    await storage.record_intent(
        client_order_id="btc", tg_id=1, ticker="KXBTC-A", action="buy",
        side="yes", count=10, price_dc=500, mode="demo-live",
    )
    assert (await broker.buy("KXETH-A", "yes", 10, 500, None)).ok is True


async def test_the_other_side_is_not_blocked(storage):
    """Buying NO while a YES order is in flight is a different trade."""
    client = _Client()
    broker = LiveBroker(client, intents=IntentRecorder(storage, 1, "demo-live"))
    await storage.record_intent(
        client_order_id="y", tg_id=1, ticker="KXBTC-A", action="buy",
        side="yes", count=10, price_dc=500, mode="demo-live",
    )
    assert (await broker.buy("KXBTC-A", "no", 10, 500, None)).ok is True


async def test_selling_is_not_blocked_by_a_pending_buy(storage):
    """Otherwise a stuck entry would prevent the exit that closes it -- the
    guard would create the exposure it exists to prevent."""
    client = _Client()
    broker = LiveBroker(client, intents=IntentRecorder(storage, 1, "demo-live"))
    await storage.record_intent(
        client_order_id="entry", tg_id=1, ticker="KXBTC-A", action="buy",
        side="yes", count=10, price_dc=500, mode="demo-live",
    )
    assert (await broker.sell("KXBTC-A", "yes", 10, 700, None)).ok is True


async def test_another_users_intent_does_not_block(storage):
    """Intents are per account. One user's stuck order must not stop
    everyone else trading that market."""
    client = _Client()
    broker = LiveBroker(client, intents=IntentRecorder(storage, 2, "demo-live"))
    await storage.record_intent(
        client_order_id="theirs", tg_id=1, ticker="KXBTC-A", action="buy",
        side="yes", count=10, price_dc=500, mode="demo-live",
    )
    assert (await broker.buy("KXBTC-A", "yes", 10, 500, None)).ok is True


async def test_the_check_happens_before_the_intent_is_written(storage):
    """Writing first then checking would find its own record and refuse
    every order."""
    client = _Client()
    broker = LiveBroker(client, intents=IntentRecorder(storage, 1, "demo-live"))
    result = await broker.buy("KXBTC-A", "yes", 10, 500, None)
    assert result.ok is True, "an order must not block itself"


async def test_a_blocked_order_writes_no_intent(storage):
    """A refusal that recorded an intent would leave a second unresolved row
    behind, compounding the state it declined to act on."""
    client = _Client()
    broker = LiveBroker(client, intents=IntentRecorder(storage, 1, "demo-live"))
    await storage.record_intent(
        client_order_id="stuck", tg_id=1, ticker="KXBTC-A", action="buy",
        side="yes", count=10, price_dc=500, mode="demo-live",
    )
    await broker.buy("KXBTC-A", "yes", 10, 500, None)
    pending = await storage.pending_intents()
    assert len(pending) == 1
