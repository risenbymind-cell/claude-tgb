"""Minimal async Telegram Bot API client (long polling).

Only the handful of methods this bot actually uses, so there is no framework to
keep in step with — and message delivery failures never take down the trade
engine that produced the message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    def __init__(self, description: str, code: int | None = None) -> None:
        super().__init__(description)
        self.description = description
        self.code = code

    @property
    def is_blocked(self) -> bool:
        """True when the user has blocked the bot or deleted the chat."""
        return self.code in (401, 403) or "bot was blocked" in self.description.lower()


class TelegramClient:
    def __init__(self, token: str, timeout: float = 40.0) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self._client = httpx.AsyncClient(timeout=timeout)
        self._offset: int | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        resp = await self._client.post(f"{self.base}/{method}", json=payload)
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(f"non-JSON response from {method}", resp.status_code)
        if not data.get("ok"):
            raise TelegramError(
                data.get("description", "unknown error"), data.get("error_code")
            )
        return data.get("result")

    # ---------------- sending ----------------

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = "HTML",
        disable_preview: bool = True,
    ) -> dict:
        return await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            link_preview_options={"is_disabled": disable_preview},
        )

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = "HTML",
    ) -> Any:
        try:
            return await self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            # Telegram rejects an edit that would not change anything; that is
            # a no-op for us, not an error worth surfacing.
            if "message is not modified" in exc.description.lower():
                return None
            raise

    async def answer_callback_query(
        self, callback_id: str, text: str | None = None, alert: bool = False
    ) -> Any:
        return await self.call(
            "answerCallbackQuery",
            callback_query_id=callback_id,
            text=text,
            show_alert=alert,
        )

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except TelegramError as exc:
            log.debug("Could not delete message: %s", exc)

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> Any:
        return await self.call(
            "setMyCommands",
            commands=[{"command": c, "description": d} for c, d in commands],
        )

    async def get_me(self) -> dict:
        return await self.call("getMe")

    # ---------------- receiving ----------------

    async def poll(self, poll_timeout: int = 25) -> AsyncIterator[dict]:
        """Yield updates forever, reconnecting through network errors."""
        backoff = 1.0
        while True:
            try:
                updates = await self.call(
                    "getUpdates",
                    offset=self._offset,
                    timeout=poll_timeout,
                    allowed_updates=["message", "callback_query"],
                )
                backoff = 1.0
            except (httpx.HTTPError, TelegramError) as exc:
                log.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            for update in updates or []:
                self._offset = update["update_id"] + 1
                yield update
