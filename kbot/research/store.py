"""On-disk format for recorded market data.

One gzipped JSON-lines file per UTC day. Two record types:

* ``book``   — a full order-book snapshot for one market at one instant
* ``settle`` — how a market finally resolved

Full snapshots rather than deltas, deliberately. A delta stream that loses one
frame is silently wrong forever; a snapshot stream that loses one frame loses
one frame. Recording is cheap and disks are large — correctness of the replay is
what matters, because every number the strategy research produces comes from it.
"""

from __future__ import annotations

import gzip
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def day_key(ts: float | None = None) -> str:
    ts = ts if ts is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class BookRecord:
    t: float
    ticker: str
    coin: str
    open_time: float
    close_time: float
    yes: list
    no: list

    @property
    def seconds_to_close(self) -> float:
        return self.close_time - self.t

    @property
    def window_seconds(self) -> float:
        span = self.close_time - self.open_time
        return span if span > 0 else 900.0


@dataclass
class SettleRecord:
    t: float
    ticker: str
    result: str  # "yes" | "no"


class RecordWriter:
    """Appends records to the current day's file, rotating at UTC midnight."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._day: str | None = None
        self._fh = None
        self.written = 0

    def _rotate(self, ts: float) -> None:
        day = day_key(ts)
        if day == self._day and self._fh is not None:
            return
        self.close()
        self._day = day
        # Append mode so a restart mid-day extends the file instead of
        # truncating a morning's worth of recording.
        self._fh = gzip.open(self.directory / f"{day}.jsonl.gz", "at", encoding="utf-8")

    def write_book(
        self,
        *,
        ticker: str,
        coin: str,
        open_time: float,
        close_time: float,
        yes: list,
        no: list,
        t: float | None = None,
    ) -> None:
        t = t if t is not None else time.time()
        self._rotate(t)
        self._fh.write(
            json.dumps(
                {
                    "type": "book",
                    "t": round(t, 3),
                    "ticker": ticker,
                    "coin": coin,
                    "o": round(open_time, 3),
                    "c": round(close_time, 3),
                    # Levels are [price_deci_cents, contract_count] — already
                    # in the bot's internal units, so replay reconstructs the
                    # book exactly with no parsing round-trip to get wrong.
                    "yes": yes,
                    "no": no,
                }
            )
            + "\n"
        )
        self.written += 1

    def write_settle(self, ticker: str, result: str, t: float | None = None) -> None:
        t = t if t is not None else time.time()
        self._rotate(t)
        self._fh.write(
            json.dumps(
                {"type": "settle", "t": round(t, 3), "ticker": ticker, "result": result}
            )
            + "\n"
        )
        self.written += 1

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def read_records(
    directory: Path, since: str | None = None, until: str | None = None
) -> Iterator[dict]:
    """Yield every record in date order across the requested day files."""
    directory = Path(directory)
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.jsonl.gz")):
        day = path.name.split(".")[0]
        if since and day < since:
            continue
        if until and day > until:
            continue
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    # A truncated final line is normal if recording was killed
                    # mid-write; skip it rather than losing the whole day.
                    continue


def available_days(directory: Path) -> list[str]:
    directory = Path(directory)
    if not directory.exists():
        return []
    return sorted(p.name.split(".")[0] for p in directory.glob("*.jsonl.gz"))


def load_session(
    directory: Path, since: str | None = None, until: str | None = None
) -> tuple[dict[str, list[BookRecord]], dict[str, str]]:
    """Group recorded books by ticker, alongside each market's settlement.

    Returns ({ticker: [BookRecord in time order]}, {ticker: "yes"|"no"}).
    """
    books: dict[str, list[BookRecord]] = {}
    settled: dict[str, str] = {}
    for rec in read_records(directory, since, until):
        if rec.get("type") == "settle":
            settled[rec["ticker"]] = rec["result"]
            continue
        if rec.get("type") != "book":
            continue
        books.setdefault(rec["ticker"], []).append(
            BookRecord(
                t=rec["t"],
                ticker=rec["ticker"],
                coin=rec["coin"],
                open_time=rec["o"],
                close_time=rec["c"],
                yes=rec.get("yes") or [],
                no=rec.get("no") or [],
            )
        )
    for series in books.values():
        series.sort(key=lambda r: r.t)
    return books, settled
