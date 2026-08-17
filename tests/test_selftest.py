"""On-demand checks against the live process.

The unit suite proves the code is right. It says nothing about whether *this
deployment* can reach Kalshi, whether its clock is close enough for a signature
to be accepted, or whether the disk it was pointed at is writable. Those fail
silently on a running system until an order is refused.

The property worth defending hardest: a check must do the real operation. One
that reads a setting and reports OK is worse than no check, because it turns
an unknown into a false reassurance.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from kbot.config import Settings
from kbot.safety import TradingMode
from kbot.webui.desk import Desk
from kbot.webui.selftest import (
    CHECK_TIMEOUT_S,
    Check,
    SelfTestReport,
    _check_credentials,
    _check_data_dir,
    _check_disk_space,
    _check_kill_switch,
    _check_order_path,
    _check_strategies,
    _timed,
    run_selftest,
)


def make_desk(tmp_path, *, mode=TradingMode.PAPER, sandbox=False):
    return Desk(
        Settings(
            telegram_token="", admin_ids=frozenset(),
            master_key=Fernet.generate_key().decode(),
            db_path=tmp_path / "data" / "d.sqlite3",
            demo=mode.uses_demo_host, mode=mode,
            series={"BTC": "KXBTC15M"}, spot_products={},
        ),
        sandbox=sandbox,
    )


@pytest.fixture()
def desk(tmp_path):
    return make_desk(tmp_path)


# ---------------- the harness ----------------


async def test_a_failing_check_becomes_a_result_not_an_exception():
    """A self-test that can itself raise reports nothing at the moment it is
    most needed."""

    async def explodes():
        raise RuntimeError("kaboom")

    check = await _timed("boom", explodes())
    assert check.ok is False
    assert "RuntimeError" in check.detail
    assert "kaboom" in check.detail


async def test_a_hanging_check_times_out():
    """A hung probe gives no verdict, which is the one thing the panel exists
    to provide."""

    async def hangs():
        await asyncio.sleep(CHECK_TIMEOUT_S + 10)

    from kbot.webui import selftest

    original = selftest.CHECK_TIMEOUT_S
    selftest.CHECK_TIMEOUT_S = 0.05
    try:
        check = await _timed("slow", hangs())
    finally:
        selftest.CHECK_TIMEOUT_S = original
    assert check.ok is False
    assert "timed out" in check.detail


async def test_a_check_records_how_long_it_took():
    async def slow():
        await asyncio.sleep(0.02)
        return True, "fine"

    check = await _timed("slow", slow())
    assert check.duration_ms >= 15


def test_the_timeout_is_short_enough_to_be_useful():
    assert 5 <= CHECK_TIMEOUT_S <= 30


# ---------------- pass, warn, fail ----------------


def test_an_advisory_failure_does_not_fail_the_run():
    """An unset optional credential should be visible without being alarming."""
    report = SelfTestReport(checks=[
        Check("a", ok=True, detail=""),
        Check("b", ok=False, detail="", advisory=True),
    ])
    assert report.ok is True
    assert report.warnings and not report.failures
    assert "advisory" in report.summary()


def test_a_real_failure_fails_the_run():
    report = SelfTestReport(checks=[
        Check("a", ok=True, detail=""),
        Check("b", ok=False, detail=""),
    ])
    assert report.ok is False
    assert "failed" in report.summary()


def test_all_passing_says_so_plainly():
    report = SelfTestReport(checks=[Check("a", ok=True, detail="")])
    assert report.ok is True
    assert "All 1 checks passed" in report.summary()


def test_the_status_words_are_the_three_the_ui_renders():
    assert Check("x", ok=True, detail="").status == "pass"
    assert Check("x", ok=False, detail="", advisory=True).status == "warn"
    assert Check("x", ok=False, detail="").status == "fail"


# ---------------- individual checks do the real thing ----------------


async def test_the_data_directory_check_actually_writes(tmp_path):
    """A directory that exists is not a directory that can be written to.
    Read-only volumes and full disks both look normal until the first write."""
    desk = make_desk(tmp_path)
    ok, detail = await _check_data_dir(desk)
    assert ok is True
    assert "writable" in detail
    # And it cleans up after itself.
    assert not (desk.settings.db_path.parent / ".selftest").exists()


async def test_an_unwritable_data_directory_fails(tmp_path):
    import os
    import stat

    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")

    desk = make_desk(tmp_path)
    target = desk.settings.db_path.parent
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        ok, detail = await _check_data_dir(desk)
    finally:
        target.chmod(0o755)
    assert ok is False
    assert "not writable" in detail


async def test_the_disk_check_reports_real_free_space(desk):
    ok, detail = await _check_disk_space(desk)
    assert ok is True
    assert "free" in detail


async def test_the_disk_check_works_before_the_directory_exists(tmp_path):
    """The state of every fresh deployment. Reporting "no disk" because a
    directory has not been created yet is a false alarm about the wrong
    thing."""
    desk = make_desk(tmp_path / "not" / "created" / "yet")
    assert not desk.settings.db_path.parent.exists()
    ok, detail = await _check_disk_space(desk)
    assert ok is True
    assert "free" in detail


async def test_the_strategy_check_loads_every_strategy():
    """Registered is not the same as constructible."""
    ok, detail = await _check_strategies()
    assert ok is True
    assert "loaded" in detail


async def test_the_kill_switch_check_reads_it(desk):
    ok, detail = await _check_kill_switch(desk)
    assert ok is True and "clear" in detail

    desk.kill.engage("testing", source="test")
    ok, detail = await _check_kill_switch(desk)
    assert ok is True, "reporting an engaged switch is not a check failure"
    assert "ENGAGED" in detail


async def test_the_credential_check_signs_rather_than_looking(desk):
    """A key that is present but unusable is the failure mode worth catching,
    and only signing catches it."""
    ok, detail = await _check_credentials(desk)
    assert ok is False
    assert "none connected" in detail

    class Broken:
        def headers(self, *a, **k):
            raise ValueError("corrupt key")

    desk._signer = Broken()
    ok, detail = await _check_credentials(desk)
    assert ok is False
    assert "cannot sign" in detail

    class Working:
        def headers(self, *a, **k):
            return {"KALSHI-ACCESS-SIGNATURE": "abc"}

    desk._signer = Working()
    ok, detail = await _check_credentials(desk)
    assert ok is True


# ---------------- the order path ----------------


async def test_the_order_path_check_never_places_an_order():
    import inspect

    src = inspect.getsource(_check_order_path)
    for forbidden in (".buy(", ".sell(", "create_order", "broker()"):
        assert forbidden not in src, forbidden


async def test_the_order_path_check_agrees_with_the_trading_gate(tmp_path):
    """It reports the same gate the trading path consults, so the answer here
    and the behaviour there cannot disagree."""
    desk = make_desk(tmp_path, mode=TradingMode.PRODUCTION_LIVE)
    desk._signer = object()
    ok, detail = await _check_order_path(desk)
    assert ok is False
    assert desk.blocked_reason() in detail


async def test_paper_reports_that_nothing_is_sent(desk):
    ok, detail = await _check_order_path(desk)
    assert ok is True
    assert "nothing sent" in detail.lower()


async def test_a_sandbox_reports_it_has_no_order_path(tmp_path):
    desk = make_desk(tmp_path, sandbox=True)
    ok, detail = await _check_order_path(desk)
    assert ok is True
    assert "sandbox" in detail.lower()


# ---------------- the whole run ----------------


async def test_local_checks_run_before_network_ones(desk, monkeypatch):
    """An obvious local fault must not be reported underneath a network
    timeout."""
    async def offline(*a, **k):
        raise OSError("no network in this test")

    monkeypatch.setattr(desk._http, "get", offline)
    monkeypatch.setattr(
        desk.discovery, "refresh", lambda *a, **k: offline()
    )

    report = await run_selftest(desk)
    names = [c.name for c in report.checks]
    assert names.index("strategies") < names.index("kalshi reachable")
    assert names.index("data directory") < names.index("market discovery")
    # The local ones still passed despite the network being gone.
    by_name = {c.name: c for c in report.checks}
    assert by_name["strategies"].ok is True
    assert by_name["data directory"].ok is True


async def test_a_network_outage_does_not_break_the_run(desk, monkeypatch):
    async def offline(*a, **k):
        raise OSError("unreachable")

    monkeypatch.setattr(desk._http, "get", offline)
    monkeypatch.setattr(desk.discovery, "refresh", lambda *a, **k: offline())

    report = await run_selftest(desk)
    assert report.ok is False
    assert any(c.name == "kalshi reachable" and not c.ok for c in report.checks)


async def test_the_report_serialises_for_the_ui(desk, monkeypatch):
    async def offline(*a, **k):
        raise OSError("unreachable")

    monkeypatch.setattr(desk._http, "get", offline)
    monkeypatch.setattr(desk.discovery, "refresh", lambda *a, **k: offline())

    payload = (await run_selftest(desk)).to_dict()
    assert set(payload) >= {"ok", "summary", "duration_ms", "checks"}
    for check in payload["checks"]:
        assert set(check) >= {"name", "status", "detail", "duration_ms"}
        assert check["status"] in {"pass", "warn", "fail"}


# ---------------- wiring ----------------


async def test_the_route_runs_it_and_audits_the_result(desk, monkeypatch):
    import json

    from kbot.webui.server import DeskServer

    async def offline(*a, **k):
        raise OSError("unreachable")

    monkeypatch.setattr(desk._http, "get", offline)
    monkeypatch.setattr(desk.discovery, "refresh", lambda *a, **k: offline())

    server = DeskServer(desk)
    raw = await server._route("POST", "/api/selftest", b"{}")
    payload = json.loads(raw.split(b"\r\n\r\n", 1)[1])
    assert "checks" in payload and "state" in payload

    entry = desk.audit[0]
    assert entry["action"] == "selftest"
    assert "summary" in entry["detail"]


def test_the_button_exists_in_the_page():
    from kbot.webui.server import PAGE

    text = PAGE.read_text()
    assert 'id="selftest-run"' in text
    assert "/api/selftest" in text


@pytest.mark.slow
async def test_against_the_real_exchange(desk):
    """The checks that only mean something against the live host."""
    report = await run_selftest(desk)
    by_name = {c.name: c for c in report.checks}
    assert by_name["kalshi reachable"].ok, by_name["kalshi reachable"].detail
    assert by_name["clock sync"].ok, by_name["clock sync"].detail
