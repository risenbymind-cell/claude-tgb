# Where this stands

A handoff note. Written so a fresh session — or you, in a month — can pick this
up without reading the whole history.

Branch: `claude/telegram-kalshi-bot-ciy8by`

---

## The one thing that matters

**No edge has been demonstrated, and there is not enough data to look for one.**

A grid of ~4,000 strategy plans was run over the recordings. The best scored
100% win rate and +$23.74. The same grid run against *shuffled settlements* —
identical prices and books, outcomes permuted — scored 100% and +$24.12, with
12/12 shuffles matching or beating the real win rate. The winners were what a
large search finds in 18 settled markets, not an edge.

That number is 18 because the recorder has **2% uptime**:

```
capture rate  1% of what 9 coins over 2 days could have produced
gaps          1, totalling 33.8h with nothing recorded
```

It was started, ran briefly, stopped, and was not running for the next day and
a half. Nothing else in the project can be concluded until this is fixed, and
it is a deployment problem rather than a code one.

**Next action: start the recorder somewhere that stays up.** See `RECORDER.md`.
It needs no credentials of any kind and runs with one command.

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
| **Self-tests** | Ten checks doing real operations against the live process |
| **Latency** | P50/P95/P99 per stage |
| **Public site** | Live at the github.io URL |

705 tests passing. `python -m pytest -q`

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
                  auth.py, selftest.py, latency.py
  research/       recorder.py, inventory.py, search.py, signals.py,
                  calibrate.py, replay.py
  engine/         broker.py (paper + live), discovery.py, runner.py
  strategy/       directional.py — five presets
  safety.py       TradingMode, KillSwitch, clock drift
site/             index.html, app.html, desk.html, login.html
docs/             generated from site/ by scripts/build-site.py
```

Read `DEPLOY.md` for hosting, `RECORDER.md` for the recorder, `UPGRADE.md` for
the architecture pass and the bugs it found.
