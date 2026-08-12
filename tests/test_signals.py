"""Signal analysis: does a book feature predict the next move?

The tool's job is to say "yes" only when that is true. Two failure modes
matter, and both are tested here: missing a real signal makes it useless, and
reporting one that is not there is worse than useless, because it is the exact
mistake the whole research pipeline exists to prevent.
"""

from __future__ import annotations

import random

import pytest

from kbot.research.signals import MIN_WINDOWS, Series, analyse, format_analysis
from kbot.research.store import RecordWriter

WINDOW = 900.0


def write_market(
    writer: RecordWriter,
    *,
    ticker: str,
    coin: str,
    open_time: float,
    mids: list[float],
    imbalances: list[float] | None = None,
) -> None:
    """Lay down one market's worth of snapshots with a chosen mid path.

    The book is synthesised around each mid: a bid a tick below, an ask a tick
    above, with depth split to produce the requested imbalance.
    """
    for i, mid in enumerate(mids):
        imb = 0.0 if imbalances is None else imbalances[i]
        yes_qty = 100.0 * (1.0 + imb)
        no_qty = 100.0 * (1.0 - imb)
        bid = int(mid) - 10
        ask = int(mid) + 10
        writer.write_book(
            ticker=ticker,
            coin=coin,
            open_time=open_time,
            close_time=open_time + WINDOW,
            # Recordings hold deci-cents already, not wire dollars.
            yes=[[bid, max(1.0, yes_qty)], [bid - 10, max(1.0, yes_qty)]],
            no=[[1000 - ask, max(1.0, no_qty)], [1000 - ask - 10, max(1.0, no_qty)]],
            t=open_time + i,
        )


@pytest.fixture()
def rec_dir(tmp_path):
    return tmp_path / "recordings"


def test_a_planted_signal_is_found(rec_dir):
    """Imbalance is made genuinely predictive; the tool must see it.

    Without this test, a tool that reports "no edge" for every input would
    pass every other check in this file.
    """
    rng = random.Random(7)
    writer = RecordWriter(rec_dir)
    for w in range(MIN_WINDOWS + 20):
        open_time = 1_700_000_000 + w * WINDOW
        mid = 500.0
        mids, imbs = [], []
        for _ in range(120):
            imb = rng.uniform(-1, 1)
            mids.append(mid)
            imbs.append(imb)
            # Tomorrow's move follows today's imbalance, plus real noise.
            mid = max(100.0, min(900.0, mid + imb * 6 + rng.gauss(0, 2)))
        write_market(
            writer, ticker=f"T{w}", coin="BTC", open_time=open_time,
            mids=mids, imbalances=imbs,
        )
    writer.close()

    result = analyse(rec_dir, horizon=10.0)
    assert result.windows >= MIN_WINDOWS
    ic = result.series["imbalance"].ic
    t = result.series["imbalance"].t_stat(10.0, result.interval)
    assert ic > 0.2, f"planted signal was missed (IC {ic:.3f})"
    assert t > 5, f"planted signal not significant (t {t:.2f})"

    text = format_analysis(result)
    assert "worth building on" in text or "implausible" in text


def test_pure_noise_reports_no_signal(rec_dir):
    """A random walk with random imbalance must not produce an edge."""
    rng = random.Random(11)
    writer = RecordWriter(rec_dir)
    for w in range(MIN_WINDOWS + 20):
        open_time = 1_700_000_000 + w * WINDOW
        mid = 500.0
        mids, imbs = [], []
        for _ in range(120):
            mids.append(mid)
            imbs.append(rng.uniform(-1, 1))  # unrelated to the path
            mid = max(100.0, min(900.0, mid + rng.gauss(0, 4)))
        write_market(
            writer, ticker=f"N{w}", coin="ETH", open_time=open_time,
            mids=mids, imbalances=imbs,
        )
    writer.close()

    result = analyse(rec_dir, horizon=10.0)
    assert abs(result.series["imbalance"].ic) < 0.05, "found an edge in pure noise"
    assert "noise" in format_analysis(result)


def test_a_single_window_is_never_a_verdict(rec_dir):
    """Nine coins over one quarter hour is one observation, not nine.

    This is the guard against the most seductive version of the mistake: a
    t-statistic that looks conclusive because thousands of snapshots were
    counted as independent when they describe a single market move.
    """
    writer = RecordWriter(rec_dir)
    open_time = 1_700_000_000
    for coin in ("BTC", "ETH", "SOL", "XRP"):
        mids, imbs = [], []
        mid = 500.0
        for i in range(200):
            imbs.append(1.0)          # a perfectly consistent "signal"
            mids.append(mid)
            mid += 2                  # and a perfectly consistent move
        write_market(
            writer, ticker=f"S-{coin}", coin=coin, open_time=open_time,
            mids=mids, imbalances=imbs,
        )
    writer.close()

    text = format_analysis(analyse(rec_dir, horizon=10.0))
    assert "NOT ENOUGH DATA" in text
    assert "worth building on" not in text


def test_overlapping_horizons_are_discounted():
    """1,000 samples of a 60s horizon taken every second are not 1,000
    independent observations, and pretending otherwise inflates every
    t-statistic by roughly the square root of the overlap."""
    rng = random.Random(3)
    s = Series("x")
    for i in range(1000):
        s.add(float(i), float(i) + rng.gauss(0, 200))
    assert s.n_eff(horizon=60.0, interval=1.0) == pytest.approx(1000 / 60, rel=0.01)
    assert s.n_eff(horizon=1.0, interval=1.0) == 1000
    # Same correlation, far smaller t once the overlap is accounted for.
    assert abs(s.t_stat(60.0, 1.0)) < abs(s.t_stat(1.0, 1.0))


def test_a_gap_in_the_recording_is_not_a_prediction(rec_dir):
    """If the recorder stopped for an hour, the snapshot before the gap must
    not be paired with the one after it."""
    writer = RecordWriter(rec_dir)
    open_time = 1_700_000_000
    for i in range(30):
        writer.write_book(
            ticker="G", coin="BTC", open_time=open_time,
            close_time=open_time + WINDOW,
            yes=[[490, 100.0]], no=[[490, 100.0]],
            t=open_time + (i if i < 15 else i + 3600),
        )
    writer.close()
    result = analyse(rec_dir, horizon=30.0)
    # Only pairs on the near side of the gap can exist, and there are none
    # within the horizon, so nothing should be paired at all.
    assert result.snapshots == 0


def test_prices_are_read_as_deci_cents_not_dollars(rec_dir):
    """Recordings store deci-cents. Re-parsing them as wire dollars multiplies
    every price by a thousand, which produces forward "moves" of tens of
    dollars on a contract that cannot exceed one."""
    writer = RecordWriter(rec_dir)
    open_time = 1_700_000_000
    write_market(
        writer, ticker="U", coin="BTC", open_time=open_time,
        mids=[500.0] * 60, imbalances=[0.0] * 60,
    )
    writer.close()
    result = analyse(rec_dir, horizon=5.0)
    for series in result.series.values():
        for _, forward in series.pairs:
            assert abs(forward) <= 1000, "price scale is wrong by 1000x"
