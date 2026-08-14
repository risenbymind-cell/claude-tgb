"""An HTTP server for the trading desk, local or hosted.

Built on `asyncio.start_server` to match the payment webhook and to keep the
dependency list at four packages. It serves two pages and a handful of JSON
routes; a framework would be the largest dependency in the project.

**Binds to localhost by default.** This process holds decrypted Kalshi
credentials and can place orders, so exposing it hands that to anyone who can
reach the port. There are exactly two supported ways to leave localhost, and
the server will not start any other way:

* `--phone`, which mints a single-use-ish URL token for a home network. Fine
  behind a router, useless on the internet, and it says so.
* A password (`DESK_PASSWORD` / `DESK_PASSWORD_HASH`), which switches on real
  session authentication -- see `auth.py`. This is the hosted path.

Binding to a public address with neither is a configuration mistake serious
enough to be worth refusing rather than warning about, so `start()` raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus

from .auth import SESSION_TTL_S, Authenticator
from .desk import Desk

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
SITE = Path(__file__).resolve().parent.parent.parent / "site"
PAGE = SITE / "desk.html"
LOGIN_PAGE = SITE / "login.html"

#: Requests that change something. Every one of these needs a CSRF token when
#: session auth is on, and none of them are reachable by a GET.
STATE_CHANGING = ("POST",)

#: Routes a sandbox instance refuses outright. Everything here either accepts
#: a credential or moves the desk toward placing a real order.
SANDBOX_FORBIDDEN = frozenset({
    "/api/connect", "/api/arm/step-a", "/api/arm/step-b",
})


def lan_address() -> str:
    """This machine's address on the local network.

    Opens a UDP socket toward a public address and asks the kernel which
    interface it would use. Nothing is sent -- UDP connect only sets the
    destination -- so it works offline and costs no round trip.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


#: Everything the page needs is inline, so the policy can forbid every remote
#: origin outright. `frame-ancestors 'none'` is the modern X-Frame-Options and
#: covers proxies that strip the older header.
CSP = (
    "default-src 'none'; "
    "img-src 'self' data:; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


def _response(
    status: str,
    body: bytes,
    content_type: str,
    *,
    set_token: str | None = None,
    clear_token: bool = False,
    secure: bool = False,
    cookie_name: str = "desk_token",
    max_age: int = 604800,
    extra_headers: str = "",
) -> bytes:
    cookie = ""
    if clear_token:
        cookie = (
            f"Set-Cookie: {cookie_name}=; Path=/; HttpOnly; SameSite=Strict; "
            "Max-Age=0\r\n"
        )
    elif set_token:
        # SameSite=Strict so another site cannot drive the desk with the
        # browser's stored cookie. HttpOnly because no script needs to read it.
        # Secure once TLS is in play, so it cannot leak over a plain-HTTP hop.
        flags = "HttpOnly; SameSite=Strict" + ("; Secure" if secure else "")
        cookie = (
            f"Set-Cookie: {cookie_name}={set_token}; Path=/; {flags}; "
            f"Max-Age={max_age}\r\n"
        )
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        # The desk is a control surface for something that spends money. No
        # caching, no framing, no sniffing, no referrer leakage.
        "Cache-Control: no-store\r\n"
        "X-Frame-Options: DENY\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "Referrer-Policy: no-referrer\r\n"
        f"Content-Security-Policy: {CSP}\r\n"
        "Connection: close\r\n"
        + extra_headers
        + cookie
        + "\r\n"
    ).encode() + body


def _json(obj, status: str = "200 OK") -> bytes:
    return _response(status, json.dumps(obj).encode(), "application/json")


def _redirect(location: str) -> bytes:
    return _response(
        "303 See Other", b"", "text/plain",
        extra_headers=f"Location: {location}\r\n",
    )


def _form_field(body: bytes, name: str) -> str:
    """One field from an application/x-www-form-urlencoded body.

    The login form is a plain HTML form rather than fetch(), so that a browser
    with JavaScript disabled -- or a page whose script failed to load -- can
    still sign in rather than showing a dead box.
    """
    try:
        fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    except ValueError:
        return ""
    values = fields.get(name) or [""]
    return values[0]


LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _header(head: str, name: str) -> str | None:
    """First value of a header, case-insensitively."""
    prefix = name.lower() + ":"
    for line in head.split("\r\n"):
        if line.lower().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return None


def _cookies(head: str) -> dict[str, str]:
    raw = _header(head, "cookie") or ""
    out: dict[str, str] = {}
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            out[name] = value
    return out


class DeskServer:
    """Serves the desk, and gates it whenever it is reachable off-machine.

    On localhost the operating system is the boundary: only this machine can
    connect, so a password would protect nothing. The moment the bind address
    leaves localhost that stops being true, and one of two credentials becomes
    mandatory:

    * a **password**, which turns on session authentication and is the only
      thing suitable for the public internet;
    * a **URL token**, generated rather than chosen so there is no weak one,
      which is adequate behind a home router and nowhere else.

    Binding publicly with neither is refused outright at `start()`.
    """

    def __init__(
        self,
        desk: Desk,
        host: str = "127.0.0.1",
        port: int = 8787,
        token: str | None = None,
        *,
        password_hash: str | None = None,
        allow_token_auth: bool = True,
        trust_proxy: bool = False,
        require_https: bool | None = None,
    ) -> None:
        self.desk = desk
        self.host = host
        self.port = port
        self.local_only = host in LOCAL_HOSTS
        self.auth = Authenticator(password_hash)
        self.trust_proxy = trust_proxy
        #: When a password is set, the URL token is not minted at all -- two
        #: ways in is one more than needed, and the weaker one would be the
        #: one an attacker uses.
        self.token = (
            None if (self.local_only or self.auth.enabled or not allow_token_auth)
            else (token or secrets.token_urlsafe(16))
        )
        #: Mark cookies Secure. Defaults to on whenever a proxy is trusted,
        #: since that is the hosted shape and it always terminates TLS.
        self.require_https = trust_proxy if require_https is None else require_https
        self._server: asyncio.AbstractServer | None = None

    # ---------------- addressing ----------------

    def url(self, host: str | None = None) -> str:
        base = f"http://{host or self.host}:{self.port}/"
        return base + (f"?t={self.token}" if self.token else "")

    def client_address(self, head: str, peer: str = "") -> str:
        """The client's address, honouring a proxy header only when told to.

        `X-Forwarded-For` is trivially forged by whoever is talking to us, so
        trusting it unconditionally would let an attacker spread their login
        attempts across imaginary addresses and never trip the lockout. It is
        read only when the operator has said a trusted proxy sits in front.
        """
        if self.trust_proxy:
            forwarded = _header(head, "x-forwarded-for")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return peer

    def _is_secure(self, head: str) -> bool:
        if not self.trust_proxy:
            return self.require_https
        proto = (_header(head, "x-forwarded-proto") or "").lower()
        return proto == "https" or self.require_https

    # ---------------- authorisation ----------------

    def _session(self, head: str):
        if not self.auth.enabled:
            return None
        return self.auth.session_for(_cookies(head).get("desk_session"))

    def _authorised(self, head: str, query: str) -> bool:
        """Token-or-open check. Session auth is handled separately."""
        if self.auth.enabled:
            return self._session(head) is not None
        if self.token is None:
            return True
        if f"t={self.token}" in query:
            return True
        cookies = _cookies(head)
        if secrets.compare_digest(cookies.get("desk_token", ""), self.token):
            return True
        presented = _header(head, "x-desk-token")
        if presented and secrets.compare_digest(presented, self.token):
            return True
        return False

    # ---------------- lifecycle ----------------

    def _refuse_unsafe_exposure(self) -> None:
        if self.local_only or self.auth.enabled or self.token:
            return
        if self.desk.sandbox:
            # Nothing to protect: a sandbox holds no credentials, refuses to
            # accept any, and has no path to an exchange. Demanding a password
            # here would be a ritual rather than a control, and the whole point
            # of the mode is that it can be handed to strangers.
            log.info(
                "Sandbox desk on %s:%d, open by design -- no credentials, no "
                "order path.", self.host, self.port,
            )
            return
        raise RuntimeError(
            f"Refusing to serve the desk on {self.host}:{self.port} with no "
            "credential.\n"
            "This port can place trades and holds decrypted Kalshi keys.\n\n"
            "For a hosted deployment, set a password:\n"
            "    DESK_PASSWORD=<at least 12 characters>\n"
            "  or, better, keep the plaintext off the host:\n"
            "    DESK_PASSWORD_HASH=$(python -m kbot.webui hash-password)\n\n"
            "For a phone on your own Wi-Fi, use --phone, which mints a URL "
            "token."
        )

    async def start(self) -> None:
        self._refuse_unsafe_exposure()
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        if not self.local_only:
            if self.auth.enabled:
                log.info(
                    "Desk on %s:%d with password authentication.",
                    self.host, self.port,
                )
                if not self.require_https:
                    log.warning(
                        "Desk is not marked HTTPS-only. Put TLS in front of it "
                        "and set DESK_TRUST_PROXY=1, or the password and "
                        "session cookie cross the network in the clear."
                    )
            else:
                log.warning(
                    "Desk reachable on %s:%d with only a URL token -- suitable "
                    "for a home network, NOT for the internet. Set "
                    "DESK_PASSWORD for a hosted deployment.",
                    self.host, self.port,
                )
        log.info("Desk on http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
            writer.close()
            return

        try:
            head = request.decode("latin-1")
            method, target, _ = head.split("\r\n", 1)[0].split(" ", 2)
            path, _, query = target.partition("?")
        except ValueError:
            writer.write(_json({"error": "bad request"}, "400 Bad Request"))
            await writer.drain()
            writer.close()
            return

        try:
            peer = writer.get_extra_info("peername") or ()
            peer_ip = peer[0] if peer else ""
        except Exception:  # noqa: BLE001
            peer_ip = ""

        body = b""
        for line in head.split("\r\n"):
            if line.lower().startswith("content-length:"):
                try:
                    length = min(int(line.split(":", 1)[1].strip()), MAX_BODY_BYTES)
                except ValueError:
                    length = 0
                if length:
                    try:
                        body = await asyncio.wait_for(
                            reader.readexactly(length), timeout=10
                        )
                    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                        body = b""
                break

        try:
            if self.auth.enabled:
                out = await self._route_authenticated(
                    method, path, query, body, head, peer_ip
                )
            elif not self._authorised(head, query):
                out = _response(
                    "401 Unauthorized",
                    b"<h1>Desk locked</h1><p>Open the link printed in the "
                    b"terminal -- it carries the access token.</p>",
                    "text/html; charset=utf-8",
                )
            else:
                out = await self._route(method, path, body)
                # First authorised load carries the token in the query; store
                # it so a reload or a home-screen launch works without it.
                if self.token and f"t={self.token}" in query and method == "GET":
                    out = _response(
                        "200 OK", out.split(b"\r\n\r\n", 1)[1],
                        "text/html; charset=utf-8" if path in ("/", "/index.html")
                        else "application/json",
                        set_token=self.token,
                        secure=self._is_secure(head),
                    )
        except Exception as exc:  # noqa: BLE001 - never take the desk down
            log.exception("Desk request failed")
            out = _json({"error": str(exc)[:200]}, "500 Internal Server Error")

        writer.write(out)
        try:
            await writer.drain()
        except OSError:
            pass
        writer.close()

    # ---------------- the authenticated surface ----------------

    async def _route_authenticated(
        self, method: str, path: str, query: str, body: bytes,
        head: str, peer_ip: str,
    ) -> bytes:
        """Everything behind the password, including the login flow itself."""
        secure = self._is_secure(head)
        address = self.client_address(head, peer_ip)
        session = self._session(head)

        # --- health is deliberately open ---
        # A load balancer cannot log in, and a health check that needs a
        # session is a health check that reports down forever. It exposes
        # nothing beyond liveness.
        if method == "GET" and path == "/healthz":
            return _json({"ok": True, "markets": len(self.desk.markets)})

        # --- login ---
        if path == "/login":
            if method == "GET":
                if session is not None:
                    return _redirect("/")
                return self._login_page()
            if method == "POST":
                password = _form_field(body, "password")
                new_session, error = self.auth.login(password, address)
                if new_session is None:
                    self.desk.log_audit("auth", "login_failed", {
                        "address": address, "error": error,
                    })
                    return self._login_page(error=error, status="401 Unauthorized")
                self.desk.log_audit("auth", "login", {"address": address})
                return _response(
                    "303 See Other", b"", "text/plain",
                    set_token=new_session.token, cookie_name="desk_session",
                    secure=secure, max_age=int(SESSION_TTL_S),
                    extra_headers="Location: /\r\n",
                )

        if path == "/logout" and method == "POST":
            if session is not None:
                self.auth.logout(session.token)
                self.desk.log_audit("auth", "logout", {"address": address})
            return _response(
                "303 See Other", b"", "text/plain",
                clear_token=True, cookie_name="desk_session",
                extra_headers="Location: /login\r\n",
            )

        # --- everything else needs a session ---
        if session is None:
            if path in ("/", "/index.html") and method == "GET":
                return _redirect("/login")
            return _json({"error": "not signed in"}, "401 Unauthorized")

        # --- CSRF on anything that changes state ---
        if method in STATE_CHANGING:
            presented = _header(head, "x-csrf-token")
            if not self.auth.check_csrf(session, presented):
                self.desk.log_audit("auth", "csrf_rejected", {
                    "address": address, "path": path,
                })
                return _json({"error": "bad or missing CSRF token"}, "403 Forbidden")

        out = await self._route(method, path, body)
        # The page needs its CSRF token, and /api/state is what it polls, so
        # the token rides along there rather than needing its own endpoint.
        if path == "/api/state" and method == "GET":
            try:
                payload = json.loads(out.split(b"\r\n\r\n", 1)[1])
                payload["csrf"] = session.csrf
                payload["auth"] = {"enabled": True}
                out = _json(payload)
            except (ValueError, IndexError):
                pass
        return out

    def _login_page(self, *, error: str | None = None, status: str = "200 OK") -> bytes:
        try:
            html = LOGIN_PAGE.read_text()
        except OSError:
            return _json({"error": f"login page missing at {LOGIN_PAGE}"},
                         "500 Internal Server Error")
        # Substituted rather than templated -- one string, and it is escaped.
        message = ""
        if error:
            safe = (
                error.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            message = f'<p class="err">{safe}</p>'
        html = html.replace("<!--ERROR-->", message)
        return _response(status, html.encode(), "text/html; charset=utf-8")

    async def _route(self, method: str, path: str, body: bytes) -> bytes:
        if method == "GET" and path in ("/", "/index.html"):
            try:
                return _response("200 OK", PAGE.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                return _json({"error": f"page missing at {PAGE}"}, "500 Internal Server Error")

        if method == "GET" and path == "/api/state":
            return _json(self.desk.snapshot())

        if method == "GET" and path == "/healthz":
            return _json({"ok": True, "markets": len(self.desk.markets)})

        if method == "POST":
            # A sandbox instance is reachable by people who are not the
            # operator, so the credential and arming routes refuse at the HTTP
            # layer as well as inside the desk. Two independent refusals rather
            # than one, checked before the body is even parsed, because this is
            # the boundary that matters.
            if self.desk.sandbox and path in SANDBOX_FORBIDDEN:
                from .desk import SANDBOX_REFUSAL

                return _json({"ok": False, "error": SANDBOX_REFUSAL}, "403 Forbidden")

            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return _json({"error": "invalid JSON"}, "400 Bad Request")

            if path == "/api/trading":
                want = bool(payload.get("enabled"))
                if want and self.desk.kill.engaged:
                    return _json(
                        {"error": "kill switch is engaged"}, "409 Conflict"
                    )
                self.desk.enabled = want
                self.desk.log_audit("operator", "trading", {"enabled": want})
                return _json(self.desk.snapshot())

            if path == "/api/shadow":
                want = bool(payload.get("enabled"))
                self.desk.shadow = want
                self.desk.log_audit("operator", "shadow", {"enabled": want})
                return _json(self.desk.snapshot())

            if path == "/api/strategy":
                name = str(payload.get("strategy", ""))
                from ..strategy import strategy_names

                if name not in strategy_names():
                    return _json({"error": f"unknown strategy {name!r}"}, "400 Bad Request")
                self.desk.strategy = name
                self.desk.log_audit("operator", "strategy", {"strategy": name})
                return _json(self.desk.snapshot())

            if path == "/api/settings":
                detail = {}
                if "size" in payload:
                    self.desk.size = max(1, min(500, int(payload["size"])))
                    detail["size"] = self.desk.size
                if "target_c" in payload:
                    self.desk.target_c = max(1, min(90, int(payload["target_c"])))
                    detail["target_c"] = self.desk.target_c
                self.desk.log_audit("operator", "settings", detail)
                return _json(self.desk.snapshot())

            if path == "/api/kill":
                self.desk.kill.engage(
                    str(payload.get("reason") or "stopped from the desk"),
                    source="desk",
                )
                self.desk.enabled = False
                self.desk.log_audit("operator", "kill", {
                    "reason": str(payload.get("reason") or "stopped from the desk"),
                })
                return _json(self.desk.snapshot())

            if path == "/api/resume":
                self.desk.kill.release()
                self.desk.log_audit("operator", "resume", {})
                return _json(self.desk.snapshot())

            if path == "/api/reset":
                self.desk.positions.clear()
                self.desk._taken.clear()
                self.desk.log_audit("operator", "reset", {})
                return _json(self.desk.snapshot())

            if path == "/api/arm/step-a":
                result = self.desk.arm_step_a(str(payload.get("phrase", "")))
                return _json({**result, "state": self.desk.snapshot()})

            if path == "/api/arm/step-b":
                result = self.desk.arm_step_b(str(payload.get("phrase", "")))
                return _json({**result, "state": self.desk.snapshot()})

            if path == "/api/arm/disarm":
                self.desk.disarm()
                return _json(self.desk.snapshot())

            if path == "/api/connect":
                result = self.desk.connect_keys(
                    str(payload.get("key_id", "")),
                    str(payload.get("private_key_pem", "")),
                )
                status = "200 OK" if result.get("ok") else "400 Bad Request"
                return _json({**result, "state": self.desk.snapshot()}, status)

            if path == "/api/health":
                result = await self.desk.health()
                return _json({**result, "state": self.desk.snapshot()})

            if path == "/api/reconcile":
                result = await self.desk.reconcile()
                return _json({**result, "state": self.desk.snapshot()})

        return _json({"error": "not found"}, "404 Not Found")
