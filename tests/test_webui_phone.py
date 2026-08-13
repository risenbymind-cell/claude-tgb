"""Phone access to the desk.

Reaching the desk from a phone means leaving localhost, and localhost was the
only thing authenticating it. Every test here is about that trade: the token
must be mandatory the instant the bind address is not local, and it must be
generated rather than configured so there is no weak one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from kbot.config import Settings
from kbot.safety import TradingMode
from kbot.webui.desk import Desk
from kbot.webui.server import PAGE, DeskServer, lan_address

PAGE_TEXT = PAGE.read_text()


@pytest.fixture()
def desk(tmp_path):
    return Desk(
        Settings(
            telegram_token="",
            admin_ids=frozenset(),
            master_key=Fernet.generate_key().decode(),
            db_path=tmp_path / "d.sqlite3",
            demo=False,
            mode=TradingMode.PAPER,
            series={"BTC": "KXBTC15M"},
            spot_products={},
        )
    )


def status(raw: bytes) -> str:
    return raw.split(b"\r\n", 1)[0].decode().split(" ", 1)[1]


# ---------------- the token ----------------


def test_localhost_needs_no_token(desk):
    """The operating system is the boundary there -- only this machine can
    connect, so a password would protect nothing."""
    assert DeskServer(desk).token is None


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::"])
def test_leaving_localhost_forces_a_token(desk, host):
    """Anyone on the Wi-Fi can reach a port that places orders."""
    server = DeskServer(desk, host=host)
    assert server.token, f"{host} must not be served unauthenticated"
    assert len(server.token) >= 16


def test_the_token_is_generated_not_configured(desk):
    """Two servers must not share a token, and no default can be guessed."""
    a = DeskServer(desk, host="0.0.0.0").token
    b = DeskServer(desk, host="0.0.0.0").token
    assert a != b


def test_the_printed_link_carries_the_token(desk):
    server = DeskServer(desk, host="0.0.0.0", port=8787)
    url = server.url("192.168.1.20")
    assert url.startswith("http://192.168.1.20:8787/")
    assert server.token in url


def test_a_localhost_link_has_no_token_in_it(desk):
    assert "?t=" not in DeskServer(desk).url()


# ---------------- the gate ----------------


def head_with(**headers) -> str:
    lines = ["GET / HTTP/1.1"] + [f"{k}: {v}" for k, v in headers.items()]
    return "\r\n".join(lines) + "\r\n\r\n"


def test_no_credential_is_refused(desk):
    server = DeskServer(desk, host="0.0.0.0")
    assert not server._authorised(head_with(Host="x"), "")


def test_a_wrong_token_is_refused(desk):
    server = DeskServer(desk, host="0.0.0.0")
    assert not server._authorised(head_with(Host="x"), "t=wrong")
    assert not server._authorised(head_with(Cookie="desk_token=wrong"), "")
    assert not server._authorised(head_with(**{"X-Desk-Token": "wrong"}), "")


def test_a_near_miss_token_is_refused(desk):
    """A prefix must not pass -- substring checks are how these get broken."""
    server = DeskServer(desk, host="0.0.0.0")
    assert not server._authorised(head_with(**{"X-Desk-Token": server.token[:-1]}), "")


def test_every_accepted_route_in(desk):
    server = DeskServer(desk, host="0.0.0.0")
    tok = server.token
    assert server._authorised(head_with(Host="x"), f"t={tok}")
    assert server._authorised(head_with(Cookie=f"desk_token={tok}"), "")
    assert server._authorised(head_with(**{"X-Desk-Token": tok}), "")


def test_the_cookie_is_locked_down(desk):
    """Set-Cookie is what lets a reload work; it must not become a way for
    another site to drive the desk."""
    from kbot.webui.server import _response

    raw = _response("200 OK", b"x", "text/html", set_token="abc").decode()
    assert "desk_token=abc" in raw
    assert "HttpOnly" in raw
    assert "SameSite=Strict" in raw


# ---------------- the network ----------------


def test_lan_address_is_an_address(desk):
    """UDP connect asks the kernel which interface it would use; nothing is
    sent, so this works offline."""
    assert re.match(r"^\d{1,3}(\.\d{1,3}){3}$", lan_address())


# ---------------- the layout ----------------


def test_the_page_is_usable_on_a_narrow_screen():
    """A 900px table on a 390px screen is not a layout problem, it is an
    unusable page: net, side and extension sit off-screen behind a scroll
    nobody discovers."""
    assert "@media (max-width: 760px)" in PAGE_TEXT
    assert "thead{display:none}" in PAGE_TEXT, "table must become cards"


def test_every_cell_carries_its_own_label():
    """With the header row hidden, a bare number means nothing."""
    assert PAGE_TEXT.count('class="card-label"') >= 18
    assert len(set(re.findall(r'data-k="(\w+)"', PAGE_TEXT))) >= 15


def test_inputs_do_not_trigger_ios_zoom():
    """Safari zooms the whole page when a focused input is under 16px, and
    the layout never comes back."""
    phone_css = PAGE_TEXT.split("@media (max-width: 760px)")[1][:1200]
    assert "font-size:16px" in phone_css


def test_it_is_installable_to_the_home_screen():
    for tag in (
        "apple-mobile-web-app-capable",
        "apple-mobile-web-app-title",
        'name="theme-color"',
        "viewport-fit=cover",
    ):
        assert tag in PAGE_TEXT, tag


def test_content_clears_the_notch_and_home_bar():
    assert "env(safe-area-inset" in PAGE_TEXT


def test_the_theme_is_declared_for_both_schemes():
    """The status bar takes its colour from these; one of them alone leaves
    a white bar above a dark page."""
    assert PAGE_TEXT.count('name="theme-color"') == 2
    assert "prefers-color-scheme: light" in PAGE_TEXT
    assert "prefers-color-scheme: dark" in PAGE_TEXT
