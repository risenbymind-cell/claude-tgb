"""Win rate by entry price.

Two things here are worth more than the rest: that a trade is never priced
with a quote from after the decision, and that the interval reported on a band
is one that survives the band's actual shape. Both have gone wrong in this
project before, and both fail silently.
"""

from __future__ import annotations

from kbot.kalshi.fees import fee_dc
from kbot.research.bands import (
    DEFAULT_BANDS,
    analyse,
    bootstrap_ci,
    close_time_of,
    split_chronologically,
    summarise,
    trades_from,
)
from kbot.research.search import Market


def market(ticker, outcome, quotes):
    """`quotes` is [(seconds_to_close, yes_ask)]; the rest is derived."""
    frames = []
    for sec, yes_ask in quotes:
        yes_bid = yes_ask - 10
        frames.append(
            (float(sec), (yes_bid + yes_ask) / 2, yes_ask,
             1000 - yes_bid, yes_bid, 1000 - yes_ask)
        )
    return Market(ticker=ticker, coin="BTC", outcome=outcome, frames=frames)


def test_a_trade_is_priced_before_the_decision_never_after():
    """The frame chosen must be at or before the entry time.

    A frame from 540s out is 60 seconds of hindsight. On a market that resolves
    96% of the time, that hindsight is most of the result.
    """
    m = market("KXBTC15M-26AUG171745-45", "yes",
               [(660, 900), (600, 910), (540, 999)])
    trades = trades_from([m], entry_at_s=600.0, size=1)
    asks = {t.side: t.ask_dc for t in trades}
    assert asks["yes"] == 910, "priced off the 600s frame, not the 540s one"


def test_the_nearest_eligible_frame_wins_not_the_earliest():
    m = market("KXBTC15M-26AUG171745-45", "yes",
               [(900, 500), (660, 900), (620, 930)])
    trades = trades_from([m], entry_at_s=600.0, size=1)
    assert {t.ask_dc for t in trades if t.side == "yes"} == {930}


def test_both_sides_are_scored_and_exactly_one_wins():
    m = market("KXBTC15M-26AUG171745-45", "yes", [(600, 900)])
    trades = trades_from([m], entry_at_s=600.0, size=1)
    assert len(trades) == 2
    assert sum(1 for t in trades if t.won) == 1


def test_a_win_pays_the_rest_of_the_dollar_less_the_entry_fee():
    m = market("KXBTC15M-26AUG171745-45", "yes", [(600, 900)])
    yes = next(t for t in trades_from([m], entry_at_s=600.0, size=10)
               if t.side == "yes")
    expected = ((1000 - 900) * 10 - fee_dc(10, 900)) / 1000
    assert yes.net == expected
    assert yes.net > 0


def test_a_loss_costs_the_whole_stake_plus_the_fee():
    """Settlement is free, so only the entry fee is charged -- but it is
    charged on a loser too, and forgetting that flatters every band."""
    m = market("KXBTC15M-26AUG171745-45", "no", [(600, 900)])
    yes = next(t for t in trades_from([m], entry_at_s=600.0, size=10)
               if t.side == "yes")
    assert yes.net == (-900 * 10 - fee_dc(10, 900)) / 1000
    assert yes.net < -9.0


def test_a_settled_quote_is_not_tradeable():
    """1000 deci-cents is the outcome, not an offer. Buying it books a
    guaranteed zero-cost win, which is how a fake edge gets in.

    The filter is per side: a YES ask at 1000 says nothing about the NO ask,
    so only the settled side is dropped.
    """
    m = market("KXBTC15M-26AUG171745-45", "yes", [(600, 1000)])
    sides = {t.side for t in trades_from([m], entry_at_s=600.0, size=1)}
    assert sides == {"no"}


def test_a_market_with_no_frame_before_entry_is_skipped():
    m = market("KXBTC15M-26AUG171745-45", "yes", [(300, 900)])
    assert trades_from([m], entry_at_s=600.0, size=1) == []


def test_bands_do_not_overlap_or_leave_gaps_at_the_edges():
    for (_, high), (low, _) in zip(DEFAULT_BANDS, DEFAULT_BANDS[1:]):
        assert high == low, "a contract priced between two bands is invisible"


def test_a_band_reports_what_the_price_implied():
    m = market("KXBTC15M-26AUG171745-45", "yes", [(600, 900)])
    band = summarise(trades_from([m], entry_at_s=600.0, size=1), 900, 980)
    assert band is not None
    assert band.n == 1
    assert band.implied == 0.9


def test_an_empty_band_is_absent_rather_than_zero():
    """Zero trades and a zero win rate must not read the same. An empty band
    reported as 0.0% is a claim about the market that nothing supports."""
    m = market("KXBTC15M-26AUG171745-45", "yes", [(600, 500)])
    assert summarise(trades_from([m], entry_at_s=600.0, size=1), 900, 980) is None


def test_a_single_trade_has_no_interval():
    m = market("KXBTC15M-26AUG171745-45", "yes", [(600, 900)])
    band = summarise(trades_from([m], entry_at_s=600.0, size=1), 900, 980)
    assert band.ci_low == float("-inf") and band.ci_high == float("inf")


def test_edge_is_signed_the_way_the_bias_is_described():
    """Longshots overpriced means a negative edge on the cheap band. If the
    sign is backwards, every conclusion drawn from this table is too."""
    losers = [market(f"KXBTC15M-26AUG17{i:02d}00-00", "no", [(600, 100)])
              for i in range(1, 21)]
    band = summarise(trades_from(losers, entry_at_s=600.0, size=1), 50, 150)
    assert band.win_rate == 0.0
    assert band.edge_pp < 0


def test_ticker_dates_parse_and_order():
    a = close_time_of("KXBTC15M-26AUG171745-45")
    b = close_time_of("KXBTC15M-26AUG171800-00")
    assert a is not None and b is not None and b > a
    assert b - a == 15 * 60


def test_an_unparseable_ticker_is_none_not_the_epoch():
    """Returning 0.0 would sort every unknown ticker to the front of a
    chronological split, which is worse than dropping it."""
    assert close_time_of("KXBTC-SOMETHING") is None
    assert close_time_of("nonsense") is None


def test_the_split_is_by_time_not_by_list_order():
    """Markets arrive newest-first from the API and are pooled across series,
    so list order is not time order."""
    ms = [
        market("KXBTC15M-26AUG171745-45", "yes", [(600, 900)]),
        market("KXBTC15M-26AUG110900-00", "yes", [(600, 900)]),
        market("KXETH15M-26AUG141200-00", "yes", [(600, 900)]),
        market("KXETH15M-26AUG151200-00", "yes", [(600, 900)]),
    ]
    early, late = split_chronologically(trades_from(ms, entry_at_s=600.0, size=1))
    assert all(e.at <= l.at for e in early for l in late)
    assert len(early) + len(late) == 8


def test_undated_trades_are_excluded_from_a_split():
    ms = [
        market("KXBTC15M-26AUG171745-45", "yes", [(600, 900)]),
        market("garbage", "yes", [(600, 900)]),
    ]
    early, late = split_chronologically(trades_from(ms, entry_at_s=600.0, size=1))
    assert len(early) + len(late) == 2


def test_the_bootstrap_interval_is_wide_when_losses_are_rare():
    """Nineteen small wins and one big loss is the shape of the band under
    test. A method that calls that significantly positive is the bug."""
    nets = [0.9] * 19 + [-9.3]
    lo, hi = bootstrap_ci(nets, resamples=2000)
    assert lo < 0 < hi, "an interval that excludes zero here is over-confident"


def test_the_bootstrap_narrows_as_losses_accumulate():
    few = bootstrap_ci([0.9] * 19 + [-9.3], resamples=2000)
    many = bootstrap_ci(([0.9] * 19 + [-9.3]) * 20, resamples=2000)
    assert (many[1] - many[0]) < (few[1] - few[0])


def test_the_bootstrap_is_deterministic():
    nets = [0.9] * 19 + [-9.3]
    assert bootstrap_ci(nets, resamples=500) == bootstrap_ci(nets, resamples=500)


def test_analyse_returns_bands_cheapest_first():
    ms = [market(f"KXBTC15M-26AUG17{i:02d}00-00", "yes", [(600, 100 + i * 80)])
          for i in range(1, 11)]
    bands = analyse(ms, entry_at_s=600.0, size=1)
    assert bands == sorted(bands, key=lambda b: b.low_dc)


def test_losses_and_wins_account_for_every_trade():
    ms = [market(f"KXBTC15M-26AUG17{i:02d}00-00",
                 "yes" if i % 2 else "no", [(600, 900)])
          for i in range(1, 11)]
    band = summarise(trades_from(ms, entry_at_s=600.0, size=1), 900, 980)
    assert band.wins + band.losses == band.n
