"""Per-stage latency.

Averages hide the thing that matters. A loop that evaluates in 2 ms typically
and 900 ms at the 99th percentile will miss precisely the entries worth having,
because slow passes are not randomly distributed -- they cluster when the book
is moving and everything is doing more work at once. So these tests are mostly
about the tail, and about not losing samples from the paths where the tail
lives.
"""

from __future__ import annotations

import time

import pytest

from kbot.webui.latency import STAGES, WINDOW, Latency, percentile


# ---------------- percentiles ----------------


def test_a_percentile_of_nothing_is_unknown_not_zero():
    """Zero would read as "instant", which is the opposite of the truth."""
    assert percentile([], 0.5) is None


def test_a_single_sample_is_its_own_percentile():
    assert percentile([7.0], 0.5) == 7.0
    assert percentile([7.0], 0.99) == 7.0


def test_percentiles_of_a_known_range():
    values = list(range(1, 101))  # 1..100
    assert percentile(values, 0.0) == 1
    assert percentile(values, 0.5) == pytest.approx(50.5)
    assert percentile(values, 1.0) == 100


def test_percentiles_do_not_depend_on_input_order():
    import random

    values = [float(v) for v in range(200)]
    shuffled = values[:]
    random.shuffle(shuffled)
    for q in (0.5, 0.95, 0.99):
        assert percentile(shuffled, q) == percentile(values, q)


def test_the_tail_tracks_the_outliers_not_the_bulk():
    """The property the whole module exists for: slow samples must move the
    tail while leaving the median alone.

    A single outlier in a hundred lands exactly on the p99 interpolation
    boundary and is therefore blended, which is correct -- one sample in a
    hundred is not yet evidence of a 99th-percentile problem. Two make it
    unambiguous.
    """
    one = [1.0] * 99 + [1000.0]
    assert percentile(one, 0.50) == pytest.approx(1.0)
    assert percentile(one, 0.99) > 10 * percentile(one, 0.50)

    several = [1.0] * 95 + [1000.0] * 5
    assert percentile(several, 0.50) == pytest.approx(1.0)
    assert percentile(several, 0.99) == pytest.approx(1000.0)


# ---------------- recording ----------------


def test_nothing_recorded_reports_nothing():
    lat = Latency()
    assert lat.stats("order") is None
    assert lat.snapshot() == []


def test_samples_are_reported_in_milliseconds():
    """Seconds in, milliseconds out -- one conversion, at the boundary."""
    lat = Latency()
    lat.record("order", 0.25)
    assert lat.stats("order")["p50"] == pytest.approx(250.0)


def test_it_counts_what_it_measured():
    lat = Latency()
    for _ in range(7):
        lat.record("feed", 0.01)
    assert lat.stats("feed")["n"] == 7


def test_stages_are_independent():
    lat = Latency()
    lat.record("order", 1.0)
    lat.record("feed", 0.001)
    assert lat.stats("order")["p50"] > lat.stats("feed")["p50"] * 100


def test_the_window_is_bounded():
    """Percentiles over an entire uptime hide a regression that started an
    hour ago, and unbounded samples are a slow leak besides."""
    lat = Latency(window=10)
    for i in range(100):
        lat.record("pass", i / 1000)
    assert lat.stats("pass")["n"] == 10


def test_old_samples_fall_out_of_the_window():
    lat = Latency(window=5)
    for _ in range(5):
        lat.record("pass", 10.0)      # slow era
    for _ in range(5):
        lat.record("pass", 0.001)     # fast era
    assert lat.stats("pass")["p50"] == pytest.approx(1.0)


def test_the_default_window_covers_a_meaningful_span():
    assert WINDOW >= 1000


def test_clearing_forgets_everything():
    lat = Latency()
    lat.record("order", 1.0)
    lat.clear()
    assert lat.snapshot() == []


# ---------------- the context manager ----------------


def test_measure_times_the_block():
    lat = Latency()
    with lat.measure("strategy"):
        time.sleep(0.02)
    assert lat.stats("strategy")["p50"] >= 15


def test_a_raising_block_is_still_recorded():
    """The failure path is the one that matters: a stage slow *because* it is
    timing out would otherwise contribute no samples, and the percentiles
    would look healthiest exactly when the system is worst."""
    lat = Latency()
    with pytest.raises(ValueError):
        with lat.measure("order"):
            raise ValueError("exchange refused")
    assert lat.stats("order")["n"] == 1


def test_measure_does_not_swallow_the_exception():
    lat = Latency()
    with pytest.raises(RuntimeError):
        with lat.measure("order"):
            raise RuntimeError("boom")


# ---------------- reporting ----------------


def test_the_snapshot_reads_as_a_pipeline():
    """Known stages in the order work happens, so the table is a pipeline
    rather than an alphabetised list."""
    lat = Latency()
    for stage in reversed(STAGES):
        lat.record(stage, 0.001)
    assert [row["stage"] for row in lat.snapshot()] == list(STAGES)


def test_unknown_stages_still_appear():
    """The stage list documents intent; it must not silently drop data."""
    lat = Latency()
    lat.record("order", 0.001)
    lat.record("zzz-custom", 0.001)
    assert {r["stage"] for r in lat.snapshot()} == {"order", "zzz-custom"}


def test_every_row_carries_the_percentiles_the_ui_shows():
    lat = Latency()
    lat.record("order", 0.01)
    row = lat.stats("order")
    for key in ("stage", "n", "p50", "p95", "p99", "max"):
        assert key in row, key


def test_max_is_the_worst_seen_not_a_percentile():
    lat = Latency()
    for _ in range(999):
        lat.record("pass", 0.001)
    lat.record("pass", 5.0)
    row = lat.stats("pass")
    assert row["max"] == pytest.approx(5000.0)
    assert row["p99"] < row["max"]


# ---------------- wired into the desk ----------------


@pytest.fixture()
def desk(tmp_path):
    from cryptography.fernet import Fernet

    from kbot.config import Settings
    from kbot.safety import TradingMode
    from kbot.webui.desk import Desk

    return Desk(Settings(
        telegram_token="", admin_ids=frozenset(),
        master_key=Fernet.generate_key().decode(),
        db_path=tmp_path / "d.sqlite3", demo=False,
        mode=TradingMode.PAPER, series={"BTC": "KXBTC15M"}, spot_products={},
    ))


def test_the_desk_exposes_latency(desk):
    assert "latency" in desk.snapshot()
    assert desk.snapshot()["latency"] == []


def test_the_desk_records_the_stages_it_runs(desk):
    desk.latency.record("strategy", 0.001)
    desk.latency.record("order", 0.5)
    stages = {row["stage"] for row in desk.snapshot()["latency"]}
    assert {"strategy", "order"} <= stages


def test_the_order_stage_is_measured_around_submission(desk, monkeypatch):
    """The round trip that sits between a decision and a fill -- the one
    number worth alarming on."""
    import inspect

    from kbot.webui import desk as desk_mod

    src = inspect.getsource(desk_mod.Desk._maybe_open)
    assert 'measure("order")' in src
    close_src = inspect.getsource(desk_mod.Desk._close)
    assert 'measure("order")' in close_src


def test_the_page_renders_the_latency_table():
    from kbot.webui.server import PAGE

    text = PAGE.read_text()
    assert 'id="latency"' in text
    assert "s.latency" in text
    for column in ("P50", "P95", "P99"):
        assert f">{column}<" in text, column
