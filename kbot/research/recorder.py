"""Capture live order books to disk so strategies can be tested against them.

Runs the same discovery and market feed the trading engine uses, so what gets
recorded is exactly what a live strategy would have seen — including the REST
polling fallback when no platform websocket credentials are configured.

It also records how each market settled. Without that, a replay cannot score a
position held to expiry, and quietly dropping those trades would bias every
result toward whatever exits happened to fill early.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

import httpx

from ..config import Settings
from ..engine.discovery import MarketDiscovery
from ..kalshi.auth import InvalidPrivateKey, Signer
from ..kalshi.rest import KalshiClient, KalshiError
from ..kalshi.ws import MarketFeed
from .store import RecordWriter

log = logging.getLogger(__name__)


class Recorder:
    def __init__(
        self,
        settings: Settings,
        directory: Path,
        coins: list[str] | None = None,
        interval: float = 1.0,
    ) -> None:
        self.settings = settings
        self.coins = [c.upper() for c in (coins or settings.coins)]
        self.interval = interval
        self.writer = RecordWriter(directory)

        self._http = httpx.AsyncClient(timeout=10.0)
        signer: Signer | None = None
        if settings.has_market_data_creds:
            try:
                signer = Signer.from_pem(settings.md_key_id, settings.md_private_key)
            except InvalidPrivateKey as exc:
                log.error("Platform key unusable (%s); recording via REST polling.", exc)

        self._signer = signer
        self.rest = KalshiClient(settings.rest_base, signer=signer, client=self._http)
        self.feed = MarketFeed(
            settings.ws_url,
            self.rest,
            signer,
            poll_interval=min(interval, settings.orderbook_poll_interval_s),
        )
        self.discovery = MarketDiscovery(self.rest, settings.series)

        #: Markets seen but not yet settled, so expiry can be resolved later.
        self._pending_settlement: dict[str, float] = {}
        self._settled: set[str] = set()
        self._empty_passes = 0
        self._stop = asyncio.Event()
        self._started_at = time.time()
        self._last_status = 0.0
        self._last_written = 0

    async def run(self, duration: float | None = None) -> None:
        self.feed.start()
        tasks = [
            asyncio.create_task(self._discovery_loop()),
            asyncio.create_task(self._sample_loop()),
            asyncio.create_task(self._settlement_loop()),
            asyncio.create_task(self._status_loop()),
        ]
        try:
            if duration:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=duration)
            else:
                await self._stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self.feed.stop()
            self.writer.close()
            await self._http.aclose()

    def stop(self) -> None:
        self._stop.set()

    #: Consecutive discovery passes returning nothing before the transport is
    #: rebuilt. Three passes at the default interval is about a minute -- long
    #: enough not to fire on a blip, short enough to lose almost no data.
    EMPTY_PASSES_BEFORE_RESET = 3

    #: How often to log that the recorder is alive and what it has captured.
    STATUS_INTERVAL_S = 300.0

    async def _status_loop(self) -> None:
        """Say something on success, periodically.

        A process that logs only on failure is indistinguishable, in a
        platform's log viewer, from a process that is not running at all --
        and "is it actually running" turned out to be the question that
        mattered most, after a day and a half of silence that nobody could
        attribute until the recordings were counted.

        The rate is included rather than just the total, because a total that
        stops climbing is the symptom worth spotting and a bare cumulative
        number hides it.
        """
        while True:
            await asyncio.sleep(self.STATUS_INTERVAL_S)
            now = time.time()
            written = self.writer.written
            elapsed = now - (self._last_status or self._started_at)
            recent = written - self._last_written
            rate = recent / elapsed if elapsed > 0 else 0.0
            uptime_h = (now - self._started_at) / 3600.0

            message = (
                "recording: %d markets live, %s records total "
                "(+%s in the last %.0fm, %.1f/s), up %.1fh"
            )
            args = (
                len(self.discovery.markets), f"{written:,}", f"{recent:,}",
                elapsed / 60, rate, uptime_h,
            )
            # Nothing captured since the last check is the failure this exists
            # to surface, so it is not logged at the same level as good news.
            if recent == 0:
                log.error(
                    message + " -- NOTHING CAPTURED since the last check", *args
                )
            else:
                log.info(message, *args)

            self._last_status = now
            self._last_written = written

    async def _discovery_loop(self) -> None:
        while True:
            try:
                markets = await self.discovery.refresh(self.coins)
                self.feed.set_tickers(m.ticker for m in markets.values())
                for market in markets.values():
                    self._pending_settlement.setdefault(
                        market.ticker, market.close_time
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad pass must not stop recording
                log.exception("Discovery failed")
                self._empty_passes += 1
            else:
                self._empty_passes = 0 if markets else self._empty_passes + 1

            # A recorder that stays alive while capturing nothing is worse than
            # one that crashes: the process looks healthy, the log looks calm,
            # and a day of data quietly does not exist. This happened -- nine
            # hours of "0 market(s) live" while Kalshi had nine markets open,
            # because the HTTP connections had died under a suspended container
            # and nothing noticed.
            if self._empty_passes >= self.EMPTY_PASSES_BEFORE_RESET:
                log.error(
                    "No markets discovered for %d consecutive passes -- "
                    "rebuilding the HTTP client. If this repeats, check "
                    "`python -m kbot.tools markets`.",
                    self._empty_passes,
                )
                await self._reset_transport()
                self._empty_passes = 0

            await asyncio.sleep(self.settings.discovery_interval_s)

    async def _reset_transport(self) -> None:
        """Replace the HTTP client and re-arm the feed.

        A pooled connection that died while the process was stopped does not
        raise on reuse in any way the discovery loop can distinguish from an
        empty result -- so recovery is a rebuild rather than a retry.
        """
        try:
            await self._http.aclose()
        except Exception:  # noqa: BLE001 - it is being discarded anyway
            pass
        self._http = httpx.AsyncClient(timeout=10.0)
        self.rest = KalshiClient(
            self.settings.rest_base, signer=self._signer, client=self._http
        )
        self.discovery.client = self.rest
        self.feed.rest = self.rest

    async def _sample_loop(self) -> None:
        last_report = time.time()
        while True:
            now = time.time()
            for coin, market in self.discovery.markets.items():
                book = self.feed.book(market.ticker)
                if book is None or book.is_stale:
                    continue
                self.writer.write_book(
                    ticker=market.ticker,
                    coin=coin,
                    open_time=market.open_time,
                    close_time=market.close_time,
                    yes=[[p, q] for p, q in sorted(book.yes.items(), reverse=True)],
                    no=[[p, q] for p, q in sorted(book.no.items(), reverse=True)],
                    t=now,
                )
            # Flush every pass: an in-progress recording should be readable
            # while it is still being written, not only after it stops.
            self.writer.flush()
            if now - last_report > 60:
                log.info(
                    "Recorded %d snapshots · %d market(s) live · feed %s",
                    self.writer.written,
                    len(self.discovery.markets),
                    "websocket" if self.feed.connected else "REST",
                )
                last_report = now
            await asyncio.sleep(self.interval)

    async def _settlement_loop(self) -> None:
        """Resolve markets once they close, so replay can score held positions."""
        while True:
            await asyncio.sleep(30)
            now = time.time()
            due = [
                ticker
                for ticker, close in self._pending_settlement.items()
                # Give the exchange a moment past the close to publish a result.
                if ticker not in self._settled and now > close + 30
            ]
            for ticker in due:
                try:
                    data = await self.rest.get_market(ticker)
                except KalshiError:
                    continue
                result = (data.get("result") or "").lower()
                if result not in {"yes", "no"}:
                    continue
                self.writer.write_settle(ticker, result, now)
                self._settled.add(ticker)
                self._pending_settlement.pop(ticker, None)
                log.info("Settled %s -> %s", ticker, result.upper())
            # Forget markets that never resolved so the dict cannot grow forever.
            for ticker, close in list(self._pending_settlement.items()):
                if now - close > 6 * 3600:
                    self._pending_settlement.pop(ticker, None)
