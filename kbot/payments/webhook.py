"""Webhook listener for payment callbacks, plus a health endpoint.

Deliberately built on `asyncio.start_server` rather than a web framework: it
serves exactly two routes, and adding a framework would be the largest
dependency in the project for no benefit.

Security posture: a callback is only acted on if the provider's signature
verifies. An unsigned or badly-signed request is answered 401 and does nothing —
otherwise anyone who guessed an order ID could mint themselves a key.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from .provider import PaymentProvider, PaymentUpdate

log = logging.getLogger(__name__)

OnPayment = Callable[[PaymentUpdate], Awaitable[None]]

MAX_BODY_BYTES = 64 * 1024


class WebhookServer:
    def __init__(
        self,
        provider: PaymentProvider,
        on_payment: OnPayment,
        host: str = "0.0.0.0",
        port: int = 8080,
        path: str = "/webhook/payment",
    ) -> None:
        self.provider = provider
        self.on_payment = on_payment
        self.host = host
        self.port = port
        self.path = path
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        log.info(
            "Webhook listening on %s:%s%s (health at /healthz)",
            self.host,
            self.port,
            self.path,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ---------------- HTTP ----------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request_line:
                return
            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                await self._respond(writer, 400, {"error": "bad request"})
                return
            method, target = parts[0], parts[1]

            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()

            length = int(headers.get("content-length", "0") or 0)
            if length > MAX_BODY_BYTES:
                await self._respond(writer, 413, {"error": "payload too large"})
                return
            body = await reader.readexactly(length) if length else b""

            path = target.split("?", 1)[0]
            if path == "/healthz":
                await self._respond(writer, 200, {"ok": True})
                return
            if path != self.path or method.upper() != "POST":
                await self._respond(writer, 404, {"error": "not found"})
                return

            await self._handle_payment(writer, body, headers)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001 - one bad request must not kill the server
            log.exception("Webhook request failed")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _handle_payment(
        self, writer: asyncio.StreamWriter, body: bytes, headers: dict[str, str]
    ) -> None:
        if not self.provider.verify_webhook(body, headers):
            log.warning("Rejected payment callback with an invalid signature")
            await self._respond(writer, 401, {"error": "bad signature"})
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            await self._respond(writer, 400, {"error": "bad json"})
            return

        update = self.provider.parse_webhook(payload)
        if update is None:
            await self._respond(writer, 202, {"ok": True, "ignored": True})
            return

        # Answer before doing the work: providers retry on a slow response, and
        # settlement is idempotent anyway.
        await self._respond(writer, 200, {"ok": True})
        try:
            await self.on_payment(update)
        except Exception:  # noqa: BLE001
            log.exception("Payment handling failed for order %s", update.order_id)

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        reason = {
            200: "OK",
            202: "Accepted",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            413: "Payload Too Large",
        }.get(status, "OK")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
