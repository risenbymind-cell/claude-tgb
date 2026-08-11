# Setup

From nothing to a bot placing paper trades. Roughly 20 minutes.

---

## 1. Create the Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`.
2. Pick a name and a username. Save the token it gives you.
3. `/setprivacy` → **Disable** is not needed — the bot only works in direct
   messages and ignores group chats.

Get your own Telegram user ID from [@userinfobot](https://t.me/userinfobot). You
need it to mint access keys.

---

## 2. Configure

```bash
git clone <this repo> /opt/directionalbot && cd /opt/directionalbot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Generate the encryption key that protects users' stored Kalshi credentials:

```bash
.venv/bin/python -m kbot.tools genkey
```

Fill in `.env`:

| Variable | Required | What it is |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | From BotFather. |
| `MASTER_KEY` | yes | The key you just generated. **Back it up** — lose it and every stored Kalshi credential becomes unreadable. |
| `ADMIN_IDS` | yes | Your Telegram user ID. |
| `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` | no | A platform key used *only* for the shared market-data websocket. Without it the bot falls back to REST polling — it works, just slower to see the book move. |
| `KALSHI_DEMO` | no | `true` points everything at Kalshi's demo environment. |

Never commit `.env`, `MASTER_KEY`, or any `.pem`. They are gitignored.

---

## 3. Check the market data

Kalshi renames series over time, so confirm the bot can see the markets before
you rely on it:

```bash
.venv/bin/python -m kbot.tools series    # which 15-min series exist right now
.venv/bin/python -m kbot.tools markets   # the live window per coin
```

`series` prints a ready-to-paste `KALSHI_SERIES=` line. A `*` marks a series
that exists on the exchange but differs from your current config.

If `markets` lists tickers and countdowns, the data path works.

---

## 4. Preflight

One command checks everything a deploy needs and exits non-zero if anything is
actually broken, so it can gate a release:

```bash
.venv/bin/python -m kbot.tools doctor
```

```
Configuration
  ✓ config valid · PRODUCTION environment
  ✓ 12 coin(s) configured
  ✓ 1 admin(s)
Storage
  ✓ /opt/directionalbot/data is writable
  ✓ existing database opens with this MASTER_KEY
Network
  ✓ webhook port 8080 is free
  ✓ Kalshi reachable · 9 live 15-minute market(s)
  ✓ Telegram token valid · @yourbot
Payments & results
  ! no payment addresses set — /buy is disabled, use /genkeys

Ready to deploy, with 1 warning(s).
```

Exit codes: `0` ready, `1` something is broken, `2` the configuration itself is
invalid.

A bad value never produces a stack trace — it produces one line naming the
variable and what was expected, and the service units carry
`RestartPreventExitStatus=2` so a typo in `.env` stops the service instead of
crash-looping.

---

## 5. Run it

```bash
.venv/bin/python -m kbot
```

You should see `Connected to Telegram as @yourbot`. Message your bot `/start`.

Then, as the admin:

```
/genkeys lifetime 1     mint yourself a key
/redeem <that key>      activate it
```

Pick your coins on the dashboard and press **Start trading**. You are in paper
mode by default — it will simulate fills against the real live book. Watch it
for a few windows before going anywhere near live.

---

## 6. Keep it running

### systemd (a plain VPS)

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

`deploy/fly.toml` pins `primary_region = "ord"` (Chicago) and disables
scale-to-zero — a trading bot must not sleep.

---

## 7. Payments (optional)

Skip this entirely if you hand out keys yourself with `/genkeys`.

### Manual — no third-party account

```
MANUAL_PAY_ADDRESSES={"BTC":"bc1...","ETH":"0x...","USDT":"T..."}
```

Buyers run `/buy`, get an address and an order ID. You confirm receipt with
`/confirm <order-id>` and the key is minted and delivered. `/confirm` with no
argument lists everything pending.

### Automatic — NOWPayments

```
PAYMENT_PROVIDER=nowpayments
NOWPAYMENTS_API_KEY=...
NOWPAYMENTS_IPN_SECRET=...
PAYMENT_CALLBACK_URL=https://yourdomain.com/webhook/payment
```

The callback URL must be reachable over HTTPS. `deploy/Caddyfile` terminates TLS
and forwards `/webhook/*` and `/healthz` to the bot on port 8080.

Callbacks that fail signature verification are rejected — the IPN secret is what
stops someone minting themselves a key by guessing an order ID.

Test the path end to end with a small real payment before announcing it.

---

## 8. The website

`site/index.html` is a single self-contained file. Open it and set:

```js
const BOT = "YourBotUsername";
```

Every CTA and pricing button then deep-links into the bot —
`Get Monthly` → `t.me/<bot>?start=buy_monthly`, which opens the buy flow on that
tier. Also replace `RESULTS_CHANNEL_URL` and `TESTIMONIALS_CHANNEL_URL` in the
Proof section with your channel links.

Serve it as static files from any host. The Caddyfile already serves it
alongside the webhook.

The ticker numbers in the page are illustrative placeholders, clearly marked as
such in the source. Wire them to a real feed or remove them — don't present
invented numbers as live quotes.

---

## 9. Results channel (optional)

Create a public Telegram channel, add the bot as an admin, then set:

```
RESULTS_CHAT_ID=@YourResultsChannel
RESULTS_POST_LOSSES=true
```

Every resolved trade is posted with a running tally. Leave `RESULTS_POST_LOSSES`
on — the running total is what makes the channel credible, and it is only
credible if it includes the red ones. Members opt in for their own trades with
`/share on`; nothing identifying is posted.

Put the channel link into `site/index.html` where `RESULTS_CHANNEL_URL` is.

---

## 10. Record from day one

Install the recorder as its own service, separate from the bot:

```bash
sudo cp deploy/directionalbot-recorder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now directionalbot-recorder
```

Separate on purpose: you want data collecting whether or not the bot is
trading, and a bot restart should not interrupt a recording. About 25 MB per
day gzipped for all coins.

This is the only thing that makes the profitability question answerable.

---

## 11. Measure before you sell

```bash
python -m kbot.research record --interval 1      # leave running for days
python -m kbot.research replay --strategy drift --size 10
python -m kbot.research sweep --strategy all --targets 5,8,12,20,30
```

Watch for a high win rate with a negative net — small targets plus full-stake
losses at expiry produce exactly that, and it is the single easiest way to
mistake a losing system for a winning one.

---

## Going live with real money

1. Run paper mode for long enough to have an opinion about the strategy, not a
   hope. `/pnl 7` splits paper and live results out separately.
2. Create a Kalshi API key: kalshi.com → Account → API Keys.
3. `/connect` in the bot, paste the key ID and the private key. It is verified
   against Kalshi before being saved, encrypted at rest, and your message is
   deleted from the chat.
4. Set your risk caps *before* flipping to Live: daily loss limit, max exposure,
   balance floor, contracts per signal.
5. Flip **Live**, press **Start trading**, and watch the first few.

The bundled strategies are reference implementations, not a validated edge. See
the README.

---

## Troubleshooting

**`markets` finds nothing** — run `series` and update `KALSHI_SERIES`. Some
lower-volume coins (ADA, BCH, TON) don't have an open window at all hours.

**Feed shows "REST polling" in `/status`** — no platform Kalshi credentials
configured. It works; the websocket is just faster.

**A user's trading stopped by itself** — the bot disables trading and says why
when a stored key stops authenticating, when live mode is on with no credentials
connected, or when the user blocks the bot.

**Profits look smaller than the price move** — they should. Kalshi's fee is
charged on entry and exit, and near mid-book it costs about 4c of round-trip
movement. `/pnl` breaks out gross, fees and net so you can see it. Targets below
the fee floor are raised automatically rather than booked as losing "wins".

**Everything looks right but no trades** — that's normal. The filters skip wide
spreads, thin books, the first and last minutes of a window, and setups where
the signal components disagree. Watch `/status` to confirm books are live.
