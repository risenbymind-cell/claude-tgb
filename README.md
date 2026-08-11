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

Your funds stay in your own Kalshi account. The bot places orders through your
API key; it never holds, moves, or withdraws money.

---

## Setup

**[SETUP.md](SETUP.md) is the full walkthrough** — BotFather to first paper
trade, deployment, and payments. The short version:

```bash
git clone <this repo> && cd claude-tgb
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

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
    rest.py            REST client (markets, portfolio, orders)
    ws.py              websocket order-book feed + REST fallback, fair-value history
    orderbook.py       book state, imbalance, microprice
  strategy/
    base.py            MarketContext / Signal / Strategy protocol
    directional.py     Drift / Fade / Hammer — replace these with your own
  engine/
    discovery.py       finds the live 15-minute market per coin
    broker.py          PaperBroker and LiveBroker, same interface
    risk.py            the risk gate
    runner.py          the engine: tick loop, execution, position management
    spot.py            optional spot reference feed
  telegram/
    api.py             minimal Bot API client (long polling)
    ui.py              dashboard text and inline keyboards
    bot.py             commands, callbacks, guided credential entry
  payments/
    provider.py        provider protocol; NOWPayments + manual
    webhook.py         callback listener and /healthz
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

177 tests, no network required, covering:

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

---

## Deploying

`deploy/` has a hardened systemd unit, a Caddyfile that terminates TLS and
serves the landing page alongside the payment webhook, and a `fly.toml` pinned
to Chicago (`ord`) with scale-to-zero disabled. `docker compose up -d` works too.

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
