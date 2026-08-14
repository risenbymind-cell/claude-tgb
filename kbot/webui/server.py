"""A local HTTP server for the trading desk.

Built on `asyncio.start_server` to match the payment webhook and to keep the
dependency list at four packages. It serves one page and a handful of JSON
routes; a framework would be the largest dependency in the project.

**Binds to localhost by default, and refuses to do otherwise without being
told twice.** This process holds decrypted Kalshi credentials and can place
orders; exposing it on 0.0.0.0 hands that to anyone who can reach the port. The
`--host` flag exists for people who know they are putting a tunnel in front of
it, and it warns loudly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import socket
from pathlib import Path

from .desk import Desk

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
PAGE = Path(__file__).resolve().parent.parent.parent / "site" / "desk.html"


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


def _response(
    status: str, body: bytes, content_type: str, *, set_token: str | None = None
) -> bytes:
    cookie = ""
    if set_token:
        # SameSite=Strict so another site cannot drive the desk with the
        # browser's stored token. HttpOnly because no script needs to read it.
        cookie = (
            f"Set-Cookie: desk_token={set_token}; Path=/; "
            "HttpOnly; SameSite=Strict; Max-Age=604800\r\n"
        )
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        # The desk is a control surface for something that spends money. No
        # caching, no framing, no sniffing.
        "Cache-Control: no-store\r\n"
        "X-Frame-Options: DENY\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "Connection: close\r\n"
        + cookie
        + "\r\n"
    ).encode() + body


def _json(obj, status: str = "200 OK") -> bytes:
    return _response(status, json.dumps(obj).encode(), "application/json")


LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class DeskServer:
    """Serves the desk, and gates it whenever it is reachable off-machine.

    On localhost the operating system is the boundary: only this machine can
    connect, so a password would protect nothing. The moment the bind address
    leaves localhost that stops being true -- anyone on the Wi-Fi, including
    whatever else is on a cafe network, can reach a port that places orders.
    A token is then mandatory rather than optional, and is generated rather
    than chosen so there is no weak one.
    """

    def __init__(
        self,
        desk: Desk,
        host: str = "127.0.0.1",
        port: int = 8787,
        token: str | None = None,
    ) -> None:
        self.desk = desk
        self.host = host
        self.port = port
        self.local_only = host in LOCAL_HOSTS
        # Generated unconditionally when exposed. Never derived from anything
        # guessable, and never taken from configuration.
        self.token = token or (None if self.local_only else secrets.token_urlsafe(16))
        self._server: asyncio.AbstractServer | None = None

    def url(self, host: str | None = None) -> str:
        base = f"http://{host or self.host}:{self.port}/"
        return base + (f"?t={self.token}" if self.token else "")

    def _authorised(self, head: str, query: str) -> bool:
        if self.token is None:
            return True
        if f"t={self.token}" in query:
            return True
        for line in head.split("\r\n"):
            low = line.lower()
            if low.startswith("cookie:") and f"desk_token={self.token}" in line:
                return True
            if low.startswith("x-desk-token:"):
                # Constant-time so a timing signal cannot leak the token.
                if secrets.compare_digest(line.split(":", 1)[1].strip(), self.token):
                    return True
        return False

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        if not self.local_only:
            log.warning(
                "Desk reachable on %s:%d -- this port can place orders. "
                "Access requires the token in the printed link.",
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
            if not self._authorised(head, query):
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
                result = self.desk.reconcile()
                return _json({**result, "state": self.desk.snapshot()})

        return _json({"error": "not found"}, "404 Not Found")
