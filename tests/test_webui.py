"""The local desk.

The desk is a control surface for something that spends money, so the tests
that matter are the refusals: it must not trade while killed, must not accept
a strategy it does not have, and must not bind somewhere reachable from
outside without saying so.
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from kbot.config import Settings
from kbot.safety import TradingMode
from kbot.webui.desk import Desk
from kbot.webui.server import DeskServer


@pytest.fixture()
def desk(tmp_path):
    settings = Settings(
        telegram_token="",
        admin_ids=frozenset(),
        master_key=Fernet.generate_key().decode(),
        db_path=tmp_path / "desk.sqlite3",
        demo=False,
        mode=TradingMode.PAPER,
        series={"BTC": "KXBTC15M"},
        spot_products={},
    )
    return Desk(settings)


async def post(server, path, payload):
    return await server._route("POST", path, json.dumps(payload).encode())


def body(raw: bytes) -> dict:
    return json.loads(raw.split(b"\r\n\r\n", 1)[1])


def status(raw: bytes) -> str:
    return raw.split(b"\r\n", 1)[0].decode().split(" ", 1)[1]


async def test_the_state_endpoint_describes_the_mode_and_the_switch(desk):
    server = DeskServer(desk)
    snap = body(await server._route("GET", "/api/state", b""))
    assert snap["mode"] == "paper"
    assert snap["places_real_orders"] is False
    assert snap["kill"]["engaged"] is False


async def test_trading_cannot_be_started_while_killed(desk):
    """The single most important refusal in the whole surface."""
    server = DeskServer(desk)
    desk.kill.engage("test", source="test")

    raw = await post(server, "/api/trading", {"enabled": True})
    assert status(raw).startswith("409")
    assert desk.enabled is False


async def test_killing_also_stops_trading_immediately(desk):
    server = DeskServer(desk)
    desk.enabled = True
    await post(server, "/api/kill", {"reason": "stop"})
    assert desk.enabled is False
    assert desk.kill.engaged


async def test_resume_clears_the_switch(desk):
    server = DeskServer(desk)
    await post(server, "/api/kill", {"reason": "x"})
    await post(server, "/api/resume", {})
    assert not desk.kill.engaged


async def test_an_unknown_strategy_is_refused(desk):
    """Falling back to a default here would trade something the operator did
    not select and did not see."""
    server = DeskServer(desk)
    raw = await post(server, "/api/strategy", {"strategy": "does-not-exist"})
    assert status(raw).startswith("400")
    assert desk.strategy == "reversion"


async def test_a_known_strategy_is_accepted(desk):
    server = DeskServer(desk)
    await post(server, "/api/strategy", {"strategy": "drift"})
    assert desk.strategy == "drift"


async def test_settings_are_clamped(desk):
    """A size typed with an extra zero must not become the position."""
    server = DeskServer(desk)
    await post(server, "/api/settings", {"size": 99999, "target_c": 9999})
    assert desk.size <= 500
    assert desk.target_c <= 90
    await post(server, "/api/settings", {"size": -5, "target_c": 0})
    assert desk.size >= 1
    assert desk.target_c >= 1


async def test_malformed_json_is_rejected_not_crashed(desk):
    server = DeskServer(desk)
    raw = await server._route("POST", "/api/trading", b"{not json")
    assert status(raw).startswith("400")


async def test_unknown_routes_404(desk):
    server = DeskServer(desk)
    assert status(await server._route("GET", "/api/nope", b"")).startswith("404")


async def test_it_binds_to_localhost_by_default(desk):
    """This port holds decrypted credentials and can place orders."""
    assert DeskServer(desk).host == "127.0.0.1"


async def test_responses_are_not_cacheable_or_frameable(desk):
    server = DeskServer(desk)
    raw = await server._route("GET", "/api/state", b"")
    head = raw.split(b"\r\n\r\n", 1)[0].decode().lower()
    assert "cache-control: no-store" in head
    assert "x-frame-options: deny" in head


def test_the_page_exists_where_the_server_looks_for_it():
    from kbot.webui.server import PAGE

    assert PAGE.exists(), f"desk page missing at {PAGE}"
    assert b"Directional" in PAGE.read_bytes()
