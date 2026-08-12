# DirectionalBot — the complete tutorial

Everything, in order: install it, prove the data path works, measure whether the
strategy is real, run it in paper, go live, sell access.

If you read one section, read [Part 4](#part-4--measuring-honestly). It is the
one that decides whether the rest is worth doing.

On Windows, and want the mechanics spelled out click by click?
[WINDOWS.md](WINDOWS.md) is this document's Part 1-3 for a Windows PC, with
no server involved.

---

## Contents

1. [Install and configure](#part-1--install-and-configure)
2. [Verify the market data](#part-2--verify-the-market-data)
3. [First run](#part-3--first-run)
4. [Measuring honestly](#part-4--measuring-honestly) ← the important one
5. [Paper trading](#part-5--paper-trading)
6. [Going live](#part-6--going-live)
7. [Selling access](#part-7--selling-access)
8. [The results channel](#part-8--the-results-channel)
9. [Deploying](#part-9--deploying)
10. [Writing your own strategy](#part-10--writing-your-own-strategy)
11. [Command reference](#command-reference)
12. [Troubleshooting](#troubleshooting)

---

## Part 1 — Install and configure

```bash
git clone <your repo> /opt/directionalbot
cd /opt/directionalbot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Three required values in `.env`:

```bash
# 1. From @BotFather on Telegram
TELEGRAM_BOT_TOKEN=123456:ABC-your-token

# 2. Generate this — it encrypts users' Kalshi keys at rest
.venv/bin/python -m kbot.tools genkey
MASTER_KEY=<paste the output>

# 3. Your Telegram user ID, from @userinfobot
ADMIN_IDS=123456789
```

**Back up `MASTER_KEY`.** Lose it and every stored Kalshi credential becomes
permanently unreadable — users would each have to reconnect.

Optional but worth it: a platform Kalshi API key (`KALSHI_API_KEY_ID` +
`KALSHI_PRIVATE_KEY_PATH`). It is used *only* for the shared market-data
websocket, never to place an order. Without it the bot falls back to REST
polling — that works, it is just slower to see the book move.

---

## Part 2 — Verify the market data

Kalshi renames and adds series. Never assume; check.

```bash
.venv/bin/python -m kbot.tools series
```

```
  BNB    KXBNB15M
  BTC    KXBTC15M
  DOGE   KXDOGE15M
  ...
KALSHI_SERIES={"BTC":"KXBTC15M","ETH":"KXETH15M",...}
```

A `*` marks a series that exists on the exchange but differs from your config.
Paste the printed `KALSHI_SERIES=` line into `.env` if anything moved.

> **The 15-minute markets are `KX<COIN>15M`.** The similarly named `KX<COIN>D`
> series are the *hourly* directional markets. Getting this wrong means trading
> a completely different product.

Then confirm live windows are visible:

```bash
.venv/bin/python -m kbot.tools markets
```

```
BTC   KXBTC15M-26AUG111400-00    closes in 7m35s
ETH   KXETH15M-26AUG111400-00    closes in 7m34s
...
```

Tickers and countdowns mean the data path works. Some low-volume coins (ADA,
BCH, TON) do not have an open window at all hours — that is normal.

---

## Part 3 — First run

Preflight first. One command checks config, database, ports, Kalshi and
Telegram, and exits non-zero if anything is broken:

```bash
.venv/bin/python -m kbot.tools doctor
```

Exit codes: `0` ready, `1` something is broken, `2` the configuration is
invalid. A bad value never produces a stack trace — it names the variable and
what was expected, and the service units carry `RestartPreventExitStatus=2` so
a typo in `.env` stops the service instead of crash-looping.

```bash
.venv/bin/python -m kbot
```

You should see `Connected to Telegram as @yourbot`. Message the bot `/start`.

As admin, give yourself access:

```
/genkeys lifetime 1
/redeem L-XXXXXXXXXXXXXXXX
```

The dashboard opens. You are in **paper mode** by default with **manual**
signals. Pick coins, press **Start trading**, and signals will arrive as the
strategy finds setups.

Nothing reaches Kalshi until you explicitly connect a key and switch to Live.

---

## Part 4 — Measuring honestly

This is the part that matters. The bundled strategies are documented reasoning,
not a proven edge. Here is how to find out what you actually have.

### 4.1 Record real market data

```bash
.venv/bin/python -m kbot.research record --interval 1
```

Leave it running. It captures every order book once per second **and records how
each market settled** — without settlements you cannot score a position held to
expiry, and silently dropping those trades biases every result.

Roughly 25 MB per day gzipped for all coins. Run it for several days.

```bash
.venv/bin/python -m kbot.research days      # what you have
```

You can replay while it is still recording; the reader handles the
partially-written file.

### 4.2 Ask the most important question first

Before testing any strategy, ask whether the market is priced correctly:

```bash
.venv/bin/python -m kbot.research calibrate
```

```
price band      markets   implied   actual      gap   EV/contract
────────────────────────────────────────────────────────────────
30-40c               57      35%      33%      -2%        -0.032
40-50c              102      45%      46%      +1%        -0.008
50-60c              105      55%      54%      -1%        -0.028
```

For every market seen trading at price P, what fraction actually settled YES?

- **`actual` tracks `implied`** → the market is efficiently priced. There is no
  free edge in simply buying a price band. Anything you earn has to come from
  timing *inside* the window, and has to clear the fee twice.
- **A persistent gap** → that band is mispriced, and that is where a real edge
  lives. The `*` marks gaps beyond two standard errors.

This is also your fraud detector. A mean absolute gap above ~15% on real data
would be an extraordinary finding. On synthetic data it usually means the
generator's price and its settlement share a source — a strategy tested on that
has learned the generator, not a market.

### 4.3 Replay a strategy

```bash
.venv/bin/python -m kbot.research replay --strategy drift --size 10
```

```
 REPLAY · strategy=drift · size=10 · target=+8c · min-conf=60%
 120 market windows · 36,000 snapshots · 113 trades taken

             trades      win       gross      fees         NET       avg      maxDD
ALL             113    75.2%     -$33.85    $32.95     -$66.80    -$0.59     $79.05

 Edge per trade: -$0.59 net of fees, over 113 trades.
 t ≈ -2.67 — plausibly real, keep testing.
```

**Read the NET column, not the win column.** That run has a 75% win rate and
loses money.

Everything is net of Kalshi's real fee. The simplifications are stated in the
output and in `replay.py`: no market-impact model, and exits fill only when a
later snapshot actually shows a bid at or above the target (pessimistic on
purpose).

### 4.4 Why chasing a win rate destroys you

You asked to tune until 92%. Here is what happens when you do.

```bash
.venv/bin/python -m kbot.research tune --win-rate 92 --by-hit-rate --ignore-fee-floor
```

```
strategy  target   conf  trades   hit%   win%        NET  avg/trade
 drift       +1c   0.45     385  73.5%   6.5%    -852.00     -2.213
★drift      +30c   0.45     385   2.3%  56.6%    +481.50     +1.251

Highest target-hit rate: 73.5% hit, but only 6.5% made money → net -852.00
Highest NET:             56.6% win → net +481.50

That 67-point gap is the fee. 74% of those trades reached their exit
target and still lost money, because the target was smaller than the
round trip cost. A screenshot of the hit rate would be true and worthless.
```

Two different numbers get called "win rate":

| | what it means |
|---|---|
| **hit%** | the trade reached its exit target — what gets screenshotted |
| **win%** | the trade actually made money after fees — what pays you |

Shrink the target and hit% climbs toward 100%. Every one of those trades can
still lose money, because Kalshi's fee costs about **4c of round-trip movement**
near mid-book. A +2c target is a guaranteed loss that *looks* like a win.

The bot refuses to do this to you: `exit_price_for` raises any target that would
not clear the round trip. `--ignore-fee-floor` disables that guard so you can see
the damage in a backtest, and nowhere else.

**There is one legitimate route to a 92% win rate: buy near-certainties.** A
contract at 92c settles in your favour ~92% of the time. It is also negative
expectancy after fees — you win 8c ninety-two times and lose 92c eight times —
which the calibration table shows you directly in the `EV/contract` column.

So: **optimise NET. Then report whatever win rate honestly comes with it.** If
that number is 56%, publish 56%. A verifiable 56% sells better than a 92% that
falls apart the first week a customer checks.

### 4.5 Sweep before believing anything

```bash
.venv/bin/python -m kbot.research sweep --strategy all --targets 5,8,12,20,30
```

One backtest in isolation is how people fool themselves. A sweep shows whether a
result is a peak or a plateau. **A single strong cell surrounded by weak ones is
overfitting.** A smooth region that stays positive across neighbouring settings
is the only shape worth trusting.

Watch for monotonic patterns too. If net improves steadily with target size
while win rate stays flat, you have discovered target sizing — not signal
quality.

---

## Part 5 — Paper trading

Paper mode is not a separate code path. `PaperBroker` and `LiveBroker` implement
the same three calls, so the only difference between a simulation and a real
order is which object the engine holds. Fills are modelled against the real live
book and capped by the size actually resting there.

Run it for days. Then:

```
/pnl 7        realised P/L, paper and live split out
/stats 7      by coin and direction, best/worst hours, drawdown
```

`/stats` shows the two things a win rate hides: **max drawdown**, and the split
between target-hit and held-to-expiry.

Go live only when the paper numbers are positive, net of fees, over enough
trades that you would defend them to a sceptic.

---

## Part 6 — Going live

1. **Create a Kalshi API key** — kalshi.com → Account → API Keys. You get a key
   ID and download an RSA private key.

2. **Connect it**: `/connect`, then paste the key ID, then the whole private key
   file. The bot verifies it against Kalshi before saving, encrypts it at rest,
   and deletes your message from the chat.

3. **Set risk caps before flipping to Live**, on the dashboard:
   - daily loss limit — trading pauses for the rest of the UTC day when hit
   - max open exposure
   - balance floor the bot will not spend below
   - contracts per signal, trades per window, max open positions

   These are recomputed from your trade ledger on every check, so restarting the
   bot cannot reset them.

4. **Flip Live, press Start trading**, and watch the first few.

`/disconnect` removes your key entirely. Switching back to paper is instant.

### What the bot does automatically

- Places a sell order the moment an entry fills, so a position is never
  unmanaged
- Raises any exit target that would not clear the round-trip fee
- Reconciles against Kalshi's real positions at startup and every 5 minutes:
  exits that filled while the bot was down, positions closed by hand, and
  partial fills are all detected and reported
- Cancels resting orders left on markets that are no longer live
- Throttles its own request rate so a 429 never lands mid-order
- Stops trading and tells the user why if their key stops authenticating

---

## Part 7 — Selling access

### Manual — no third-party account

```bash
MANUAL_PAY_ADDRESSES={"BTC":"bc1...","ETH":"0x...","USDT":"T..."}
```

Buyers run `/buy`, choose a tier, and get an address plus an order ID. You
confirm receipt:

```
/confirm                 lists everything pending
/confirm db-123-abc      settles it and delivers the key
```

### Automatic — NOWPayments

```bash
PAYMENT_PROVIDER=nowpayments
NOWPAYMENTS_API_KEY=...
NOWPAYMENTS_IPN_SECRET=...
PAYMENT_CALLBACK_URL=https://yourdomain.com/webhook/payment
```

The key is minted and delivered the moment payment confirms on-chain. Callbacks
that fail signature verification are rejected — the IPN secret is what stops
someone minting a key by guessing an order ID. One payment issues exactly one
key no matter how many times the provider retries.

Test with a small real payment before announcing it.

### The website

`site/index.html` is one self-contained file. Set:

```js
const BOT = "YourBotUsername";
```

Every pricing button then deep-links into the bot on that tier
(`t.me/<bot>?start=buy_monthly`). Replace `RESULTS_CHANNEL_URL` and
`TESTIMONIALS_CHANNEL_URL` with your channel links.

The ticker numbers are illustrative placeholders, marked as such in the source.
Wire them to a real feed or remove them.

Admin commands: `/genkeys <tier> <count>`, `/keystats`, `/grant <id> <tier>`,
`/sales`.

---

## Part 8 — The results channel

```bash
RESULTS_CHAT_ID=@YourResultsChannel
RESULTS_POST_LOSSES=true
```

Create a public channel, add the bot as an admin. Every resolved trade posts
with a running tally:

```
💰 CASHED OUT · ETH DOWN        🔻 CLOSED RED · ETH DOWN
10 sh @ 47.2c → 55.2c            10 sh @ 58.9c → 0c
gross +$0.80 · fees $0.04        gross -$5.89 · fees $0.02
+$0.76 net                       -$5.91 net

Running: 40 trades · 31 wins (78%) · -$25.73 net
```

**Leave `RESULTS_POST_LOSSES` on.** The running tally is what makes the channel
worth anything, and it is only credible if it includes the red ones. A feed of
nothing but wins is not evidence — every trader knows a losing system can
produce a long green streak, and the first person who does the arithmetic on
your own numbers will find the gap.

Members opt in for their own trades with `/share on`. Posts carry no username,
size attribution or account detail.

---

## Part 9 — Deploying

### systemd

```bash
sudo useradd --system --home /opt/directionalbot directionalbot
sudo chown -R directionalbot:directionalbot /opt/directionalbot
sudo cp deploy/directionalbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now directionalbot
journalctl -u directionalbot -f
```

### Docker

```bash
docker compose up -d
docker compose logs -f
```

### fly.io

```bash
fly launch --no-deploy --copy-config
fly volumes create kbot_data --size 1
fly secrets set TELEGRAM_BOT_TOKEN=... MASTER_KEY=... ADMIN_IDS=...
fly deploy
```

`deploy/fly.toml` pins `ord` (Chicago) and disables scale-to-zero.

### TLS and the website

`deploy/Caddyfile` terminates TLS, serves `site/`, and forwards `/webhook/*` and
`/healthz` to the bot on 8080.

**Back up `data/`.** It holds users, access keys, invoices and the trade ledger —
and the risk caps are enforced from that ledger.

---

## Part 10 — Writing your own strategy

A strategy is a pure function of market state. It cannot place orders, read the
database, or know which user it runs for.

```python
# kbot/strategy/mine.py
from .base import MarketContext, Signal


class MyStrategy:
    name = "mine"
    description = "Shown in the bot's strategy picker."

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        # Prices and fair-value changes are DECI-CENTS: 20 == 2 cents.
        book = ctx.book
        imbalance = book.imbalance()
        if imbalance is None or ctx.fv_change_20s is None:
            return None

        if imbalance > 0.5 and ctx.fv_change_20s > 20:
            price = book.best_ask("yes")
            if price is None:
                return None
            return Signal(
                coin=ctx.coin,
                ticker=ctx.ticker,
                side="yes",          # "yes" = UP, "no" = DOWN
                confidence=0.7,      # 0..1, compared against the user's floor
                price_dc=price,
                reason="what fired, shown to the user",
            )
        return None
```

Register it in `kbot/strategy/__init__.py`:

```python
from .mine import MyStrategy
REGISTRY = {s.name: s for s in (DriftStrategy(), FadeStrategy(),
                                HammerStrategy(), MyStrategy())}
```

It now appears in the picker and in the research tools:

```bash
python -m kbot.research replay --strategy mine
python -m kbot.research sweep --strategy all
```

### What `MarketContext` gives you

| field | meaning |
|---|---|
| `book` | the live order book — `imbalance()`, `microprice()`, `best_ask(side)`, `depth(side)`, `size_at_ask(side)`, `spread` |
| `fv_change_5s/20s/60s` | fair-value change over that window, **deci-cents** |
| `seconds_to_close` | time left in the window |
| `samples` | how much history exists — check before trusting momentum |
| `book_age_s` | staleness; supplied by the caller so replays work |
| `spot`, `spot_change_pct` | optional spot reference |

### Units, once

Everything internal is **integer deci-cents**: 1000 = $1.00, 10 = 1 cent.
Kalshi's 15-minute markets tick in tenths of a cent below 10c and above 90c, so
whole cents cannot represent a real quote. User-facing settings stay in cents;
conversion lives only in `kbot/kalshi/prices.py`.

Before choosing a profit target, check what it must clear:

```python
from kbot.kalshi.fees import breakeven_cents
breakeven_cents(count=10, entry_dc=500)   # ≈ 4.0 cents just to break even
```

---

## Command reference

### Telegram — traders

| | |
|---|---|
| `/start` · `/dashboard` | open the dashboard |
| `/buy` | buy access with crypto |
| `/redeem KEY` | activate access |
| `/connect` · `/disconnect` | manage your Kalshi API key |
| `/positions` | open positions and recent trades |
| `/pnl [days]` | realised P/L, paper and live split |
| `/stats [days]` | by coin, direction, hour; drawdown |
| `/status` | feed health and live markets |
| `/share on\|off` | post your trades to the results channel |
| `/stop` | stop trading |

### Telegram — admin

| | |
|---|---|
| `/genkeys <tier> [n]` | mint access keys |
| `/keystats` · `/sales` | redemptions and revenue |
| `/grant <tg_id> <tier>` | grant access directly |
| `/confirm [order-id]` | settle a manual payment |

### CLI

```bash
python -m kbot                          # run the bot
python -m kbot.tools genkey             # generate a MASTER_KEY
python -m kbot.tools series             # re-derive 15-min series from the API
python -m kbot.tools markets            # current live window per coin
python -m kbot.tools mintkeys weekly 10 # mint keys without Telegram

python -m kbot.research record          # capture live books
python -m kbot.research days            # what you have recorded
python -m kbot.research calibrate       # is the market priced correctly?
python -m kbot.research replay          # score a strategy, net of fees
python -m kbot.research sweep           # strategies × targets
python -m kbot.research tune            # search a win-rate target, and price it
```

---

## Troubleshooting

**`markets` finds nothing** — run `series` and update `KALSHI_SERIES`. Low-volume
coins do not always have an open window.

**`/status` says "REST polling"** — no platform Kalshi credentials. It works; the
websocket is just faster.

**No trades for hours** — normal. The filters skip wide spreads, thin books, the
first and last minutes of a window, and setups where the signal components
disagree. Confirm books are live with `/status`.

**Profits smaller than the price move** — correct. The fee is charged on entry
and exit, roughly 4c of round-trip movement near mid-book. `/pnl` and `/stats`
break out gross, fees and net.

**"Reconciled N positions, found 1"** — the ledger and Kalshi disagreed. The
message says exactly how. A partial fill is reported rather than closed, because
the remainder still needs managing.

**Trading stopped by itself** — the bot disables trading and says why: an invalid
key, live mode with no credentials, a hit daily loss limit, or the user blocked
the bot.

**A backtest looks too good** — run `calibrate`. A mean absolute gap above ~15%
means the data, not the strategy, is doing the work.

---

## The one-paragraph version

Record real data for a week. Run `calibrate` to see whether the market is priced
correctly and where a gap might exist. Run `sweep` to see whether any setting is
a plateau rather than a lucky cell. Read the **NET** column, never the win
column. Paper trade until the numbers hold up over enough trades to defend.
Then go live small, with caps set before you flip the switch, and publish the
real record — losses included — because a verifiable 56% is worth more than a
92% that does not survive contact with a customer's spreadsheet.
