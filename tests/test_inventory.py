"""What the recordings can support.

The rest of the research package refuses to answer on a thin sample. That
refusal is correct and it is also silent about distance -- you learn that you
cannot know something, not how far you are from knowing it. These tests are
about the arithmetic that turns the refusal into a number, and about the two
ways a sample can be inadequate: too few observations, and too few
*independent* ones.
"""

from __future__ import annotations

import gzip
import json

import pytest

from kbot.research.inventory import (
    ONE_SIDED_SHARE,
    WINDOWS_PER_COIN_PER_DAY,
    Inventory,
    format_inventory,
    take_inventory,
)
from kbot.research.signals import MIN_WINDOWS


def write_day(directory, day: str, records: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with gzip.open(directory / f"{day}.jsonl.gz", "wt") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def book(ticker: str, coin: str, t: float) -> dict:
    return {
        "type": "book", "t": t, "ticker": ticker, "coin": coin,
        "o": t - 100, "c": t + 800,
        "yes": [[500, 10]], "no": [[490, 10]],
    }


def settle(ticker: str, result: str) -> dict:
    return {"type": "settle", "ticker": ticker, "result": result}


# ---------------- counting ----------------


def test_an_empty_directory_counts_nothing(tmp_path):
    inv = take_inventory(tmp_path / "nope")
    assert inv.settled == 0
    assert inv.windows == 0
    assert inv.days == []


def test_it_counts_windows_snapshots_and_settlements(tmp_path):
    d = tmp_path / "rec"
    write_day(d, "2026-01-01", [
        book("KXBTC-A", "BTC", 100.0), book("KXBTC-A", "BTC", 101.0),
        settle("KXBTC-A", "yes"),
        book("KXETH-A", "ETH", 100.0),
        settle("KXETH-A", "no"),
        book("KXSOL-A", "SOL", 100.0),  # no settlement -- a window, not settled
    ])
    inv = take_inventory(d)
    assert inv.windows == 3
    assert inv.snapshots == 4
    assert inv.settled == 2
    assert inv.yes == 1 and inv.no == 1
    assert sorted(inv.coins) == ["BTC", "ETH", "SOL"]
    assert inv.coins["BTC"].snapshots == 2


def test_an_unsettled_window_is_not_counted_as_settled(tmp_path):
    """A window with no outcome cannot score a prediction, so counting it
    toward the threshold would overstate how much is known."""
    d = tmp_path / "rec"
    write_day(d, "2026-01-01", [book("KXBTC-A", "BTC", 100.0)])
    inv = take_inventory(d)
    assert inv.windows == 1
    assert inv.settled == 0


def test_days_are_counted_from_the_files(tmp_path):
    d = tmp_path / "rec"
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        write_day(d, day, [book("KXBTC-A", "BTC", 100.0), settle("KXBTC-A", "yes")])
    inv = take_inventory(d)
    assert len(inv.days) == 3
    assert inv.days[0] == "2026-01-01" and inv.days[-1] == "2026-01-03"


# ---------------- the threshold ----------------


def make(settled: int, yes: int, days: int = 1, coins: int = 1) -> Inventory:
    from kbot.research.inventory import CoinInventory

    inv = Inventory(days=[f"d{i}" for i in range(days)])
    per = settled // coins if coins else 0
    remaining_yes = yes
    for i in range(coins):
        n = per if i < coins - 1 else settled - per * (coins - 1)
        y = min(n, remaining_yes)
        remaining_yes -= y
        inv.coins[f"C{i}"] = CoinInventory(
            coin=f"C{i}", windows=n, settled=n, yes=y, no=n - y
        )
    return inv


def test_below_the_threshold_no_verdict_is_claimed():
    inv = make(MIN_WINDOWS - 1, yes=90)
    assert inv.enough_for_a_verdict is False
    assert inv.shortfall == 1
    assert "Below the" in inv.verdict()


def test_at_the_threshold_a_verdict_becomes_possible():
    inv = make(MIN_WINDOWS, yes=MIN_WINDOWS // 2)
    assert inv.enough_for_a_verdict is True
    assert inv.shortfall == 0
    assert "Enough" in inv.verdict()


def test_the_threshold_matches_the_tool_that_enforces_it():
    """Restating it here rather than importing would let the two drift, and
    the desk would report progress toward a bar the analysis does not use."""
    from kbot.research import inventory

    assert inventory.MIN_WINDOWS is MIN_WINDOWS

    from kbot.webui.desk import MIN_RESEARCH_WINDOWS

    assert MIN_RESEARCH_WINDOWS == MIN_WINDOWS


def test_progress_is_capped_at_one():
    assert make(MIN_WINDOWS * 3, yes=MIN_WINDOWS).progress == 1.0


def test_no_data_says_so_plainly():
    assert "No settled markets" in Inventory().verdict()


# ---------------- independence, not just count ----------------


def test_a_one_sided_sample_is_refused_even_when_large():
    """The failure that looks like success: a full sample from one sustained
    trend produces a confident edge that is really one move counted again and
    again."""
    inv = make(MIN_WINDOWS * 2, yes=int(MIN_WINDOWS * 2 * 0.95))
    assert inv.enough_for_a_verdict is True
    assert inv.is_one_sided is True
    assert "same way" in inv.verdict()


def test_a_balanced_sample_passes():
    inv = make(MIN_WINDOWS, yes=MIN_WINDOWS // 2)
    assert inv.is_one_sided is False
    assert "reasonably balanced" in inv.verdict()


def test_the_one_sided_boundary_matches_calibrate():
    from kbot.research.calibrate import ONE_SIDED_SHARE as CALIBRATE_SHARE

    assert ONE_SIDED_SHARE == CALIBRATE_SHARE


def test_an_empty_sample_is_not_called_one_sided():
    assert Inventory().is_one_sided is False


# ---------------- rates ----------------


def test_the_ideal_rate_assumes_every_window_is_captured():
    inv = make(0, yes=0, days=1, coins=2)
    inv.coins["C0"].settled = 0
    expected = MIN_WINDOWS / (2 * WINDOWS_PER_COIN_PER_DAY)
    assert inv.days_remaining() == pytest.approx(expected)


def test_the_observed_rate_reflects_what_actually_happened():
    """The number that matters for planning: it absorbs every hour the
    recorder was not running, which in practice is most of them."""
    inv = make(20, yes=10, days=2)
    assert inv.observed_per_day == pytest.approx(10.0)
    assert inv.days_remaining_observed() == pytest.approx((MIN_WINDOWS - 20) / 10.0)


def test_the_observed_rate_is_the_one_reported():
    """A forecast built on an ideal the recorder is nowhere near meeting is a
    forecast that will be wrong by an order of magnitude."""
    inv = make(18, yes=2, days=2, coins=9)
    verdict = inv.verdict()
    observed = inv.days_remaining_observed()
    assert f"{observed:.0f}" in verdict
    assert "if the recorder ran continuously" in verdict


def test_capture_efficiency_exposes_a_recorder_that_is_not_running():
    """A different problem from needing more days, fixed a different way."""
    inv = make(18, yes=2, days=2, coins=9)
    efficiency = inv.capture_efficiency
    assert efficiency is not None
    assert efficiency < 0.05
    assert format_inventory(inv).count("capture rate") == 1


def test_a_healthy_capture_rate_is_not_warned_about():
    inv = make(2 * WINDOWS_PER_COIN_PER_DAY, yes=WINDOWS_PER_COIN_PER_DAY,
               days=2, coins=1)
    assert inv.capture_efficiency == pytest.approx(1.0)
    assert "capture rate" not in format_inventory(inv)


def test_rates_are_zero_once_the_threshold_is_met():
    inv = make(MIN_WINDOWS, yes=MIN_WINDOWS // 2, days=5)
    assert inv.days_remaining() == 0.0
    assert inv.days_remaining_observed() == 0.0


def test_rates_are_unknown_rather_than_wrong_with_no_history():
    assert Inventory().days_remaining_observed() is None
    assert Inventory().days_remaining() is None


# ---------------- rendering ----------------


def test_the_report_shows_the_shortfall_and_every_coin(tmp_path):
    d = tmp_path / "rec"
    write_day(d, "2026-01-01", [
        book("KXBTC-A", "BTC", 100.0), settle("KXBTC-A", "yes"),
        book("KXETH-A", "ETH", 100.0), settle("KXETH-A", "no"),
    ])
    text = format_inventory(take_inventory(d))
    assert f"2/{MIN_WINDOWS}" in text
    assert "BTC" in text and "ETH" in text
    assert "toward a verdict" in text


def test_the_real_recordings_are_readable_if_present():
    """Guards the actual on-disk format against a schema change in the
    recorder -- a synthetic fixture would not notice one."""
    from pathlib import Path

    directory = Path("data/recordings")
    if not directory.exists() or not list(directory.glob("*.jsonl.gz")):
        pytest.skip("no recordings checked in")
    inv = take_inventory(directory)
    assert inv.snapshots > 0
    assert inv.windows >= inv.settled
    assert inv.yes + inv.no == inv.settled
