"""Calibration — does the price predict the outcome?

The diagnostic that catches a backtest built on data where price and settlement
share a source. These tests build both a well-priced market and a rigged one and
check it can tell them apart.
"""

from __future__ import annotations

import random

from kbot.research.calibrate import Bucket, calibration
from kbot.research.calibrate import render as render_calibration
from kbot.research.store import RecordWriter


def write_market(writer, ticker, price_dc, result, *, t0=1_000_000.0, n=40):
    for i in range(n):
        writer.write_book(
            ticker=ticker, coin="BTC", open_time=t0, close_time=t0 + 900,
            yes=[[price_dc - 10, 100.0]], no=[[1000 - price_dc - 10, 100.0]],
            t=t0 + i * 10,
        )
    writer.write_settle(ticker, result, t=t0 + 930)


# ---------------- the arithmetic ----------------


def test_bucket_edge_and_significance():
    fair = Bucket(low_dc=600, high_dc=700, observations=100, settled_yes=65)
    assert fair.implied == 0.65
    assert fair.actual == 0.65
    assert abs(fair.edge) < 0.01
    assert not fair.significant

    skewed = Bucket(low_dc=600, high_dc=700, observations=100, settled_yes=95)
    assert skewed.edge > 0.25
    assert skewed.significant


def test_thin_buckets_are_never_significant():
    assert not Bucket(600, 700, 1, 1).significant


def test_ev_accounts_for_the_fee():
    # A perfectly priced 65c contract still loses, because of the fee.
    fair = Bucket(600, 700, 1000, 650)
    assert fair.net_per_contract_dc() < 0


def test_ev_is_positive_when_the_gap_beats_the_fee():
    assert Bucket(600, 700, 1000, 900).net_per_contract_dc() > 0


# ---------------- against recordings ----------------


def test_an_efficient_market_shows_almost_no_gap(tmp_path):
    writer = RecordWriter(tmp_path)
    random.seed(1)
    t = 1_000_000.0
    # 65c contracts that settle yes 65% of the time: correctly priced.
    for i in range(200):
        write_market(
            writer, f"T{i}", 650, "yes" if random.random() < 0.65 else "no", t0=t
        )
        t += 1000
    writer.close()

    rows = calibration(tmp_path)
    band = [r for r in rows if r.low_dc == 600][0]
    assert band.observations == 200
    assert abs(band.edge) < 0.08
    assert "close to correctly priced" in render_calibration(rows)


def test_a_rigged_market_is_flagged(tmp_path):
    writer = RecordWriter(tmp_path)
    t = 1_000_000.0
    # 65c contracts that always settle yes: a generator artifact, not an edge.
    for i in range(200):
        write_market(writer, f"T{i}", 650, "yes", t0=t)
        t += 1000
    writer.close()

    rows = calibration(tmp_path)
    band = [r for r in rows if r.low_dc == 600][0]
    assert band.actual == 1.0
    assert band.significant
    text = render_calibration(rows)
    assert "very large gap" in text


def test_a_market_never_seen_near_the_sample_point_is_skipped(tmp_path):
    """Only observations close to the chosen point in the window count."""
    writer = RecordWriter(tmp_path)
    # Snapshots only in the first minute; nothing near 450s before close.
    write_market(writer, "T1", 650, "yes", n=5)
    writer.close()
    assert calibration(tmp_path) == []


def test_markets_without_a_settlement_are_skipped(tmp_path):
    writer = RecordWriter(tmp_path)
    for i in range(20):
        writer.write_book(
            ticker=f"T{i}", coin="BTC", open_time=0.0, close_time=900.0,
            yes=[[500, 10.0]], no=[[490, 10.0]], t=450.0,
        )
    writer.close()
    assert calibration(tmp_path) == []


def test_empty_input_renders_a_useful_message(tmp_path):
    assert "No settled markets" in render_calibration(calibration(tmp_path))


def test_each_market_counts_once(tmp_path):
    """A market that sat still must not outvote one that moved."""
    writer = RecordWriter(tmp_path)
    write_market(writer, "T1", 650, "yes", n=200)  # many snapshots
    write_market(writer, "T2", 650, "no", t0=2_000_000.0, n=40)  # few
    writer.close()

    rows = calibration(tmp_path)
    band = [r for r in rows if r.low_dc == 600][0]
    assert band.observations == 2  # one per market, not per snapshot
    assert band.settled_yes == 1


def test_coin_filter(tmp_path):
    writer = RecordWriter(tmp_path)
    write_market(writer, "T1", 650, "yes")
    writer.close()
    assert calibration(tmp_path, coins=["ETH"]) == []
    assert calibration(tmp_path, coins=["BTC"]) != []
