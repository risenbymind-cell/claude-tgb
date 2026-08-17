"""Strategy search, and the null pass that makes it mean anything.

A search over thousands of plans always returns a winner. The only question
is whether that winner beats what the same search extracts from noise, and
these tests fix the machinery that answers it.
"""

from __future__ import annotations

import random

import pytest

from kbot.research.search import (
    Market,
    Plan,
    all_plans,
    evaluate,
    run_grid,
    shuffled,
    _classify,
)


def frames(path, seconds_start=900.0, step=5.0):
    """(seconds_to_close, mid, yes_ask, no_ask, yes_bid, no_bid)."""
    out = []
    for i, mid in enumerate(path):
        sec = max(0.0, seconds_start - i * step)
        mid = int(mid)
        out.append((sec, float(mid), mid + 10, 1000 - mid + 10, mid - 10, 1000 - mid - 10))
    return out


def market(path, outcome="yes", regime="any", **kw):
    m = Market(ticker="T", coin="BTC", outcome=outcome, frames=frames(path), regime=regime)
    m.position_at = kw.get("position_at", {})
    return m


def test_the_grid_is_actually_thousands_of_plans():
    plans = all_plans()
    assert len(plans) >= 1000
    assert len(set(plans)) == len(plans), "duplicate plans inflate the search"


def test_shuffling_moves_only_the_label():
    """Every price path, book and regime must survive intact -- otherwise the
    null pass is measuring a different market, not the same market with the
    answer removed."""
    ms = [market([500 + i] * 80, outcome="yes" if i % 2 else "no") for i in range(8)]
    out = shuffled(ms, random.Random(1))

    assert [m.ticker for m in out] == [m.ticker for m in ms]
    assert [m.frames for m in out] == [m.frames for m in ms]
    assert [m.regime for m in out] == [m.regime for m in ms]
    assert sorted(m.outcome for m in out) == sorted(m.outcome for m in ms), (
        "shuffling must permute outcomes, not invent them"
    )


def test_shuffling_is_a_permutation_not_a_resample():
    """Resampling would change the base rate, and a changed base rate is its
    own explanation for a difference in results."""
    ms = [market([500] * 80, outcome="no") for _ in range(9)]
    ms += [market([500] * 80, outcome="yes") for _ in range(3)]
    out = shuffled(ms, random.Random(7))
    assert sum(1 for m in out if m.outcome == "yes") == 3


def test_regimes_are_classified_from_the_path_alone():
    assert _classify([500.0] * 50) == "quiet"
    assert _classify([500.0 + i * 6 for i in range(50)]) == "volatile"
    trend = [500.0 + i * 2 for i in range(50)]          # range ~100, net ~100
    assert _classify(trend) == "trending"
    # An odd length so the path ends where it began -- with 50 points it ends
    # on the high and reads as a trend, which is correct and not what is
    # being tested here.
    chop = [500.0 + (60 if i % 2 else 0) for i in range(51)]
    assert _classify(chop) == "ranging"


def test_a_plan_below_the_trade_floor_is_dropped():
    """One lucky trade is not a strategy, and with thousands of plans there
    are always plenty of them."""
    ms = [market([500] * 80, position_at={600.0: 0.95})]
    plans = all_plans()[:50]
    assert run_grid(ms, plans, min_trades=5) == []


def test_a_plan_that_never_qualifies_takes_no_trades():
    plan = Plan(600.0, 0.85, True, 15, None, "any")
    ms = [market([500] * 80, position_at={600.0: 0.5})]   # dead centre
    assert evaluate(plan, ms).trades == 0


def test_a_regime_filter_only_sees_its_own_regime():
    plan = Plan(600.0, 0.55, True, 15, None, "trending")
    trending = market([500] * 80, regime="trending", position_at={600.0: 0.95})
    volatile = market([500] * 80, regime="volatile", position_at={600.0: 0.95})
    assert evaluate(plan, [trending]).trades == 1
    assert evaluate(plan, [volatile]).trades == 0


def test_fee_is_charged_on_both_legs_of_a_completed_trade():
    """A search that forgets a fee finds edges that do not survive contact
    with the exchange."""
    plan = Plan(600.0, 0.55, False, 5, None, "any")
    # Flat through the entry at T-600s, then a rise that clears the target,
    # so the exit is a fill rather than a settlement.
    path = [500] * 61 + list(range(500, 700, 5))
    m = market(path, outcome="yes", position_at={600.0: 0.95})
    r = evaluate(plan, [m])
    assert r.trades == 1

    # Entry lifts the 510 ask, target is 560, so gross is 50 dc x 10 = 500.
    # Both legs carry a fee, so net must fall short of that.
    assert 0 < r.net_dc < 500, f"fees look wrong (net {r.net_dc})"


def test_win_rate_is_computed_from_net_not_gross():
    """A trade that clears its target and still loses to fees is not a win,
    and counting it as one is how a 90% win rate hides a loss."""
    plan = Plan(600.0, 0.55, False, 1, None, "any")
    rising = list(range(500, 560, 2))
    m = market(rising + [560] * 70, outcome="yes", position_at={600.0: 0.95})
    r = evaluate(plan, [m])
    if r.trades:
        assert (r.wins > 0) == (r.net_dc > 0)


# ---------------- the command's verdict ----------------
#
# The grid will always produce winners on a small sample. Everything here is
# about making that impossible to mistake for a finding -- including by a
# shell script, which reads exit codes rather than prose.


def _run_search(tmp_path, **kwargs):
    """Run cmd_search over a synthetic recording directory."""
    import argparse
    import gzip
    import json

    from kbot.research.__main__ import cmd_search

    directory = tmp_path / "rec"
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    # Enough markets and samples for the grid to find trades in.
    for m in range(kwargs.pop("markets", 12)):
        ticker = f"KX-{m}"
        base = m * 1000.0
        for i in range(90):
            records.append({
                "type": "book", "t": base + i * 10, "ticker": ticker,
                "coin": "BTC", "o": base, "c": base + 900,
                "yes": [[400 + (i * 5) % 200, 50]],
                "no": [[600 - (i * 5) % 200, 50]],
            })
        records.append({
            "type": "settle", "ticker": ticker,
            "result": "yes" if m % 2 else "no",
        })
    with gzip.open(directory / "2026-01-01.jsonl.gz", "wt") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")

    args = argparse.Namespace(
        dir=directory, min_trades=kwargs.pop("min_trades", 3),
        trials=kwargs.pop("trials", 3), seed=1, **kwargs
    )
    return cmd_search(args)


def test_no_data_exits_nonzero(tmp_path, capsys):
    import argparse

    from kbot.research.__main__ import cmd_search

    empty = tmp_path / "empty"
    empty.mkdir()
    code = cmd_search(argparse.Namespace(
        dir=empty, min_trades=3, trials=2, seed=1
    ))
    assert code != 0


def test_a_small_sample_is_warned_about_before_the_results(tmp_path, capsys):
    """A reader who scans the top and stops must not see a 100% win rate
    presented as a finding with the caveat below the fold."""
    _run_search(tmp_path)
    out = capsys.readouterr().out
    assert "below the" in out
    # The warning has to precede the results table.
    assert out.index("below the") < out.index("plans took at least")


def test_failing_the_null_test_exits_nonzero(tmp_path, capsys):
    """`search && deploy` must not proceed on noise. A command that prints
    "worth nothing" and then reports success to the shell invites exactly the
    automation this pass exists to prevent."""
    code = _run_search(tmp_path)
    out = capsys.readouterr().out
    if "VERDICT: nothing here is distinguishable" in out:
        assert code == 2
    else:
        # Passed the null on a small sample, which is itself a coin flip.
        assert code == 2
        assert "coin flip" in out


def test_a_small_sample_never_reports_success(tmp_path, capsys):
    """Whichever way the null test lands, a sample below the threshold cannot
    return 0 -- passing a null test on 12 markets is not evidence."""
    assert _run_search(tmp_path, markets=12) != 0
