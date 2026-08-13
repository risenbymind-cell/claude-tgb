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
from pathlib import Path

from .desk import Desk

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
PAGE = Path(__file__).resolve().parent.parent.parent / "site" / "desk.html"


def _response(status: str, body: bytes, content_type: str) -> bytes:
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        # The desk is a control surface for something that spends money. No
        # caching, no framing, no sniffing.
        "Cache-Control: no-store\r\n"
        "X-Frame-Options: DENY\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body


def _json(obj, status: str = "200 OK") -> bytes:
    return _response(status, json.dumps(obj).encode(), "application/json")


class DeskServer:
    def __init__(self, desk: Desk, host: str = "127.0.0.1", port: int = 8787) -> None:
        self.desk = desk
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            log.warning(
                "Desk bound to %s -- this port can place orders and holds "
                "decrypted credentials. Put authentication in front of it.",
                self.host,
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
            method, path, _ = head.split("\r\n", 1)[0].split(" ", 2)
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
            out = await self._route(method, path.split("?")[0], body)
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
                return _json(self.desk.snapshot())

            if path == "/api/strategy":
                name = str(payload.get("strategy", ""))
                from ..strategy import strategy_names

                if name not in strategy_names():
                    return _json({"error": f"unknown strategy {name!r}"}, "400 Bad Request")
                self.desk.strategy = name
                return _json(self.desk.snapshot())

            if path == "/api/settings":
                if "size" in payload:
                    self.desk.size = max(1, min(500, int(payload["size"])))
                if "target_c" in payload:
                    self.desk.target_c = max(1, min(90, int(payload["target_c"])))
                return _json(self.desk.snapshot())

            if path == "/api/kill":
                self.desk.kill.engage(
                    str(payload.get("reason") or "stopped from the desk"),
                    source="desk",
                )
                self.desk.enabled = False
                return _json(self.desk.snapshot())

            if path == "/api/resume":
                self.desk.kill.release()
                return _json(self.desk.snapshot())

            if path == "/api/reset":
                self.desk.positions.clear()
                self.desk._taken.clear()
                return _json(self.desk.snapshot())

        return _json({"error": "not found"}, "404 Not Found")
