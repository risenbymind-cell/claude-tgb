# DirectionalBot

A Telegram bot that watches Kalshi's 15-minute crypto up/down markets (BTC, ETH,
SOL, XRP, DOGE, BNB, HYPE), reads the live order book, and either sends you a
signal or places the trade on your own Kalshi account.

Everything runs from one chat: pick your coins, set your size and risk caps,
start in a paper simulation, and switch to live when you're satisfied.

---

## Read this first

**The bundled strategies are reference implementations, not a validated edge.**

`kbot/strategy/directional.py` ships three presets — **Drift** (momentum),
**Fade** (mean reversion) and **Hammer** (sweep-follow) — built from ordinary
market-microstructure reasoning: order-book imbalance, short-horizon drift in
fair value, spread and liquidity filters. Every threshold is a named constant.
None of it has been backtested for you, and no claim is made that any preset is
profitable.

If you have a tested edge, this repo is the harness to run it in: implement the
`Strategy` protocol, register it, and it appears in the bot's strategy picker
with no other changes. If you don't, run paper mode and measure before you risk
money.

Kalshi trading carries real risk of loss. Nothing here is financial advice.

---

## What it does

| | |
|---|---|
| **Live order book** | Websocket feed of `orderbook_delta`, one shared book per market, with REST snapshot polling as a fallback. |
| **Market discovery** | Finds the current 15-minute window per coin every 20s by listing open markets in each series — no hard-coded ticker formats. |
| **Manual mode** | Every signal arrives in Telegram with side, price, confidence and the reasoning behind it. |
| **Auto mode** | The bot sizes, places, and manages the trade on your account. |
| **Exits** | A sell order goes in the moment an entry fills — entry + N cents, or an absolute target, raised automatically if it would not clear the round-trip fee. Unfilled positions settle with the window. |
| **Fees** | Kalshi's quadratic trading fee is modelled exactly and charged on both legs. All P/L is booked net, and gross/fees/net are reported separately. |
| **Risk caps** | Daily loss limit, max open exposure, balance floor, per-window and total position limits. |
| **Paper mode** | The same code path with a simulated broker, filling against real live prices. Default for every new user. |
| **Access keys** | Daily / weekly / monthly / lifetime tiers, minted by an admin or bought with crypto in chat. |
| **Payments** | `/buy` issues a crypto invoice and delivers the key automatically once payment confirms. Manual address + admin confirmation works with no third-party account. |
| **Website** | `site/index.html` — a self-contained landing page whose pricing buttons deep-link into the bot on the right tier. |
| **Results channel** | Resolved trades posted publicly with a running tally — **including losses**, because a wins-only feed is not evidence. Members opt in with `/share`; posts are anonymous. |
| **Research** | Record live books and replay any strategy over them, scored net of fees. |
| **Reconciliation** | The ledger is checked against Kalshi's real positions at startup and periodically: exits that filled while the bot was down, positions closed by hand, and partial fills are all detected. |
| **Rate limiting** | Client-side token buckets, metered separately for reads and writes, so a 429 never lands mid-order. |

Your funds stay in your own Kalshi account. The bot places orders through your
API key; it never holds, moves, or withdraws money.

---

## Setup

**[TUTORIAL.md](TUTORIAL.md) is the complete guide** — install, measure,
paper, live, sell. **[UPGRADE.md](UPGRADE.md) is the platform status — architecture, what is hardened, and what is not**.
**[SETUP.md](SETUP.md) is the shorter deployment walkthrough**, and
**[WINDOWS.md](WINDOWS.md) is the click-by-click version for a Windows PC** — BotFather to first paper
trade, deployment, and payments. The short version:

**No server at all** — push this repo to your GitHub, then
[render.com](https://render.com) → New → Blueprint → pick the repo. It prompts
for a Telegram bot token and your Telegram user ID, generates the `MASTER_KEY`
itself, and starts both the bot and the recorder with persistent disks. See
`render.yaml`.

**One command on your own server:**

```bash
git clone <this repo> /opt/directionalbot
sudo /opt/directionalbot/scripts/bootstrap.sh
```

It installs everything, runs a wizard for the three values only you can supply
(bot token, your Telegram ID, production or demo), generates the `MASTER_KEY`,
verifies with the preflight, and starts both services. Safe to re-run.

<details>
<summary>Or do it by hand</summary>

```bash
git clone <this repo> && cd claude-tgb
pip install -r requirements.txt
python -m kbot.tools setup     # writes .env, validating as it goes
```

Or write `.env` yourself:

1. **`TELEGRAM_BOT_TOKEN`** — from [@BotFather](https://t.me/BotFather).
2. **`MASTER_KEY`** — `python -m kbot.tools genkey`. This encrypts users' Kalshi
   private keys at rest. Back it up; losing it makes every stored credential
   unreadable.
3. **`ADMIN_IDS`** — your Telegram user ID, so you can mint access keys.
4. *(optional)* **`KALSHI_API_KEY_ID`** + **`KALSHI_PRIVATE_KEY_PATH`** — a
   platform key used **only** for the shared market-data websocket. Without it
   the bot falls back to REST order-book polling, which works but is slower.

Then:

```bash
python -m kbot
```

</details>

Or with Docker:

```bash
docker compose up -d
```

### Verify Kalshi series tickers

Kalshi renames and adds series over time, so the bot can re-derive the map from
the live API rather than trusting a hard-coded list:

```bash
python -m kbot.tools series    # 15-min series that exist right now
python -m kbot.tools markets   # the live window per coin
```

`series` prints a ready-to-paste `KALSHI_SERIES=` line. Note that the
15-minute markets are the `KX<COIN>15M` series — the similarly-named
`KX<COIN>D` series are the *hourly* directional markets.

Set `KALSHI_DEMO=true` to point everything at Kalshi's demo environment while
you're getting set up.

---

## Using it

**As an operator:**

```
/genkeys weekly 10     mint 10 weekly access keys
/keystats              redemption counts per tier
/grant <tg_id> monthly grant access directly, no key needed
/sales                 revenue by tier
/confirm [order-id]    settle a manual payment (no arg lists pending)
```

Keys can also be minted without Telegram: `python -m kbot.tools mintkeys weekly 10`.

**As a trader:**

```
/start                 open the dashboard
/buy                   buy access with crypto
/redeem YOUR-KEY       activate access
/connect               add your Kalshi API key (guided, key deleted from chat)
/positions             open positions and recent trades
/pnl [days]            realised P/L, paper and live split out
/stats [days]          breakdown by coin, direction and hour, plus drawdown
/share on|off          post your closed trades to the results channel
/status                feed health and the live markets right now
/stop                  stop trading
```

Everything else is buttons on the dashboard: mode, paper/live, coins, strategy,
size, entry band, exit rules and risk caps.

### Going live

1. Create an API key at kalshi.com → Account → API Keys. You get a **key ID** and
   an **RSA private key** file.
2. Send `/connect` and paste both. The bot verifies the key against Kalshi before
   saving it, encrypts it, and deletes your message from the chat.
3. Flip **Live** on the dashboard and press **Start trading**.

You can go back to paper at any time, and `/disconnect` removes your key entirely.

---

## How it's put together

```
kbot/
  config.py            environment-driven settings
  storage.py           sqlite: users, settings, encrypted creds, keys, trade ledger
  kalshi/
    auth.py            RSA-PSS request signing
    fees.py            Kalshi's quadratic fee model and breakeven maths
    throttle.py        read/write token buckets per rate-limit tier
    rest.py            REST client (markets, portfolio, orders)
    ws.py              websocket order-book feed + REST fallback, fair-value history
    orderbook.py       book state, imbalance, microprice
  strategy/
    base.py            MarketContext / Signal / Strategy protocol
    directional.py     Drift / Fade / Hammer — replace these with your own
  engine/
    discovery.py       finds the live 15-minute market per coin
    broker.py          PaperBroker and LiveBroker, same interface
    reconcile.py       ledger vs. Kalshi's actual positions
    risk.py            the risk gate
    runner.py          the engine: tick loop, execution, position management
    spot.py            optional spot reference feed
  telegram/
    api.py             minimal Bot API client (long polling)
    ui.py              dashboard text and inline keyboards
    bot.py             commands, callbacks, guided credential entry
    channel.py         public results channel (wins and losses)
  payments/
    provider.py        provider protocol; NOWPayments + manual
    webhook.py         callback listener and /healthz
  research/
    calibrate.py       is the market priced correctly? where an edge would live
    store.py           gzipped JSON-lines recording format
    recorder.py        live capture, including how each market settled
    replay.py          the backtest simulator
    report.py          statistics and the honesty caveats
site/index.html        the landing page
deploy/                systemd unit, Caddyfile, fly.toml
```

Four design decisions worth knowing:

- **Strategies are pure.** A strategy sees a `MarketContext` and returns a
  `Signal` or `None`. It cannot place orders, read the database, or know which
  user it runs for. That makes it testable in isolation and safe to share across
  users — the engine evaluates each (strategy, market) pair once per tick and
  applies each user's own risk settings to the result.

- **Paper mode is not a separate code path.** `PaperBroker` and `LiveBroker`
  implement the same three calls, so the only difference between a simulation and
  a real order is which object the engine is holding. Paper fills are modelled
  against the real book: an entry fills only at or above the live ask, capped by
  the size actually resting there.

- **Fees are first-class, not an afterthought.** Kalshi charges
  `ceil(0.07 x contracts x P x (1-P))` on every fill. Near the middle of the
  book — where these markets live — that is roughly **4c of round-trip drag**,
  so a "+3c" target is a guaranteed loss dressed as a win. The bot models the
  fee exactly (verified against real fills), charges it on entry and exit,
  books all P/L net, and raises any exit target that would not clear it.
  Settlement is free, so a position held to expiry pays the entry fee only.

- **Risk caps are enforced against the ledger, not memory.** Daily loss limits
  and exposure caps are recomputed from stored trades on every check, so
  restarting the bot cannot reset a user's limits.

- **Prices are integer deci-cents everywhere internally.** Kalshi's 15-minute
  markets tick in *tenths of a cent* below $0.10 and above $0.90, so whole
  cents cannot represent a real quote (a live DOGE market at 95.1c/97.9c is an
  ordinary sight). Conversion to and from the API's fixed-point dollar strings
  happens only in `kalshi/prices.py`; user-facing settings stay in cents.

### Adding your own strategy

```python
# kbot/strategy/mine.py
class MyStrategy:
    name = "mine"
    description = "What it does, shown in the picker."

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        # Prices and fair-value changes are deci-cents: 20 == 2 cents.
        if ctx.book.imbalance() > 0.5 and (ctx.fv_change_20s or 0) > 20:
            return Signal(
                coin=ctx.coin, ticker=ctx.ticker, side="yes",
                confidence=0.7, price_dc=ctx.book.best_ask("yes"),
                reason="why this fired",
            )
        return None
```

Remember the fee floor when choosing a target: `kbot.kalshi.fees.breakeven_cents`
tells you how far price must move before a round trip is worth taking.

Register it in `kbot/strategy/__init__.py` and it shows up on the dashboard.

---

## Tests

```bash
pip install pytest pytest-asyncio
python -m pytest
```

279 tests, no network required, covering:

- price/unit conversion and the order-book maths
- the fee model, pinned to fee figures read off real fills
- all three strategies and every filter
- the V2 order translation — where a sign error would silently invert every
  DOWN trade
- storage, access keys, the risk gate, the paper broker, market discovery
- the dashboard's settings logic
- payments: signature verification, and that one payment issues exactly one key
  no matter how many times the provider retries its callback
- the full engine loop end to end against injected market state — entry, exit,
  settlement, risk blocks, access enforcement
- record/replay round trips, and the replay arithmetic against hand-computed
  answers
- calibration, including that it can tell an efficiently priced market from a
  rigged one
- configuration validation, and that a bad value exits 2 without a traceback
- the setup wizard: that it validates before writing, never clobbers an
  existing `.env`, and writes it chmod 600
- rate-limit pacing, and reconciliation against every divergence it can find
- that the results channel posts losses by default

---

## Measuring the strategies

There is no point selling signals you have not measured. `kbot.research` records
live order books and replays any strategy over them, scored net of fees:

```bash
python -m kbot.research record --interval 1        # capture (run for days)
python -m kbot.research days                       # what you have
python -m kbot.research calibrate                   # is the market priced right?
python -m kbot.research replay --strategy drift --size 10
python -m kbot.research sweep --strategy all --targets 5,8,12,20,30
python -m kbot.research tune --win-rate 92          # what a win-rate target costs
```

### Start with `calibrate`

Before testing any strategy, ask whether the market is priced correctly: for
every market seen trading at price P, what fraction settled YES? If `actual`
tracks `implied`, there is no free edge in buying a price band and anything you
earn must come from timing inside the window. A persistent gap is where a real
edge would live.

It is also the fraud detector. A mean absolute gap above ~15% on real data would
be extraordinary; on synthetic data it usually means the generator's price and
its settlement share a source, and a strategy tested on it has learned the
generator rather than the market.

### Two numbers get called "win rate"

`tune` reports both, because the gap between them is the whole game:

| | |
|---|---|
| **hit%** | the trade reached its exit target — what gets screenshotted |
| **win%** | the trade actually made money after fees — what pays you |

Shrink the profit target and hit% climbs toward 100% while every one of those
trades loses money, because the round trip costs ~4c of movement near mid-book.
Optimise **NET**, then report whatever win rate honestly comes with it.

The replay reports trades, win rate, gross, fees, net, average edge per trade
and max drawdown — overall and per coin — plus a rough t-statistic so a
promising-looking result over 20 trades is labelled as noise rather than a
discovery.

It records how each market settled, because a strategy that holds to expiry
cannot be scored without that, and dropping those trades would bias every
number toward whatever exits happened to fill early. Trades whose settlement
was never captured are reported as unresolved rather than counted.

What it does not model: market impact from your own order, and queue position
on resting exits (an exit only fills when a bid actually reaches the target,
which is the pessimistic assumption). Watch for the trap the harness makes
obvious — a **high win rate with a negative net**, which is what a small profit
target plus full-stake losses at expiry produces.

## The results channel

Set `RESULTS_CHAT_ID` and the bot posts every resolved trade to a public
channel with a running tally attached.

That tally counts losses. `RESULTS_POST_LOSSES` defaults to true and should stay
that way — a feed of nothing but wins is not evidence, anyone who trades works
that out quickly, and a verifiable record is a stronger claim than a highlight
reel. Members opt in for their own trades with `/share on`; posts carry no
username, account detail or size attribution.

## Deploying

CI runs the full suite on every push, plus three checks that guard the
deployment contract: every module imports on the runtime dependencies alone, a
bad config exits `2` rather than crash-looping, and every environment variable
the code reads is documented in `.env.example`. The second job builds the Docker
image and confirms it fails cleanly on a bad config.

Run the preflight first — it validates config, checks the database opens with
your `MASTER_KEY`, confirms Kalshi and Telegram are reachable, and exits
non-zero if anything is broken:

```bash
python -m kbot.tools doctor
```

Configuration problems exit with code `2` and print one line naming the
variable, and the service units carry `RestartPreventExitStatus=2` so a typo in
`.env` stops the service rather than crash-looping.

`deploy/` has a hardened systemd unit, a Caddyfile that terminates TLS and
serves the landing page alongside the payment webhook, and a `fly.toml` pinned
to Chicago (`ord`) with scale-to-zero disabled. `docker compose up -d` works too.

`deploy/directionalbot-recorder.service` runs the market-data recorder as its
own service, so data collects from day one whether or not the bot is trading.

The bot exposes `/healthz` on port 8080 for health checks. It only needs an
inbound port at all if you take payment callbacks.

## Operational notes

- `data/` holds the sqlite database: users, access keys and the trade ledger.
  Back it up. Risk caps are enforced from it.
- Never commit `.env`, `MASTER_KEY`, or any `.pem`. They are gitignored.
- If a user blocks the bot, their trading is switched off automatically rather
  than left running unwatched.
- If a user's stored key stops authenticating, trading stops for that user and
  they are told why.
- The landing page's ticker numbers are illustrative placeholders, marked as
  such in the source. Wire them to a real feed or remove them.

## Disclaimer

This is trading-automation software, not financial, investment, or trading
advice. Signals and automation are provided as-is with no guarantee of profit or
performance. Backtested or past results do not predict future results. You are
solely responsible for your own trading decisions, your account, and your funds.
Only trade money you can afford to lose.
