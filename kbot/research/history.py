"""Kalshi's own history, for testing a plan on markets it was not fitted to.

The local recordings are 18 settled markets, which is not enough for a search
over thousands of plans to mean anything. But Kalshi keeps its settled markets
and serves one-minute candlesticks for each, and both are public. That is
enough to score a specific plan over hundreds of windows it never saw.

This is not a substitute for the recorder. Two things are genuinely worse here:

* **One-minute resolution.** The recorder samples about once a second. A candle
  reports open/high/low/close over a minute, so an entry is priced at a minute
  boundary and the exit scan cannot see a target touched and left within the
  same minute except through the high. Both effects flatter a plan slightly --
  a target is more likely to register as reached, and slippage between the
  decision and the fill is invisible.
* **No depth.** A candle has a bid and an ask but no size behind them, so a
  fill is assumed available. On a thin 15-minute book that is optimistic.

Both caveats point the same way: results here are an **upper bound**. A plan
that loses money on this data would lose more in reality, which is exactly
what makes it useful for refutation.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from datetime import datetime
from pathlib import Path

import httpx

from ..kalshi.prices import dollars_to_dc
from .search import ENTRY_TIMES, Market, _classify

log = logging.getLogger(__name__)

#: Kalshi's public read endpoints need no credentials.
PROD_REST = "https://external-api.kalshi.com/trade-api/v2"

#: Requests in flight while fetching candlesticks. Deliberately modest: this is
#: someone else's public API being asked for hundreds of windows, and nothing
#: here is urgent.
CONCURRENCY = 2

#: Candle width, in minutes. One is the finest Kalshi offers.
PERIOD_INTERVAL = 1


#: Kalshi rate-limits public reads, and a 429 is a request to wait rather than
#: a failure. Backing off and retrying is the difference between 63 markets and
#: the several hundred the analysis needs.
MAX_RETRIES = 6
BACKOFF_BASE_S = 1.5


def _ts(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


async def _get(
    client: httpx.AsyncClient, url: str, params: dict
) -> httpx.Response | None:
    """GET with backoff on rate limiting and transient server errors."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            if attempt == MAX_RETRIES - 1:
                log.warning("%s: %s", url, exc)
                return None
            await asyncio.sleep(BACKOFF_BASE_S * (2**attempt))
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES - 1:
                log.warning("%s: gave up on %s", url, resp.status_code)
                return resp
            # Honour Retry-After when offered; otherwise exponential.
            wait = BACKOFF_BASE_S * (2**attempt)
            header = resp.headers.get("retry-after")
            if header:
                try:
                    wait = max(wait, float(header))
                except ValueError:
                    pass
            await asyncio.sleep(wait)
            continue
        return resp
    return None


async def fetch_settled(
    client: httpx.AsyncClient, series: str, want: int
) -> list[dict]:
    """Settled markets for one series, newest first, following the cursor."""
    out: list[dict] = []
    cursor: str | None = None
    while len(out) < want:
        params: dict = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = await _get(client, f"{PROD_REST}/markets", params)
        if resp is None or resp.status_code != 200:
            log.warning("%s: markets returned %s", series,
                    "no response" if resp is None else resp.status_code)
            break
        payload = resp.json()
        batch = payload.get("markets") or []
        for market in batch:
            # `result` is the settled side. Anything else -- void, or still
            # settling -- cannot score a prediction.
            if market.get("result") in ("yes", "no"):
                out.append(market)
        cursor = payload.get("cursor")
        if not cursor or not batch:
            break
    return out[:want]


async def fetch_candles(
    client: httpx.AsyncClient, series: str, market: dict
) -> list[dict]:
    open_ts, close_ts = _ts(market["open_time"]), _ts(market["close_time"])
    resp = await _get(
        client,
        f"{PROD_REST}/series/{series}/markets/{market['ticker']}/candlesticks",
        {
            "start_ts": open_ts - 60,
            "end_ts": close_ts + 60,
            "period_interval": PERIOD_INTERVAL,
        },
    )
    if resp is None or resp.status_code != 200:
        return []
    return resp.json().get("candlesticks") or []


def _dc(block: dict | None, key: str = "close_dollars") -> int | None:
    if not block:
        return None
    raw = block.get(key)
    if raw in (None, ""):
        return None
    try:
        value = dollars_to_dc(raw)
    except Exception:  # noqa: BLE001
        return None
    # 0 and 1000 are settlement, not a tradeable quote.
    return value if 0 < value < 1000 else None


def to_market(series: str, market: dict, candles: list[dict]) -> Market | None:
    """Turn candlesticks into the same shape the search already scores.

    Reusing `search.Market` matters: the plan is then scored by exactly the
    function that found it, so a difference in result cannot be a difference in
    interpretation.
    """
    close_ts = _ts(market["close_time"])
    frames: list[tuple] = []
    mids: list[float] = []

    for candle in candles:
        end = candle.get("end_period_ts")
        if end is None:
            continue
        seconds_to_close = float(close_ts - end)
        # Drop the settlement candle and anything after it: at expiry the quote
        # is the outcome, and using it would be reading the answer.
        if seconds_to_close <= 0:
            continue
        yes_bid = _dc(candle.get("yes_bid"))
        yes_ask = _dc(candle.get("yes_ask"))
        if yes_bid is None or yes_ask is None:
            continue
        # A NO quote is the mirror of the YES book.
        no_bid = 1000 - yes_ask
        no_ask = 1000 - yes_bid
        mid = (yes_bid + yes_ask) / 2
        mids.append(mid)
        frames.append((seconds_to_close, mid, yes_ask, no_ask, yes_bid, no_bid))

    if len(frames) < 8:
        return None
    frames.sort(key=lambda f: -f[0])

    built = Market(
        ticker=market["ticker"],
        coin=series.replace("KX", "").replace("15M", ""),
        outcome=market["result"],
        frames=frames,
        regime=_classify(mids),
    )
    # Position in the window's range at each entry time, using only what was
    # visible at that instant -- the same rule the recorded path uses.
    for at in ENTRY_TIMES:
        best = None
        for i, frame in enumerate(frames):
            gap = abs(frame[0] - at)
            # Half a candle: with one-minute data the nearest observation to a
            # given second can be up to 30s away.
            if gap <= 30.0 and (best is None or gap < best[0]):
                best = (gap, i, frame[1])
        if best is None:
            continue
        _, idx, mid = best
        seen = [f[1] for f in frames[: idx + 1]]
        low, high = min(seen), max(seen)
        if high - low < 20:
            continue
        built.position_at[at] = (mid - low) / (high - low)
    return built


# ---------------- caching ----------------


def cache_path(directory: Path, series: str) -> Path:
    return Path(directory) / f"history-{series}.jsonl.gz"


def load_cached(directory: Path, series: str) -> list[Market]:
    path = cache_path(directory, series)
    if not path.exists():
        return []
    out: list[Market] = []
    with gzip.open(path, "rt") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.append(
                Market(
                    ticker=row["ticker"], coin=row["coin"], outcome=row["outcome"],
                    frames=[tuple(f) for f in row["frames"]],
                    regime=row["regime"],
                    position_at={float(k): v for k, v in row["position_at"].items()},
                )
            )
    return out


def save_cached(directory: Path, series: str, markets: list[Market]) -> None:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with gzip.open(cache_path(directory, series), "wt") as fh:
        for m in markets:
            fh.write(json.dumps({
                "ticker": m.ticker, "coin": m.coin, "outcome": m.outcome,
                "frames": [list(f) for f in m.frames], "regime": m.regime,
                "position_at": {str(k): v for k, v in m.position_at.items()},
            }) + "\n")


async def harvest(
    directory: Path,
    series_list: list[str],
    per_series: int,
    *,
    refresh: bool = False,
    progress=None,
) -> list[Market]:
    """Fetch (or load) settled markets with candlesticks, per series."""
    everything: list[Market] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for series in series_list:
            if not refresh:
                cached = load_cached(directory, series)
                if len(cached) >= per_series:
                    if progress:
                        progress(series, len(cached), cached=True)
                    everything.extend(cached[:per_series])
                    continue

            settled = await fetch_settled(client, series, per_series)
            gate = asyncio.Semaphore(CONCURRENCY)

            async def one(market: dict, series=series) -> Market | None:
                async with gate:
                    candles = await fetch_candles(client, series, market)
                return to_market(series, market, candles)

            built = [m for m in await asyncio.gather(
                *(one(m) for m in settled)
            ) if m is not None]
            if built:
                save_cached(directory, series, built)
            if progress:
                progress(series, len(built), cached=False)
            everything.extend(built)
    return everything
