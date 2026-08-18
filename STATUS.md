# Where this stands

A handoff note. Written so a fresh session — or you, in a month — can pick this
up without reading the whole history.

Branch: `claude/telegram-kalshi-bot-ciy8by`

---

## The one thing that matters

**No edge exists here, and that is now measured rather than pending.**

A search of ~4,000 plans returned a best of 100% win rate, +$23.74. Tested on
**360 settled markets it was never fitted to**, the same plan scored 58.7% and
**−$83.39**. Across the whole grid on that data, **0 of 620 plans** with ≥30
trades finished positive.

The reason is structural, not a matter of searching harder:

- These markets settle on the **CF Benchmarks index**, not on Kalshi's book —
  the average of 60 one-second samples in the final minute.
- Kalshi's own price already tracks it to **69% accuracy ten minutes out** and
  **93% one minute out**. The best plan the search found was 58.7% accurate at
  the same instant: **less informative than the price it was trading against.**
- A spot-based model built from public data is a **worse forecast** than the
  market price (Brier 0.1785 vs 0.1743).
- The professional edge sits behind **licensed second-resolution index data**,
  which is the one input that cannot be reached publicly.

Full workings: `research/FINDINGS.md`, `WHAT_BOTS_ACTUALLY_DO.md`,
`SPOT_INDEX_TEST.md`, and `HOW_TO_FIND_ONE.md` for where to look instead.

**Where the evidence now points: the passive side of the book.** Transaction
costs explain **84%** of every loss recorded here — a taker pays 0.789c of
half-spread plus 1.093c of fee, and loses 2.243c. A resting quote is paid that
half-spread instead. See `research/MAKER.md`; the best cell absorbs 0.770c of
adverse selection before turning negative, and two cheap things gate it —
confirming Kalshi's maker fee, and running the recorder for order-book depth.

**Directional prediction is finished.** Four candidates have now failed, the
last of them at −$0.105/trade on 1,894 held-out markets after looking like
+$0.432 at t=+3.54. Both remaining avenues start with the recorder, which is
still at **2% uptime** — see `RECORDER.md`; it needs no credentials and one
command.

---

## What works

| Area | State |
|---|---|
| **Web desk** (`python -m kbot.webui`) | Nine tabs, all wired to real data |
| **Demo-live orders** | Real orders on Kalshi's demo exchange, durable intent log |
| **Hosting** | Password auth, sessions, CSRF, lockout; refuses public bind without a password |
| **Sandbox** (`--sandbox`) | Real market data, no credentials, no order path, safe to expose |
| **Recorder** | One-command deploy, heartbeat logging every 5 min |
| **Research tab** | Distance to a statistically meaningful verdict |
| **Self-tests** | Ten checks doing real operations, from the desk or `/selftest` |
| **Latency** | P50/P95/P99 per stage |
| **Out-of-sample testing** | `research/history.py` — scores a plan on Kalshi's own settled markets |
| **Multiple-comparison guard** | `bands --scan` reports the cell count and chance expectation with every result |
| **Maker economics** | `maker` — spread capture vs fee, and the adverse selection each cell can absorb |
| **Safety** | Single-instance lock; duplicate in-flight intents refused; kill switch with four inputs |
| **Container** | Built, run against live Kalshi, non-root, healthcheck verified |
| **Public site** | Live at the github.io URL |

757 tests passing. `python -m pytest -q`

---

## Outstanding, and why I could not finish them

**1. The recorder is not running.** Needs a host that does not sleep. Free
tiers idle out on no inbound traffic, and the recorder has none by design.

**2. `konneh.bot` has no DNS.** The site serves from the github.io URL. The
domain is a single switch — create `site/CNAME` containing the domain, rebuild,
push — but **set DNS first**: publishing a CNAME makes Pages 301 the working
URL to a host that does not resolve, which takes the site offline. I did this
once and had to undo it.

DNS records needed (verified against the live Pages hosts):

```
A     @   185.199.108.153, .109.153, .110.153, .111.153
AAAA  @   2606:50c0:8000::153, :8001::153, :8002::153, :8003::153
```

**3. The landing page's buttons go nowhere.** `site/index.html` has
`const BOT = "YourBotUsername"`. Set it, run `python scripts/build-site.py`,
commit.

**4. Production-live is wired but untested.** Same code path as demo, pointed
at a different host, gated behind two typed phrases plus two environment
variables. I could not test it without real money at risk.

**5. Phases 2–10 of the original spec are mostly unbuilt.** `UPGRADE.md` §9
carries the current state. The largest genuine gaps are the feature pipeline
(~8 of 28 features, offline only), the execution engine (no post-only,
cancel/replace or queue awareness), and ~6 of 22 risk limits with no circuit
breakers.

**6. No leader election.** `kbot/lock.py` *refuses* a second instance rather
than coordinating one, which is the right trade for a single-operator
deployment and wrong for a highly available one.

---

## Things worth not re-learning

- **Kalshi returns 403 to any request carrying a browser `Origin` header.** A
  web page cannot call it. That is why the desk is a local Python process the
  browser talks to, not a static site.
- **Prices are deci-cents** (integers, 0–1000) everywhere internally.
- **Hold-to-expiry at a +8¢ target needs a 92.2% win rate to break even.** A
  90% win rate still loses money — the fee is charged on both legs. Read the
  net column, never the win rate.
- **Stops made results worse** (−$16 to −$20 vs −$3.54). 73.9% of books have no
  bid on the losing side in the final 20 seconds, so a stop fills into the
  collapse. Positions are flattened 45s before expiry instead.
- **Aggregates have lied twice here.** An outcome allowlist silently dropped
  losing trades and showed a real −$3.54 loss as "100% win rate, +$7.19". A
  one-sided sample produced a fake 43.8% "edge". Both were found by dumping the
  underlying rows. Do not trust a summary without checking what it can hide.

---

## Layout

```
kbot/
  webui/          the desk: desk.py (state), server.py (HTTP),
                  auth.py, latency.py
  research/       recorder.py, inventory.py, history.py, search.py,
                  signals.py, calibrate.py, replay.py
  engine/         broker.py (paper + live), discovery.py, runner.py
  strategy/       directional.py — five presets
  safety.py       TradingMode, KillSwitch, clock drift
  selftest.py     live checks, shared by the desk and the bot
  lock.py         one trader per account
site/             index.html, app.html, desk.html, login.html
docs/             generated from site/ by scripts/build-site.py
```

Read `DEPLOY.md` for hosting, `RECORDER.md` for the recorder, `UPGRADE.md` for
the architecture pass and the bugs it found, and `research/` for the edge
question, which is answered rather than open.
