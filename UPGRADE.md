# Trading platform upgrade — status

Against a ten-phase specification. **Phase 1 is complete. Phases 2–10 are
not started**, except where earlier work already covered part of them.

This document is the honest ledger: what exists, what it defends against, what
it does not, and what a reasonable next step is. Nothing here claims an edge.

---

## 1 — Architecture

```
kbot/
├─ config.py       settings; resolves TRADING_MODE, the single mode authority
├─ safety.py       TradingMode · KillSwitch · clock-drift check
├─ redact.py       secret scrubbing for logs and exceptions
│
├─ kalshi/         auth (RSA-PSS)   rest (V2 orders, 429 backoff, throttle)
│                  ws (feed, seq validation, snapshot repair)
│                  orderbook  ·  prices (deci-cent boundary)  ·  fees
│
├─ engine/         runner (the loop, mode gate, kill check)
│                  broker (Paper | Live + IntentRecorder)
│                  risk  ·  discovery  ·  reconcile  ·  spot
│
├─ strategy/       base (MarketContext, Signal)
│                  directional (drift, fade, hammer)
│
├─ research/       recorder · store · replay · report · calibrate · signals
│
├─ telegram/       bot (commands) · ui · api · channel
└─ storage.py      SQLite: users, trades, access keys, invoices, order_intents
```

Money flows one way, and only one way:

```
feed → OrderBook → MarketContext → Strategy → Signal
                                       ↓
                              risk check → kill switch
                                       ↓
                        intent persisted → Broker → exchange
                                       ↓
                                    ledger
```

A strategy is a pure function of `MarketContext` and cannot place an order.
That part of the specification was already satisfied before this work.

### Units

All internal prices are **integer deci-cents** (1000 = $1.00), because Kalshi's
tick structure is 0.1¢ below $0.10 and above $0.90. Conversion to and from the
wire format happens in exactly one place, `kalshi/prices.py`. No float is used
for a price, size, fee, or P/L comparison.

---

## 2 — What was found

Ordered by severity. All four are fixed.

### Sequence numbers were recorded and never validated

`seq` was stored on every snapshot and delta and never checked. A single
dropped frame left a book that was no longer the exchange's — with plausible
levels and a tight spread, so nothing looked wrong. Every order priced off it
would have been priced off fiction.

This is the worst failure mode the system had, because it is invisible. The
recording format's own docstring already made this argument for why it stores
snapshots rather than deltas; the live path had no equivalent.

### One boolean stood between simulation and real money

`user.settings["paper"]` decided whether orders were real. `KALSHI_DEMO`
separately decided the API host. Two independent switches for one decision,
able to disagree — production credentials, production host, one mis-tapped
button.

### Blocked signals consumed the market's trade budget

Found while wiring the kill switch. The one-signal-per-window key was claimed
*before* the order was attempted, so releasing the kill switch mid-window left
that market dead until it rolled. A pause became an outage.

### A transport failure escaped the broker

`LiveBroker` caught `KalshiError` but not `httpx.HTTPError`, and the REST
client re-raises the transport exception once retries are exhausted. A network
failure mid-order propagated as an unhandled exception rather than a failed
order.

### Two things that turned out to be fine

Checked rather than assumed, and both were already correct:

- `client_order_id` is stable across the REST client's internal retries — the
  request body is built once, so a retry is idempotent rather than a second
  order.
- `replay` reads recordings as deci-cents, not wire dollars. (The new `signals`
  module got this wrong on first writing and now shares `replay`'s helper.)

---

## 3 — Phase 1: what now exists

### Three modes, and the process outranks the user

| Mode | Orders | Host | Stakes |
|---|---|---|---|
| `paper` *(default)* | none | **production** book | — |
| `demo-live` | real API calls | demo | demo funds |
| `production-live` | real API calls | production | **real money** |

`TRADING_MODE` is the only thing that selects a host. Reaching
`production-live` also requires:

```
ALLOW_PRODUCTION_ORDERS=I_UNDERSTAND_THESE_ARE_REAL_ORDERS
```

A sentence, not a boolean, so copying `true` from another variable cannot
satisfy it. Two independent settings means no single edit starts sending real
orders.

A `KALSHI_DEMO` that contradicts `TRADING_MODE` is a **startup error**, not a
silently resolved conflict. An unrecognised value is refused rather than
defaulted.

In a paper process, a user who selects live gets a paper broker **and is told
so once**. Downgrading silently would be its own hazard: someone reading a
dashboard that says LIVE over simulated fills draws exactly the wrong
conclusion from the results.

**Paper deliberately reads the production book.** The two non-production modes
answer different questions and neither substitutes for the other — paper asks
*is the strategy any good* and needs genuine liquidity; demo asks *does the
order code work* and its P/L is not evidence about anything.

### Kill switch

Four independent inputs, read fresh immediately before every order, never
cached:

| Input | Use |
|---|---|
| `KILL_SWITCH=1` | orchestrator, deploy pipeline |
| a `KILL` file beside the database | ops, shell access |
| `/kill` in Telegram (admin) | from a phone |
| runtime flag | circuit breakers |

`/kill` is unconditional — no confirmation step, because the value of a kill
switch is that it works first press for someone already alarmed. Turning it
back on is what should be deliberate.

`/resume` re-reads the switch after releasing and reports **"still engaged"**
when an environment switch remains, rather than claiming success. An unreadable
kill file counts as engaged: the failure direction here must be toward not
trading.

### Sequence validation and recovery

A delta whose sequence skips is **dropped, not applied** — applying it produces
a plausible book that is wrong, strictly worse than an obviously stale one. The
book is marked `desynced`, `is_stale` returns true (which is what stops it
pricing an order), and a repair loop fetches a fresh snapshot.

The repair runs beside the socket, not in the message handler, so one book's
REST round trip cannot stall every other market's deltas.

Duplicate and replayed frames are distinguished from gaps — nothing is lost, so
they are ignored without triggering recovery. Frames with no sequence number
are still applied.

### Durable order intent

Every live order writes an intent to `order_intents` **before** submission,
keyed by the `client_order_id` it will carry. The id is generated in the broker
rather than in the REST layer, so the value on disk is provably the value sent.
It is the table's primary key: a replayed submission cannot create a second row
on our side any more than it can on Kalshi's.

The distinction that matters:

| Outcome | Recorded as | Why |
|---|---|---|
| 4xx | `rejected` | the exchange answered and said no |
| 5xx or transport failure | `unknown` | **not** a refusal; the order may be live |
| filled | `filled` | with order id, count, fee |

At startup, every pending intent is checked against Kalshi's fills before the
ledger is compared to the account — an unrecorded fill is a position, and
reconciling first would report it as a mismatch and correct the wrong side of
it. An intent that could not be checked **stays pending**: failing to verify
something is not evidence it never happened.

### Preflight

`python -m kbot.tools doctor` additionally checks:

- mode and host agree (asserted, not assumed — this is what would have caught
  the old arrangement, and a regression here is silent)
- **clock drift** against the exchange's `Date` header, round-trip halved and
  subtracted. Signatures carry a millisecond timestamp, so drift surfaces as an
  authentication failure partway through a session — indistinguishable from a
  bad key, and it sends you looking in the wrong place. An unreachable host is
  reported as *unverified*, never as drift.
- kill-switch state, so a machine that will not trade says why before anyone
  starts debugging the strategy

### Secret redaction

The Telegram token is *inside* the API URL, and httpx logs request URLs at
INFO. The token reached the log on every call, and a traceback carried it again
in the exception text. Neither is a log statement anyone in this project wrote,
so no amount of care inside `kbot` would have prevented it.

A filter on the root logger **and its handlers** (a logger-level filter does
not see records from child loggers) scrubs formatted messages and exception
arguments. Exact values are registered once config resolves; shape patterns
catch Telegram tokens, PEM keys including the escaped-newline `.env` form,
Fernet keys, and Authorization headers.

Verified end to end against the exact httpx line and traceback that motivated
it.

---

## 4 — Schema migration

Additive only. `CREATE TABLE IF NOT EXISTS` runs at startup, so an existing
database gains `order_intents` on next boot with **no migration step and no
downtime**. No existing table or column changed. Rollback is safe: older code
ignores the new table.

---

## 5 — New environment variables

All documented in `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` · `demo-live` · `production-live` |
| `ALLOW_PRODUCTION_ORDERS` | unset | must equal `I_UNDERSTAND_THESE_ARE_REAL_ORDERS` |
| `KILL_SWITCH` | unset | `1`/`true` blocks all orders |
| `KALSHI_DEMO` | unset | **deprecated**; contradicting `TRADING_MODE` is an error |

---

## 6 — Tests

**378 passing**, up from 300 at the start of this work.

New files: `test_safety.py` (29), `test_sequence.py` (8), `test_redact.py` (14),
plus additions to `test_engine.py`, `test_storage.py`,
`test_broker_and_discovery.py`, `test_bot_settings.py`.

The two that matter most are in `test_signals.py`: a **planted signal must be
found**, and **pure noise must produce nothing**. Without the first, a tool that
always reports "no edge" passes every other check in the suite.

```bash
python -m pytest -q
```

---

## 7 — Safe paper-mode runbook

```bash
# 1. Confirm what you are about to run.
python -m kbot.tools doctor      # must say: mode · PAPER

# 2. Start the recorder. It needs no bot token and no Kalshi account.
python -m kbot.research record --interval 1

# 3. Start the bot.
python -m kbot
```

Windows: `powershell -ExecutionPolicy Bypass -File scripts\run-windows.ps1`,
and again with `-Recorder` in a second window. See
[WINDOWS.md](WINDOWS.md).

Then leave it alone. `/health` shows mode, kill switch, and book freshness.

---

## 8 — Production-live checklist

Do not begin until `signals` and `replay` show a positive net edge on an
out-of-sample period. There is no such evidence today.

1. `python -m kbot.research signals` reports ≥200 distinct windows and a
   feature clearing |t| = 2
2. `python -m kbot.research replay` shows positive **net** P/L on a period the
   parameters were not chosen on
3. Run `demo-live` first — this proves the order code, not the strategy
4. Reconcile clean: `/reconcile`, no pending intents
5. Set risk caps **before** switching mode
6. `TRADING_MODE=production-live` **and** `ALLOW_PRODUCTION_ORDERS=…`
7. `python -m kbot.tools doctor` — expect the PRODUCTION-LIVE warning and a
   clock in sync
8. Confirm `/kill` works before you need it
9. Smallest size the bot allows; watch the first several fills end to end

---

## 9 — Not done

Phases 2–10 remain. In priority order:

| Phase | Gap |
|---|---|
| 2 | Latency instrumentation (p50/p95/p99 per stage); bounded queues; monotonic clocks throughout; REST polling still runs alongside a healthy socket |
| 3 | Feature pipeline — roughly 8 of ~28 specified features exist, in `research/signals.py`, and only offline |
| 4 | Strategy ensemble; strategies do not return edge/slippage/adverse-selection estimates |
| 5 | Fair-value and calibration layer; Brier score, log loss, calibration curve |
| 6 | Execution engine — no post-only, cancel/replace, queue awareness, or maker/taker logic |
| 7 | ~6 of ~22 risk limits exist; no circuit breakers |
| 8 | Walk-forward validation; replay does not model queue position or latency |
| 9 | 3 of 15 specified commands (`/health`, `/kill`, `/resume`) |
| 10 | Property-based tests; no benchmark suite |

**No performance benchmark and no replay benchmark exist.** Both are
deliverables in the specification and both are absent — publishing a number I
have not measured would be the same failure as a fabricated backtest.

---

## 10 — Limitations and known risks

1. **No demonstrated edge.** The bundled strategies are reference
   implementations. Every result so far is either not significant or an
   artifact. Do not fund this.
2. **Insufficient data.** `signals` requires 200 distinct 15-minute windows and
   currently has ~1. Nine coins in the same quarter hour ride the same crypto
   tape — that is closer to one observation than nine, and the tool withholds
   all verdicts below the threshold for exactly that reason.
3. **REST polling still runs** beside the websocket. Correct but wasteful, and
   Phase 2 asks for it to stop.
4. **Market impact is not modelled** anywhere. Replay results at size are
   optimistic.
5. **Partial fills are reported, not managed.** Reconciliation surfaces them;
   nothing closes them automatically.
6. **The intent log is not yet consulted before submission.** It records and
   recovers; it does not yet refuse a duplicate in-flight intent.
7. **Single-process.** No leader election. Two instances against one account
   would double every position, and nothing detects that.
8. **The Docker image has never been built** — no daemon in the development
   environment. CI's docker job is its first real execution.
9. **Clock drift is checked at startup only.** Phase 2 asks for continuous
   monitoring.
10. **Paper fills are optimistic.** They assume the resting size at the touch
    is available and ignore queue position — the direction of that error is
    toward flattering results.
