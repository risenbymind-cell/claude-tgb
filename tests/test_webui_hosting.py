"""Hosting the desk: authentication, exposure guards, and the sandbox.

The desk places trades. Every test here is about the boundary between the
internet and that capability -- what has to be true before a request gets to
touch it, and what the server refuses to do at all.
"""

from __future__ import annotations

import json
import time

import pytest
from cryptography.fernet import Fernet

from kbot.config import Settings
from kbot.safety import TradingMode
from kbot.webui.auth import (
    LOCKOUT_AFTER,
    MIN_PASSWORD_CHARS,
    SESSION_IDLE_S,
    SESSION_TTL_S,
    AuthError,
    Authenticator,
    hash_password,
    password_hash_from_env,
    verify_password,
)
from kbot.webui.desk import Desk, SandboxLocked
from kbot.webui.server import DeskServer, _cookies, _form_field, _header

PASSWORD = "correcthorsebatterystaple"


def make_settings(tmp_path, mode=TradingMode.PAPER):
    return Settings(
        telegram_token="", admin_ids=frozenset(),
        master_key=Fernet.generate_key().decode(),
        db_path=tmp_path / "desk.sqlite3", demo=mode.uses_demo_host,
        mode=mode, series={"BTC": "KXBTC15M"}, spot_products={},
    )


@pytest.fixture()
def desk(tmp_path):
    return Desk(make_settings(tmp_path))


@pytest.fixture()
def sandbox_desk(tmp_path):
    return Desk(make_settings(tmp_path), sandbox=True)


@pytest.fixture(scope="module")
def pw_hash():
    # Hashed once: scrypt is deliberately slow, and every test reusing one
    # hash keeps the suite honest about that cost instead of hiding it.
    return hash_password(PASSWORD)


def status(raw: bytes) -> str:
    return raw.split(b"\r\n", 1)[0].decode().split(" ", 1)[1]


def head_of(raw: bytes) -> str:
    return raw.split(b"\r\n\r\n", 1)[0].decode()


def body_of(raw: bytes) -> dict:
    return json.loads(raw.split(b"\r\n\r\n", 1)[1])


def request(method, path, *, cookie="", csrf=None, extra=""):
    lines = [f"{method} {path} HTTP/1.1", "Host: desk.example.com"]
    if cookie:
        lines.append(f"Cookie: {cookie}")
    if csrf:
        lines.append(f"X-CSRF-Token: {csrf}")
    if extra:
        lines.append(extra)
    return "\r\n".join(lines) + "\r\n\r\n"


# ---------------- password hashing ----------------


def test_a_password_is_never_stored_in_the_clear(pw_hash):
    assert PASSWORD not in pw_hash
    assert pw_hash.startswith("scrypt$")


def test_the_right_password_verifies(pw_hash):
    assert verify_password(PASSWORD, pw_hash)


def test_the_wrong_password_does_not(pw_hash):
    assert not verify_password("wrong", pw_hash)
    assert not verify_password(PASSWORD + "x", pw_hash)
    assert not verify_password(PASSWORD[:-1], pw_hash)


def test_two_hashes_of_one_password_differ(pw_hash):
    """A per-hash salt, or one rainbow table breaks every deployment."""
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_a_malformed_hash_fails_closed():
    for junk in ("", "nonsense", "scrypt$bad", "md5$1$2$3$4$5", "scrypt$a$b$c$d$e"):
        assert verify_password(PASSWORD, junk) is False


def test_the_parameters_travel_with_the_hash(pw_hash):
    """So they can be raised later without invalidating what is stored."""
    scheme, n, r, p, salt, digest = pw_hash.split("$")
    assert scheme == "scrypt"
    assert int(n) >= 2**14 and int(r) >= 8
    assert len(bytes.fromhex(salt)) >= 16


# ---------------- configuration ----------------


def test_no_password_configured_means_disabled(monkeypatch):
    monkeypatch.delenv("DESK_PASSWORD", raising=False)
    monkeypatch.delenv("DESK_PASSWORD_HASH", raising=False)
    assert password_hash_from_env() is None


def test_a_short_password_is_refused_at_startup(monkeypatch):
    """Refused when it is cheap to fix, not after someone is brute-forced."""
    monkeypatch.delenv("DESK_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("DESK_PASSWORD", "short")
    with pytest.raises(AuthError) as exc:
        password_hash_from_env()
    assert str(MIN_PASSWORD_CHARS) in str(exc.value)


def test_a_plaintext_password_is_hashed_at_startup(monkeypatch):
    monkeypatch.delenv("DESK_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("DESK_PASSWORD", PASSWORD)
    stored = password_hash_from_env()
    assert stored.startswith("scrypt$")
    assert verify_password(PASSWORD, stored)


def test_a_hash_is_used_as_given(monkeypatch, pw_hash):
    monkeypatch.setenv("DESK_PASSWORD_HASH", pw_hash)
    assert password_hash_from_env() == pw_hash


def test_something_that_is_not_a_hash_is_refused(monkeypatch):
    """Pasting the plaintext into the _HASH variable must not silently make
    the literal password the hash."""
    monkeypatch.setenv("DESK_PASSWORD_HASH", PASSWORD)
    with pytest.raises(AuthError):
        password_hash_from_env()


# ---------------- sessions ----------------


def test_login_mints_a_session(pw_hash):
    auth = Authenticator(pw_hash)
    session, error = auth.login(PASSWORD)
    assert error is None and session is not None
    assert len(session.token) >= 32
    assert len(session.csrf) >= 32


def test_a_wrong_password_mints_nothing(pw_hash):
    auth = Authenticator(pw_hash)
    session, error = auth.login("nope")
    assert session is None and error


def test_two_logins_get_different_sessions(pw_hash):
    """Session fixation: an id planted before authentication must not
    survive it."""
    auth = Authenticator(pw_hash)
    a, _ = auth.login(PASSWORD)
    b, _ = auth.login(PASSWORD)
    assert a.token != b.token
    assert a.csrf != b.csrf


def test_an_unknown_token_is_not_a_session(pw_hash):
    auth = Authenticator(pw_hash)
    assert auth.session_for("made-up") is None
    assert auth.session_for(None) is None


def test_logout_destroys_the_session(pw_hash):
    auth = Authenticator(pw_hash)
    session, _ = auth.login(PASSWORD)
    auth.logout(session.token)
    assert auth.session_for(session.token) is None


def test_a_session_expires_absolutely(pw_hash):
    auth = Authenticator(pw_hash)
    session, _ = auth.login(PASSWORD)
    later = time.time() + SESSION_TTL_S + 1
    assert auth.session_for(session.token, now=later) is None


def test_a_session_expires_when_idle(pw_hash):
    auth = Authenticator(pw_hash)
    session, _ = auth.login(PASSWORD)
    later = time.time() + SESSION_IDLE_S + 1
    assert auth.session_for(session.token, now=later) is None


def test_activity_keeps_a_session_alive(pw_hash):
    auth = Authenticator(pw_hash)
    session, _ = auth.login(PASSWORD)
    now = time.time()
    for step in range(1, 5):
        t = now + step * (SESSION_IDLE_S - 60)
        assert auth.session_for(session.token, now=t) is not None


# ---------------- lockout ----------------


def test_repeated_failures_lock_out(pw_hash):
    auth = Authenticator(pw_hash)
    for _ in range(LOCKOUT_AFTER):
        auth.login("wrong", address="1.2.3.4")
    assert auth.lockout_remaining("1.2.3.4") > 0


def test_a_lockout_refuses_even_the_right_password(pw_hash):
    """Otherwise it is not a lockout, it is a hint."""
    auth = Authenticator(pw_hash)
    for _ in range(LOCKOUT_AFTER):
        auth.login("wrong", address="1.2.3.4")
    session, error = auth.login(PASSWORD, address="1.2.3.4")
    assert session is None
    assert "too many attempts" in error


def test_the_lockout_is_per_address(pw_hash):
    auth = Authenticator(pw_hash)
    for _ in range(LOCKOUT_AFTER):
        auth.login("wrong", address="1.2.3.4")
    session, error = auth.login(PASSWORD, address="5.6.7.8")
    assert session is not None


def test_a_success_clears_the_counter(pw_hash):
    auth = Authenticator(pw_hash)
    for _ in range(LOCKOUT_AFTER - 1):
        auth.login("wrong", address="1.2.3.4")
    auth.login(PASSWORD, address="1.2.3.4")
    for _ in range(LOCKOUT_AFTER - 1):
        auth.login("wrong", address="1.2.3.4")
    assert auth.lockout_remaining("1.2.3.4") == 0


def test_the_lockout_escalates_across_rounds(pw_hash):
    """Waiting out a lockout must not restore the original allowance.

    Escalation is measured across rounds rather than within one, because
    guesses made while already locked out are refused before they are counted
    -- so the attacker's only move is to wait, and waiting has to cost more
    each time or the lockout is just a slow rate limit.
    """
    auth = Authenticator(pw_hash)
    now = time.time()
    for _ in range(LOCKOUT_AFTER):
        auth.login("wrong", address="1.2.3.4", now=now)
    first = auth.lockout_remaining("1.2.3.4", now)
    assert first > 0

    # Wait it out, then guess wrong once more.
    now += first + 1
    auth.login("wrong", address="1.2.3.4", now=now)
    second = auth.lockout_remaining("1.2.3.4", now)
    assert second > first

    now += second + 1
    auth.login("wrong", address="1.2.3.4", now=now)
    assert auth.lockout_remaining("1.2.3.4", now) > second


def test_guesses_during_a_lockout_are_refused_before_being_counted(pw_hash):
    """Otherwise a script hammering through a lockout would push the delay to
    its cap and lock out the real operator for a quarter of an hour."""
    auth = Authenticator(pw_hash)
    now = time.time()
    for _ in range(LOCKOUT_AFTER):
        auth.login("wrong", address="1.2.3.4", now=now)
    locked = auth.lockout_remaining("1.2.3.4", now)
    for _ in range(50):
        auth.login("wrong", address="1.2.3.4", now=now)
    assert auth.lockout_remaining("1.2.3.4", now) == locked


# ---------------- CSRF ----------------


def test_csrf_requires_the_session_token(pw_hash):
    auth = Authenticator(pw_hash)
    session, _ = auth.login(PASSWORD)
    assert auth.check_csrf(session, session.csrf) is True
    assert auth.check_csrf(session, "wrong") is False
    assert auth.check_csrf(session, None) is False
    assert auth.check_csrf(session, "") is False


def test_one_sessions_csrf_does_not_work_for_another(pw_hash):
    auth = Authenticator(pw_hash)
    a, _ = auth.login(PASSWORD)
    b, _ = auth.login(PASSWORD)
    assert auth.check_csrf(a, b.csrf) is False


def test_no_session_means_no_csrf(pw_hash):
    assert Authenticator(pw_hash).check_csrf(None, "anything") is False


# ---------------- exposure guards ----------------


async def test_localhost_needs_no_credential(desk):
    server = DeskServer(desk)
    await server.start()
    await server.stop()


async def test_a_public_bind_with_no_credential_is_refused(desk):
    """The single most important refusal for a hosted deployment."""
    server = DeskServer(desk, host="0.0.0.0", allow_token_auth=False)
    with pytest.raises(RuntimeError) as exc:
        await server.start()
    assert "DESK_PASSWORD" in str(exc.value)


async def test_a_public_bind_with_a_password_is_allowed(desk, pw_hash):
    server = DeskServer(desk, host="0.0.0.0", port=18991, password_hash=pw_hash)
    await server.start()
    await server.stop()


async def test_a_public_bind_with_a_token_is_allowed(desk):
    server = DeskServer(desk, host="0.0.0.0", port=18992)
    assert server.token
    await server.start()
    await server.stop()


async def test_a_sandbox_may_be_public_with_nothing(sandbox_desk):
    """Nothing to protect: no credentials, no order path."""
    server = DeskServer(
        sandbox_desk, host="0.0.0.0", port=18993, allow_token_auth=False
    )
    await server.start()
    await server.stop()


def test_a_password_suppresses_the_url_token(desk, pw_hash):
    """Two doors is one more than needed, and the weak one is the one an
    attacker uses."""
    server = DeskServer(desk, host="0.0.0.0", password_hash=pw_hash)
    assert server.token is None


# ---------------- the authenticated surface ----------------


@pytest.fixture()
async def hosted(desk, pw_hash):
    return DeskServer(desk, host="0.0.0.0", password_hash=pw_hash, trust_proxy=True)


async def sign_in(server):
    raw = await server._route_authenticated(
        "POST", "/login", "", b"password=" + PASSWORD.encode(), request("POST", "/login"), "1.2.3.4",
    )
    cookie = [
        line for line in head_of(raw).split("\r\n") if line.startswith("Set-Cookie")
    ][0]
    token = cookie.split("desk_session=")[1].split(";")[0]
    session = server.auth.sessions[token]
    return token, session.csrf


async def test_the_root_redirects_to_login_when_signed_out(hosted):
    raw = await hosted._route_authenticated(
        "GET", "/", "", b"", request("GET", "/"), "1.2.3.4"
    )
    assert status(raw).startswith("303")
    assert "Location: /login" in head_of(raw)


async def test_the_api_401s_when_signed_out(hosted):
    raw = await hosted._route_authenticated(
        "GET", "/api/state", "", b"", request("GET", "/api/state"), "1.2.3.4"
    )
    assert status(raw).startswith("401")


async def test_health_is_reachable_without_a_session(hosted):
    """A load balancer cannot log in, and a health check that needs a session
    reports down forever."""
    raw = await hosted._route_authenticated(
        "GET", "/healthz", "", b"", request("GET", "/healthz"), "1.2.3.4"
    )
    assert status(raw).startswith("200")
    assert body_of(raw)["ok"] is True


async def test_a_wrong_password_does_not_sign_in(hosted):
    raw = await hosted._route_authenticated(
        "POST", "/login", "", b"password=wrong", request("POST", "/login"), "1.2.3.4"
    )
    assert status(raw).startswith("401")
    assert "Set-Cookie" not in head_of(raw)


async def test_the_right_password_signs_in(hosted):
    token, csrf = await sign_in(hosted)
    assert token and csrf


async def test_the_session_cookie_is_locked_down(hosted):
    raw = await hosted._route_authenticated(
        "POST", "/login", "", b"password=" + PASSWORD.encode(),
        request("POST", "/login"), "1.2.3.4",
    )
    head = head_of(raw)
    assert "HttpOnly" in head
    assert "SameSite=Strict" in head
    # trust_proxy=True implies TLS in front, so the cookie must be Secure.
    assert "Secure" in head


async def test_a_signed_in_request_reaches_the_api(hosted):
    token, _ = await sign_in(hosted)
    raw = await hosted._route_authenticated(
        "GET", "/api/state", "", b"",
        request("GET", "/api/state", cookie=f"desk_session={token}"), "1.2.3.4",
    )
    assert status(raw).startswith("200")


async def test_the_csrf_token_rides_along_with_the_state(hosted):
    token, csrf = await sign_in(hosted)
    raw = await hosted._route_authenticated(
        "GET", "/api/state", "", b"",
        request("GET", "/api/state", cookie=f"desk_session={token}"), "1.2.3.4",
    )
    payload = body_of(raw)
    assert payload["csrf"] == csrf
    assert payload["auth"]["enabled"] is True


async def test_a_post_without_csrf_is_refused(hosted):
    token, _ = await sign_in(hosted)
    raw = await hosted._route_authenticated(
        "POST", "/api/trading", "", b'{"enabled":true}',
        request("POST", "/api/trading", cookie=f"desk_session={token}"), "1.2.3.4",
    )
    assert status(raw).startswith("403")
    assert hosted.desk.enabled is False


async def test_a_post_with_csrf_is_accepted(hosted):
    token, csrf = await sign_in(hosted)
    raw = await hosted._route_authenticated(
        "POST", "/api/trading", "", b'{"enabled":true}',
        request("POST", "/api/trading", cookie=f"desk_session={token}", csrf=csrf),
        "1.2.3.4",
    )
    assert status(raw).startswith("200")
    assert hosted.desk.enabled is True


async def test_logout_ends_the_session(hosted):
    token, csrf = await sign_in(hosted)
    await hosted._route_authenticated(
        "POST", "/logout", "", b"",
        request("POST", "/logout", cookie=f"desk_session={token}", csrf=csrf),
        "1.2.3.4",
    )
    raw = await hosted._route_authenticated(
        "GET", "/api/state", "", b"",
        request("GET", "/api/state", cookie=f"desk_session={token}"), "1.2.3.4",
    )
    assert status(raw).startswith("401")


async def test_a_login_is_audited(hosted):
    await sign_in(hosted)
    assert any(a["action"] == "login" for a in hosted.desk.audit)


async def test_a_failed_login_is_audited(hosted):
    await hosted._route_authenticated(
        "POST", "/login", "", b"password=wrong", request("POST", "/login"), "1.2.3.4"
    )
    assert any(a["action"] == "login_failed" for a in hosted.desk.audit)


# ---------------- proxy headers ----------------


def test_a_forwarded_address_is_ignored_by_default(desk, pw_hash):
    """X-Forwarded-For is forged by whoever is talking to us. Trusting it
    unconditionally would let an attacker spread login attempts across
    imaginary addresses and never trip the lockout."""
    server = DeskServer(desk, host="0.0.0.0", password_hash=pw_hash)
    head = request("GET", "/", extra="X-Forwarded-For: 9.9.9.9")
    assert server.client_address(head, "1.2.3.4") == "1.2.3.4"


def test_a_forwarded_address_is_used_when_trusted(desk, pw_hash):
    server = DeskServer(
        desk, host="0.0.0.0", password_hash=pw_hash, trust_proxy=True
    )
    head = request("GET", "/", extra="X-Forwarded-For: 9.9.9.9, 10.0.0.1")
    assert server.client_address(head, "1.2.3.4") == "9.9.9.9"


def test_forwarded_proto_marks_the_cookie_secure(desk, pw_hash):
    server = DeskServer(
        desk, host="0.0.0.0", password_hash=pw_hash, trust_proxy=True
    )
    assert server._is_secure(request("GET", "/", extra="X-Forwarded-Proto: https"))


# ---------------- headers ----------------


async def test_every_response_carries_the_hardening_headers(desk):
    server = DeskServer(desk)
    head = head_of(await server._route("GET", "/api/state", b"")).lower()
    for header in (
        "cache-control: no-store",
        "x-frame-options: deny",
        "x-content-type-options: nosniff",
        "referrer-policy: no-referrer",
        "content-security-policy:",
    ):
        assert header in head, header


async def test_the_csp_forbids_remote_origins(desk):
    server = DeskServer(desk)
    head = head_of(await server._route("GET", "/api/state", b""))
    csp = [l for l in head.split("\r\n") if l.startswith("Content-Security-Policy")][0]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "connect-src 'self'" in csp


# ---------------- the sandbox ----------------


def test_a_sandbox_cannot_be_built_on_a_live_mode(tmp_path):
    """Contradictory intentions are refused rather than guessed at."""
    for mode in (TradingMode.DEMO_LIVE, TradingMode.PRODUCTION_LIVE):
        with pytest.raises(SandboxLocked):
            Desk(make_settings(tmp_path, mode), sandbox=True)


def test_a_sandbox_refuses_to_connect_credentials(sandbox_desk):
    result = sandbox_desk.connect_keys("key-id", "-----BEGIN PRIVATE KEY-----")
    assert result["ok"] is False
    assert sandbox_desk.connected_key_id is None


def test_a_sandbox_refuses_arming(sandbox_desk):
    from kbot.webui.desk import PRODUCTION_ACK_A, PRODUCTION_ACK_B

    assert sandbox_desk.arm_step_a(PRODUCTION_ACK_A)["ok"] is False
    assert sandbox_desk.arm_step_b(PRODUCTION_ACK_B)["ok"] is False
    assert sandbox_desk.production_armed is False


def test_a_sandbox_broker_is_always_paper(sandbox_desk):
    """The last line of defence: even with a signer somehow installed, no
    live broker can be constructed."""
    from kbot.engine.broker import PaperBroker

    sandbox_desk._signer = object()
    assert isinstance(sandbox_desk.broker(), PaperBroker)


async def test_sandbox_routes_refuse_at_the_http_layer(sandbox_desk):
    """Two independent refusals, not one."""
    server = DeskServer(sandbox_desk)
    for path in ("/api/connect", "/api/arm/step-a", "/api/arm/step-b"):
        raw = await server._route("POST", path, b"{}")
        assert status(raw).startswith("403"), path


async def test_a_sandbox_still_serves_the_desk(sandbox_desk):
    server = DeskServer(sandbox_desk)
    assert status(await server._route("GET", "/api/state", b"")).startswith("200")
    assert body_of(await server._route("GET", "/api/state", b""))["sandbox"] is True


def test_the_sandbox_says_so_in_its_posture(sandbox_desk):
    why = sandbox_desk.snapshot()["live_posture"]["why"]
    assert "sandbox" in why.lower()
    assert "cannot place an order" in why.lower()


# ---------------- helpers ----------------


def test_header_lookup_is_case_insensitive():
    head = "GET / HTTP/1.1\r\nX-Forwarded-For: 1.2.3.4\r\n\r\n"
    assert _header(head, "x-forwarded-for") == "1.2.3.4"
    assert _header(head, "X-Forwarded-For") == "1.2.3.4"
    assert _header(head, "missing") is None


def test_cookie_parsing():
    head = "GET / HTTP/1.1\r\nCookie: a=1; desk_session=abc; b=2\r\n\r\n"
    assert _cookies(head)["desk_session"] == "abc"


def test_form_parsing():
    assert _form_field(b"password=hunter2", "password") == "hunter2"
    assert _form_field(b"password=a+b%20c", "password") == "a b c"
    assert _form_field(b"", "password") == ""
    assert _form_field(b"other=1", "password") == ""


def test_the_login_page_exists_where_the_server_looks():
    from kbot.webui.server import LOGIN_PAGE

    assert LOGIN_PAGE.exists(), f"login page missing at {LOGIN_PAGE}"
    text = LOGIN_PAGE.read_text()
    assert "<!--ERROR-->" in text, "error placeholder must exist"
    assert 'name="password"' in text
    assert 'method="POST"' in text


def test_the_login_page_is_not_indexable():
    from kbot.webui.server import LOGIN_PAGE

    assert "noindex" in LOGIN_PAGE.read_text()


# ---------------- the process ----------------
#
# These start a real process. A hosted app is redeployed by being sent
# SIGTERM, so "does it die cleanly" is a property of the deployment, not a
# detail -- and it cannot be checked without actually signalling something.


@pytest.mark.slow
def test_sigterm_shuts_down_cleanly(tmp_path):
    """A redeploy sends SIGTERM while the desk may be mid-order. Dying at the
    socket instead of draining turns a recoverable intent into a position
    nobody knows about."""
    import os
    import signal
    import subprocess
    import sys
    import urllib.request

    env = {**os.environ, "TRADING_MODE": "paper", "DB_PATH": str(tmp_path / "d.sqlite3")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "kbot.webui", "--sandbox", "--port", "18781"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen("http://127.0.0.1:18781/healthz", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            proc.kill()
            pytest.fail("desk never became healthy")

        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("desk did not exit within 30s of SIGTERM")

    assert proc.returncode == 0, f"unclean exit {proc.returncode}:\n{out}"
    assert "Shutting down" in out
    assert "stopped cleanly" in out


@pytest.mark.slow
def test_it_refuses_to_start_publicly_with_no_password(tmp_path):
    """The refusal has to survive the whole startup path, not just the unit
    test -- this is the one that would actually be hit by a bad deploy."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "TRADING_MODE": "paper", "DB_PATH": str(tmp_path / "d.sqlite3")}
    env.pop("DESK_PASSWORD", None)
    env.pop("DESK_PASSWORD_HASH", None)
    proc = subprocess.run(
        [sys.executable, "-m", "kbot.webui", "--host", "0.0.0.0", "--port", "18782"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 2, "a bad deploy must fail, not serve"
    assert "DESK_PASSWORD" in proc.stderr


async def test_a_login_error_is_escaped_into_the_page(hosted):
    raw = await hosted._route_authenticated(
        "POST", "/login", "", b"password=wrong", request("POST", "/login"), "1.2.3.4"
    )
    page = raw.split(b"\r\n\r\n", 1)[1].decode()
    assert "incorrect password" in page
    assert "<!--ERROR-->" not in page
