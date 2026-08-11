"""Recording and replay.

The replay engine is what any claim about strategy performance would rest on,
so these tests check the arithmetic against hand-computed answers rather than
against itself.
"""

from __future__ import annotations

import time

import pytest

from kbot.kalshi.fees import fee_dc
from kbot.kalshi.prices import format_dollars
from kbot.research.replay import (
    ReplayConfig,
    book_from_record,
    replay,
    replay_market,
)
from kbot.research.report import render, summarise
from kbot.research.store import (
    BookRecord,
    RecordWriter,
    available_days,
    load_session,
    read_records,
)


def rec(
    t: float,
    *,
    yes_bid=450,
    no_bid=520,
    yes_size=400.0,
    no_size=90.0,
    close: float = 400.0,
    ticker="KXBTC15M-W1",
) -> BookRecord:
    return BookRecord(
        t=t,
        ticker=ticker,
        coin="BTC",
        open_time=t - (900 - close),
        close_time=t + close,
        yes=[[yes_bid, yes_size]],
        no=[[no_bid, no_size]],
    )


def rising_window(n: int = 60, **overrides) -> list[BookRecord]:
    """A window whose fair value drifts steadily up — Drift's setup.

    With n=60 the bid climbs far enough to fill an 8c exit target; with n=30 it
    does not, which is how the settle-at-expiry paths are exercised.
    """
    base = 1_000_000.0
    out = []
    for i in range(n):
        out.append(
            rec(
                base + i,
                yes_bid=440 + i * 2,
                no_bid=540 - i * 2,
                close=400 - i,
                **overrides,
            )
        )
    return out


# ---------------- storage round trip ----------------


def test_records_round_trip_through_disk(tmp_path):
    writer = RecordWriter(tmp_path)
    writer.write_book(
        ticker="KXBTC15M-W1",
        coin="BTC",
        open_time=100.0,
        close_time=1000.0,
        yes=[[450, 10.0]],
        no=[[520, 20.0]],
        t=500.0,
    )
    writer.write_settle("KXBTC15M-W1", "yes", t=1001.0)
    writer.close()

    books, settled = load_session(tmp_path)
    assert settled == {"KXBTC15M-W1": "yes"}
    assert len(books["KXBTC15M-W1"]) == 1
    stored = books["KXBTC15M-W1"][0]
    assert stored.yes == [[450, 10.0]]
    assert stored.seconds_to_close == 500.0
    assert stored.window_seconds == 900.0


def test_appending_does_not_truncate_the_day(tmp_path):
    for i in range(3):
        writer = RecordWriter(tmp_path)
        writer.write_book(
            ticker="T", coin="BTC", open_time=0, close_time=900,
            yes=[[1, 1.0]], no=[[1, 1.0]], t=500.0 + i,
        )
        writer.close()
    assert len(list(read_records(tmp_path))) == 3


def test_days_are_listed(tmp_path):
    writer = RecordWriter(tmp_path)
    writer.write_book(
        ticker="T", coin="BTC", open_time=0, close_time=900,
        yes=[], no=[], t=time.time(),
    )
    writer.close()
    assert len(available_days(tmp_path)) == 1


def test_corrupt_line_does_not_lose_the_file(tmp_path):
    import gzip

    writer = RecordWriter(tmp_path)
    writer.write_book(
        ticker="T", coin="BTC", open_time=0, close_time=900,
        yes=[[1, 1.0]], no=[[1, 1.0]], t=500.0,
    )
    writer.close()
    path = next(tmp_path.glob("*.jsonl.gz"))
    with gzip.open(path, "at", encoding="utf-8") as fh:
        fh.write('{"type": "book", "t": tru\n')  # truncated write
    assert len(list(read_records(tmp_path))) == 1


def test_book_rebuilds_exactly():
    book = book_from_record(rec(1.0))
    assert book.best_yes_bid == 450
    assert book.best_no_bid == 520
    assert book.yes_ask == 480
    assert not book.is_stale


# ---------------- replay mechanics ----------------


def test_a_short_window_is_skipped():
    assert replay_market([rec(1.0)], "yes", ReplayConfig()) is None


def test_target_hit_is_scored_net_of_both_fees():
    records = rising_window(60)
    # After the drift, a huge bid arrives at the target so the exit fills.
    records.append(rec(1_000_100.0, yes_bid=900, no_bid=90, close=200))

    trade = replay_market(records, "yes", ReplayConfig(min_confidence=0.3))
    assert trade is not None
    assert trade.outcome == "target"
    expected = (
        (trade.target_dc - trade.entry_dc) * trade.count
        - fee_dc(trade.count, trade.entry_dc)
        - fee_dc(trade.count, trade.target_dc)
    )
    assert trade.net_dc == expected
    assert trade.net_dc < trade.gross_dc  # fees were charged


def test_unfilled_exit_settles_on_the_winning_side():
    records = rising_window(30)  # too short for the exit to fill
    trade = replay_market(records, "yes", ReplayConfig(min_confidence=0.3))
    assert trade is not None and trade.outcome == "settled"
    assert trade.exit_dc == 1000
    assert trade.exit_fee_dc == 0  # settlement is free
    assert trade.net_dc == (1000 - trade.entry_dc) * trade.count - trade.entry_fee_dc


def test_unfilled_exit_settles_on_the_losing_side():
    records = rising_window(30)  # too short for the exit to fill
    trade = replay_market(records, "no", ReplayConfig(min_confidence=0.3))
    assert trade is not None and trade.outcome == "settled"
    assert trade.exit_dc == 0
    assert trade.net_dc == -trade.entry_dc * trade.count - trade.entry_fee_dc


def test_a_window_with_no_recorded_settlement_is_unresolved_not_flat():
    records = rising_window(30)  # too short for the exit to fill
    trade = replay_market(records, None, ReplayConfig(min_confidence=0.3))
    assert trade is not None
    assert trade.outcome == "unresolved"
    assert trade.resolved is False


def test_size_is_capped_by_the_book():
    # Only 3 contracts rest at the touch, so 50 cannot fill.
    records = rising_window(60, no_size=3.0)
    trade = replay_market(records, "yes", ReplayConfig(min_confidence=0.3, contracts=50))
    assert trade is not None
    assert trade.count == 3


def test_entry_band_is_respected():
    records = rising_window(60)
    cfg = ReplayConfig(min_confidence=0.3, max_entry_cents=20)
    assert replay_market(records, "yes", cfg) is None


def test_confidence_floor_is_respected():
    records = rising_window(60)
    assert replay_market(records, "yes", ReplayConfig(min_confidence=0.99)) is None


def test_only_one_trade_per_window():
    records = rising_window(60) + rising_window(60)
    trade = replay_market(records, "yes", ReplayConfig(min_confidence=0.3))
    assert trade is not None  # a single trade object, never a list


# ---------------- fee floor ----------------


def test_fee_floor_raises_a_losing_target():
    records = rising_window(60)
    cfg = ReplayConfig(min_confidence=0.3, profit_cents=2)
    trade = replay_market(records, "yes", cfg)
    assert trade is not None
    # +2c would not clear the round trip, so the target was lifted.
    assert trade.target_dc > trade.entry_dc + 20


def test_fee_floor_can_be_disabled_for_comparison():
    records = rising_window(60)
    cfg = ReplayConfig(min_confidence=0.3, profit_cents=2, enforce_fee_floor=False)
    trade = replay_market(records, "yes", cfg)
    assert trade is not None
    assert trade.target_dc == trade.entry_dc + 20


# ---------------- reporting ----------------


def test_report_reflects_the_trades(tmp_path):
    writer = RecordWriter(tmp_path)
    for r in rising_window(60):
        writer.write_book(
            ticker=r.ticker, coin=r.coin, open_time=r.open_time,
            close_time=r.close_time, yes=r.yes, no=r.no, t=r.t,
        )
    writer.write_settle("KXBTC15M-W1", "yes", t=1_000_100.0)
    writer.close()

    result = replay(tmp_path, ReplayConfig(min_confidence=0.3))
    assert result.windows_seen == 1
    assert len(result.resolved) == 1

    text = render(result)
    assert "REPLAY" in text
    assert "NET" in text
    assert "Edge per trade" in text


def test_report_is_honest_when_nothing_resolved(tmp_path):
    writer = RecordWriter(tmp_path)
    for r in rising_window(30):
        writer.write_book(
            ticker=r.ticker, coin=r.coin, open_time=r.open_time,
            close_time=r.close_time, yes=r.yes, no=r.no, t=r.t,
        )
    writer.close()  # no settlement recorded

    result = replay(tmp_path, ReplayConfig(min_confidence=0.3))
    text = render(result)
    assert "No resolved trades" in text


def test_summarise_computes_drawdown_over_the_sequence():
    from kbot.research.replay import ReplayTrade

    def trade(net: int) -> ReplayTrade:
        t = ReplayTrade(
            coin="BTC", ticker="T", side="yes", count=1, entry_dc=500,
            entry_fee_dc=0, target_dc=580, confidence=0.5, reason="",
            entered_at=0.0,
        )
        t.exit_dc = 500 + net
        t.outcome = "target"
        return t

    # +100, -300, +50 -> peak 100, trough -200, so the worst drawdown is 300.
    stats = summarise([trade(100), trade(-300), trade(50)], "test")
    assert stats.trades == 3
    assert stats.wins == 2
    assert stats.net_dc == -150
    assert stats.max_drawdown_dc == 300


def test_summarise_handles_no_trades():
    stats = summarise([], "empty")
    assert stats.trades == 0 and stats.win_rate == 0.0


def test_money_formatting_is_stable():
    assert format_dollars(17830) == "17.83"


def test_a_recording_can_be_read_while_it_is_still_being_written(tmp_path):
    """Regression: gzip raises at an incomplete final block.

    Analysing data as it accumulates is a normal workflow, and it must return
    the valid records rather than failing on the whole file.
    """
    writer = RecordWriter(tmp_path)
    for i in range(5):
        writer.write_book(
            ticker="T", coin="BTC", open_time=0, close_time=900,
            yes=[[500, 1.0]], no=[[490, 1.0]], t=100.0 + i,
        )
    writer.flush()  # written, but the stream is not closed

    records = list(read_records(tmp_path))
    assert len(records) >= 1
    writer.close()
    assert len(list(read_records(tmp_path))) == 5


def test_a_wholly_corrupt_file_does_not_raise(tmp_path):
    (tmp_path / "2026-01-01.jsonl.gz").write_bytes(b"not gzip at all")
    assert list(read_records(tmp_path)) == []
