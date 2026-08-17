"""The AETHER DESK console surfaces: audit, decisions, shadow, arming, connect.

These sit on top of the desk tested in test_webui.py. The theme carries over:
the interesting behaviour is what gets refused, and what a UI action can and
cannot actually reach in the backend.
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kbot.config import Settings
from kbot.safety import TradingMode
from kbot.strategy import Signal
from kbot.webui.desk import (
    ARM_WINDOW_S,
    PRODUCTION_ACK_A,
    PRODUCTION_ACK_B,
    Desk,
    MarketView,
)
from kbot.webui.server import DeskServer


def make_settings(tmp_path, *, mode=TradingMode.PAPER, master_key=None):
    return Settings(
        telegram_token="",
        admin_ids=frozenset(),
        master_key=master_key or Fernet.generate_key().decode(),
        db_path=tmp_path / "desk.sqlite3",
        demo=False,
        mode=mode,
        series={"BTC": "KXBTC15M"},
        spot_products={},
    )


@pytest.fixture()
def desk(tmp_path):
    return Desk(make_settings(tmp_path))


@pytest.fixture()
def armed_desk(tmp_path):
    """A desk running in a process actually started production-live -- the
    only configuration in which step B of arming can ever succeed."""
    return Desk(make_settings(tmp_path, mode=TradingMode.PRODUCTION_LIVE))


async def post(server, path, payload):
    return await server._route("POST", path, json.dumps(payload).encode())


def body(raw: bytes) -> dict:
    return json.loads(raw.split(b"\r\n\r\n", 1)[1])


def status(raw: bytes) -> str:
    return raw.split(b"\r\n", 1)[0].decode().split(" ", 1)[1]


@pytest.fixture(scope="module")
def rsa_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


# ---------------- audit ----------------


async def test_boot_is_the_first_audit_entry(desk):
    desk.start()
    assert desk.audit[-1]["action"] == "engine_boot"
    await desk.stop()


async def test_commands_are_audited(desk):
    server = DeskServer(desk)
    await post(server, "/api/kill", {"reason": "manual stop"})
    assert desk.audit[0]["action"] == "kill"
    assert desk.audit[0]["actor"] == "operator"
    assert desk.audit[0]["detail"]["reason"] == "manual stop"

    await post(server, "/api/resume", {})
    assert desk.audit[0]["action"] == "resume"

    await post(server, "/api/strategy", {"strategy": "drift"})
    assert desk.audit[0]["detail"]["strategy"] == "drift"

    await post(server, "/api/settings", {"size": 5})
    assert desk.audit[0]["detail"]["size"] == 5


async def test_the_state_endpoint_carries_recent_audit(desk):
    server = DeskServer(desk)
    await post(server, "/api/kill", {"reason": "x"})
    snap = body(await server._route("GET", "/api/state", b""))
    assert any(a["action"] == "kill" for a in snap["audit"])


# ---------------- decisions ----------------


def _live_book(ticker):
    import time

    from kbot.kalshi.orderbook import OrderBook

    book = OrderBook(ticker)
    book.yes = {510: 50.0}
    book.no = {490: 100.0}
    book.updated_at = time.time()
    return book


def _view():
    return MarketView(
        coin="BTC", ticker="KXBTC15M-BTC", seconds_to_close=400.0,
        mid_dc=500.0, spread_dc=2, yes_ask=51, no_ask=51,
        depth=100.0, yes_depth=60.0, no_depth=40.0, imbalance=0.2,
        vwap_dc=498.0, range_dc=20.0, extension=0.1, velocity_dc=1.0,
        mom20_dc=0.5, rt_fee_dc=140, book_age_s=0.5,
    )


async def test_decisions_start_empty(desk):
    assert list(desk.decisions) == []


# ---------------- shadow mode ----------------


async def test_shadow_mode_logs_but_does_not_open_a_position(desk, monkeypatch):
    desk.enabled = True
    desk.shadow = True
    view = _view()
    book = _live_book(view.ticker)
    monkeypatch.setattr(desk.feed, "book", lambda ticker: book)

    signal = Signal(
        coin="BTC", ticker=view.ticker, side="yes",
        confidence=0.7, price_dc=510, reason="test_fire",
    )
    await desk._maybe_open(view, signal, 1000.0)

    assert desk.positions == []
    assert any(a["action"] == "shadow_signal" for a in desk.audit)


async def test_normal_mode_still_opens_a_position(desk, monkeypatch):
    desk.enabled = True
    assert desk.shadow is False
    view = _view()
    book = _live_book(view.ticker)
    monkeypatch.setattr(desk.feed, "book", lambda ticker: book)

    signal = Signal(
        coin="BTC", ticker=view.ticker, side="yes",
        confidence=0.7, price_dc=510, reason="test_fire",
    )
    await desk._maybe_open(view, signal, 1000.0)

    assert len(desk.positions) == 1
    assert desk.positions[0].client_order_id.startswith("paper-")


async def test_the_shadow_endpoint_toggles_and_is_audited(desk):
    server = DeskServer(desk)
    snap = body(await post(server, "/api/shadow", {"enabled": True}))
    assert snap["shadow"] is True
    assert desk.audit[0]["action"] == "shadow"


# ---------------- production arming ----------------


async def test_step_a_requires_the_exact_phrase(desk):
    assert desk.arm_step_a("close enough")["ok"] is False
    assert desk._armed_a_at is None
    assert desk.arm_step_a(PRODUCTION_ACK_A)["ok"] is True
    assert desk._armed_a_at is not None


async def test_step_b_without_step_a_is_refused(desk):
    result = desk.arm_step_b(PRODUCTION_ACK_B)
    assert result["ok"] is False
    assert desk.production_armed is False


async def test_step_b_wrong_phrase_is_refused(desk):
    desk.arm_step_a(PRODUCTION_ACK_A)
    result = desk.arm_step_b("not it")
    assert result["ok"] is False
    assert desk.production_armed is False


async def test_step_b_is_refused_outside_production_mode(desk):
    """A desk running in paper mode cannot be armed into placing real orders
    by typing a phrase -- the phrase can only expose the real gate."""
    desk.arm_step_a(PRODUCTION_ACK_A)
    result = desk.arm_step_b(PRODUCTION_ACK_B)
    assert result["ok"] is False
    assert "paper" in result["error"]
    assert desk.production_armed is False


async def test_step_b_succeeds_when_the_process_is_already_production_live(armed_desk):
    armed_desk.arm_step_a(PRODUCTION_ACK_A)
    result = armed_desk.arm_step_b(PRODUCTION_ACK_B)
    assert result["ok"] is True
    assert armed_desk.production_armed is True


async def test_step_a_expires(desk):
    desk.arm_step_a(PRODUCTION_ACK_A)
    desk._armed_a_at -= ARM_WINDOW_S + 1
    result = desk.arm_step_b(PRODUCTION_ACK_B)
    assert result["ok"] is False
    assert "expired" in result["error"]


async def test_disarm_clears_armed_state(armed_desk):
    armed_desk.arm_step_a(PRODUCTION_ACK_A)
    armed_desk.arm_step_b(PRODUCTION_ACK_B)
    assert armed_desk.production_armed is True
    armed_desk.disarm()
    assert armed_desk.production_armed is False


async def test_arming_routes_are_wired(desk):
    server = DeskServer(desk)
    raw = await post(server, "/api/arm/step-a", {"phrase": PRODUCTION_ACK_A})
    assert body(raw)["ok"] is True
    raw = await post(server, "/api/arm/step-b", {"phrase": PRODUCTION_ACK_B})
    assert body(raw)["ok"] is False  # still paper mode
    await post(server, "/api/arm/disarm", {})
    assert desk.production_armed is False


# ---------------- connect ----------------


async def test_connect_rejects_garbage(desk):
    result = desk.connect_keys("some-key-id", "not a real pem")
    assert result["ok"] is False
    assert desk.connected_key_id is None


async def test_connect_rejects_empty_fields(desk):
    assert desk.connect_keys("", "")["ok"] is False


async def test_connect_accepts_a_real_key_and_masks_it(desk, rsa_pem):
    result = desk.connect_keys("abcd1234efgh5678", rsa_pem)
    assert result["ok"] is True
    assert result["key_id"] != "abcd1234efgh5678"
    assert "abcd" in result["key_id"]
    assert desk.connected_key_id == "abcd1234efgh5678"


async def test_connect_never_writes_the_pem_to_the_audit_log(desk, rsa_pem):
    desk.connect_keys("abcd1234efgh5678", rsa_pem)
    for entry in desk.audit:
        assert rsa_pem not in json.dumps(entry)


async def test_a_connected_key_is_never_written_to_disk(desk, rsa_pem):
    """It used to be, encrypted, and nothing ever read it back.

    A credential written but never loaded survives no restart, leaves a file
    holding a private key at whatever umask the process had, and is
    undecryptable anyway whenever MASTER_KEY is the ephemeral one. Pure
    liability. The key lives in memory for this process only.
    """
    desk.connect_keys("abcd1234efgh5678", rsa_pem)
    directory = desk.settings.db_path.parent
    if directory.exists():
        for path in directory.rglob("*"):
            if path.is_file():
                assert rsa_pem.encode() not in path.read_bytes(), path
    assert not (directory / "desk_key.enc").exists()


async def test_connect_endpoint_rejects_bad_key_with_400(desk):
    server = DeskServer(desk)
    raw = await post(server, "/api/connect", {"key_id": "x", "private_key_pem": "nope"})
    assert status(raw).startswith("400")


async def test_connect_endpoint_accepts_a_real_key(desk, rsa_pem):
    server = DeskServer(desk)
    raw = await post(
        server, "/api/connect", {"key_id": "abcd1234efgh5678", "private_key_pem": rsa_pem}
    )
    assert status(raw).startswith("200")
    snap = body(await server._route("GET", "/api/state", b""))
    assert snap["live_posture"]["keys_connected"] is True


# ---------------- ephemeral master key warning ----------------


async def test_master_key_is_ephemeral_is_surfaced(tmp_path):
    from kbot.config import load_settings
    import os

    old = os.environ.pop("MASTER_KEY", None)
    try:
        settings = load_settings(require_bot=False)
    finally:
        if old is not None:
            os.environ["MASTER_KEY"] = old
    assert settings.master_key_is_ephemeral is True

    desk = Desk(settings)
    snap = desk.snapshot()
    assert snap["live_posture"]["master_key_is_ephemeral"] is True


async def test_master_key_is_not_ephemeral_when_configured(desk):
    assert desk.settings.master_key_is_ephemeral is False
    assert desk.snapshot()["live_posture"]["master_key_is_ephemeral"] is False


# ---------------- research ----------------


async def test_the_research_route_answers(desk):
    server = DeskServer(desk)
    raw = await server._route("GET", "/api/research", b"")
    assert status(raw).startswith("200")
    payload = body(raw)
    assert "settled" in payload and "required" in payload


async def test_research_reports_no_data_rather_than_failing(desk, tmp_path, monkeypatch):
    """A missing recordings directory is the normal state on a fresh install,
    not an error worth breaking a tab over."""
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path / "does-not-exist"))
    desk._research_cache = None
    payload = await desk.research()
    assert payload["available"] is True
    assert payload["settled"] == 0
    assert payload["enough"] is False


async def test_research_is_cached(desk, monkeypatch):
    """It reads every recording on disk; the desk polls once a second."""
    calls = []
    real = desk.research

    from kbot.research import inventory as inv_mod

    original = inv_mod.take_inventory

    def counting(directory):
        calls.append(directory)
        return original(directory)

    monkeypatch.setattr(inv_mod, "take_inventory", counting)
    desk._research_cache = None
    await desk.research()
    await desk.research()
    await desk.research()
    assert len(calls) == 1, "the inventory must not be re-read on every poll"


async def test_the_research_tab_exists_in_the_page():
    from kbot.webui.server import PAGE

    text = PAGE.read_text()
    assert 'data-tab="research"' in text
    assert 'id="tab-research"' in text
    assert "function renderResearch" in text
    assert "/api/research" in text


# ---------------- health / reconcile ----------------


async def test_health_reports_clock_sync(desk):
    result = await desk.health()
    assert "ok" in result
    assert desk.audit[0]["action"] == "health_check"


async def test_reconcile_forces_rediscovery_and_is_audited(desk):
    desk.last_refresh = 999999999.0
    result = await desk.reconcile()
    assert result["ok"] is True
    assert desk.last_refresh == 0.0
    assert desk.audit[0]["action"] == "reconcile"


async def test_health_and_reconcile_routes_are_wired(desk):
    server = DeskServer(desk)
    raw = await post(server, "/api/health", {})
    assert status(raw).startswith("200")
    raw = await post(server, "/api/reconcile", {})
    assert status(raw).startswith("200")
