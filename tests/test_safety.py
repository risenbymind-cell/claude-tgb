"""Trading mode and the kill switch.

The property under test throughout is that no *single* thing -- one variable,
one database row, one button -- can move the system from simulated to real
orders. Every test here is a way that could have happened before.
"""

from __future__ import annotations

import pytest

from kbot.safety import (
    PRODUCTION_ACK,
    KillSwitch,
    ModeError,
    TradingMode,
    resolve_mode,
)

# ---------------- mode resolution ----------------


def test_no_configuration_at_all_is_paper():
    assert resolve_mode(None) is TradingMode.PAPER
    assert resolve_mode("") is TradingMode.PAPER


def test_paper_does_not_place_orders():
    mode = TradingMode.PAPER
    assert not mode.places_real_orders
    assert not mode.risks_real_money


def test_demo_live_places_orders_but_risks_no_money():
    """Demo orders are real API calls with real rate limits, so everything
    guarding submission must apply -- but nothing is at stake."""
    mode = TradingMode.DEMO_LIVE
    assert mode.places_real_orders
    assert not mode.risks_real_money
    assert mode.uses_demo_host


def test_paper_reads_the_production_book():
    """Simulating against the demo book would simulate against liquidity that
    is not there, which makes every paper result meaningless."""
    assert not TradingMode.PAPER.uses_demo_host


def test_production_needs_a_second_independent_acknowledgement():
    with pytest.raises(ModeError, match="ALLOW_PRODUCTION_ORDERS"):
        resolve_mode("production-live")


@pytest.mark.parametrize("ack", ["true", "1", "yes", "YES", "", "  ", "i understand"])
def test_a_truthy_value_is_not_an_acknowledgement(ack):
    """The acknowledgement is a sentence, not a boolean, precisely so that
    copying a boolean from another variable cannot satisfy it."""
    with pytest.raises(ModeError):
        resolve_mode("production-live", ack=ack)


def test_production_is_reachable_with_the_exact_acknowledgement():
    assert (
        resolve_mode("production-live", ack=PRODUCTION_ACK)
        is TradingMode.PRODUCTION_LIVE
    )


def test_a_typo_is_rejected_rather_than_guessed():
    """`lve` must not fall through to anything. Silently resolving a typo to a
    default is fine when the default is paper and catastrophic if it is not,
    so the rule is simply to refuse."""
    for bad in ("lve", "prod-live", "real", "yes", "1"):
        with pytest.raises(ModeError, match="TRADING_MODE"):
            resolve_mode(bad)


def test_a_contradictory_legacy_flag_is_refused():
    """KALSHI_DEMO used to pick the host while a per-user boolean picked
    whether orders were real. Those could disagree; now they cannot, and a
    configuration that still tries is an error rather than a silent winner."""
    with pytest.raises(ModeError, match="contradicts"):
        resolve_mode("demo-live", legacy_demo=False)
    with pytest.raises(ModeError, match="contradicts"):
        resolve_mode("production-live", ack=PRODUCTION_ACK, legacy_demo=True)


def test_legacy_demo_is_consistent_with_paper():
    """Paper deliberately reads the production book, so KALSHI_DEMO=false
    alongside TRADING_MODE=paper is not a conflict."""
    assert resolve_mode("paper", legacy_demo=False) is TradingMode.PAPER
    assert resolve_mode("paper", legacy_demo=True) is TradingMode.PAPER


def test_every_mode_has_a_label_naming_the_stakes():
    """Someone reading the dashboard has to be able to tell, from the label
    alone, whose money is at risk and whether the P/L means anything."""
    for mode in TradingMode:
        assert mode.label

    assert "your own money" in TradingMode.PRODUCTION_LIVE.label

    # Demo spends demo funds -- so the label has to say that it is not real,
    # and equally that its P/L is not evidence, since the demo book is thin.
    demo = TradingMode.DEMO_LIVE.label
    assert "demo funds" in demo
    assert "not" in demo and "evidence" in demo

    # Paper trades against production liquidity; saying so is what stops
    # someone assuming it is the same thing as demo.
    assert "production book" in TradingMode.PAPER.label


# ---------------- kill switch ----------------


@pytest.fixture()
def kill(tmp_path, monkeypatch):
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    return KillSwitch(path=tmp_path / "KILL")


def test_a_fresh_switch_is_clear(kill):
    assert not kill.engaged
    assert kill.state().describe() == "clear"


def test_the_environment_variable_engages_it(kill, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH", "1")
    state = kill.state()
    assert state.engaged and state.source == "environment"


def test_a_file_engages_it(kill):
    kill.path.parent.mkdir(parents=True, exist_ok=True)
    kill.path.write_text("stopped by ops")
    state = kill.state()
    assert state.engaged
    assert state.source == "file"
    assert "stopped by ops" in (state.reason or "")


def test_a_command_engages_it_and_survives_a_restart(tmp_path, monkeypatch):
    """A kill switch that a restart clears is not a kill switch -- a crash
    loop would resume trading on its own."""
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    path = tmp_path / "KILL"
    KillSwitch(path=path).engage("daily loss limit", source="circuit-breaker")

    reborn = KillSwitch(path=path)
    state = reborn.state()
    assert state.engaged
    assert "daily loss limit" in (state.reason or "")


def test_release_clears_the_file_and_the_runtime_flag(kill):
    kill.engage("test")
    assert kill.engaged
    kill.release()
    assert not kill.engaged
    assert not kill.path.exists()


def test_release_cannot_clear_the_environment_switch(kill, monkeypatch):
    """A process cannot un-set its own configuration in a way that survives,
    so reporting the switch as released would be a lie that re-engages on the
    next read."""
    monkeypatch.setenv("KILL_SWITCH", "true")
    kill.release()
    assert kill.engaged


def test_the_state_is_never_cached(kill):
    """Read fresh every time. A cached kill switch keeps trading after
    someone has pulled it, which is the only failure that matters."""
    assert not kill.engaged
    kill.path.parent.mkdir(parents=True, exist_ok=True)
    kill.path.write_text("pulled externally")
    assert kill.engaged  # no re-construction, no explicit refresh


def test_an_unreadable_kill_file_is_treated_as_engaged(kill, monkeypatch):
    """The failure direction here has to be toward not trading."""
    kill.path.parent.mkdir(parents=True, exist_ok=True)
    kill.path.write_text("x")

    def boom(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(type(kill.path), "read_text", boom)
    assert kill.engaged


# ---------------- clock drift ----------------


class _Resp:
    def __init__(self, date: str | None):
        self.headers = {"date": date} if date else {}


class _Client:
    def __init__(self, resp=None, exc=None):
        self._resp, self._exc = resp, exc

    async def get(self, url):
        if self._exc:
            raise self._exc
        return self._resp

    async def aclose(self):
        pass


def _http_date(offset_s: float) -> str:
    import email.utils
    import time

    return email.utils.formatdate(time.time() + offset_s, usegmt=True)


async def test_a_synchronised_clock_passes():
    from kbot.safety import measure_clock_drift

    check = await measure_clock_drift("http://x", client=_Client(_Resp(_http_date(0))))
    assert check.ok
    assert check.drift_s is not None and abs(check.drift_s) < 2


async def test_a_drifted_clock_is_caught():
    """Signatures carry a timestamp, so drift fails as an authentication error
    mid-session -- which reads like a bad key, and sends you looking in the
    wrong place entirely."""
    from kbot.safety import measure_clock_drift

    # Server 60s behind us.
    check = await measure_clock_drift(
        "http://x", client=_Client(_Resp(_http_date(-60)))
    )
    assert not check.ok
    assert check.drift_s is not None and check.drift_s > 50


async def test_an_unreachable_host_is_not_reported_as_drift():
    """Absence of evidence is not drift; saying so would send someone to
    change a clock that was fine."""
    from kbot.safety import measure_clock_drift

    check = await measure_clock_drift("http://x", client=_Client(exc=OSError("down")))
    assert check.ok
    assert check.drift_s is None


async def test_a_missing_or_unparseable_date_header_is_not_drift():
    from kbot.safety import measure_clock_drift

    assert (await measure_clock_drift("http://x", client=_Client(_Resp(None)))).ok
    assert (
        await measure_clock_drift("http://x", client=_Client(_Resp("not a date")))
    ).ok
