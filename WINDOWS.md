# DirectionalBot on Windows — start to finish

The whole thing, in order, for a Windows PC. No server, no card, no Docker.

For what the bot *is* and how to decide whether the strategy works, read
[TUTORIAL.md](TUTORIAL.md) — especially Part 4. This document is only the
mechanics of getting it running on your own machine.

**Time:** about 20 minutes, most of it waiting for downloads.

---

## Contents

1. [Install Python and Git](#1--install-python-and-git)
2. [Create your Telegram bot](#2--create-your-telegram-bot)
3. [Get your Telegram user ID](#3--get-your-telegram-user-id)
4. [Get the code and run it](#4--get-the-code-and-run-it)
5. [Give yourself access](#5--give-yourself-access)
6. [Start the recorder](#6--start-the-recorder)
7. [Stop the PC sleeping](#7--stop-the-pc-sleeping)
8. [Start it automatically at login](#8--start-it-automatically-at-login)
9. [What to do for the next two weeks](#9--what-to-do-for-the-next-two-weeks)
10. [Then, and only then: going live](#10--then-and-only-then-going-live)
11. [Daily operation](#daily-operation)
12. [Troubleshooting](#troubleshooting)

---

## 1 — Install Python and Git

**Python** — [python.org/downloads](https://www.python.org/downloads/), get 3.11
or newer.

> On the very first screen of the installer, tick **"Add python.exe to PATH"**.
> It is easy to miss and nothing works without it. If you already installed
> Python and forgot, run the installer again and choose *Modify*.

**Git** — [git-scm.com/download/win](https://git-scm.com/download/win). Accept
every default.

Close any PowerShell windows you have open, then open a new one — PATH changes
only apply to windows opened afterwards. Check both:

```powershell
python --version
git --version
```

Two version numbers means you're ready. `'python' is not recognized` means the
PATH tickbox was missed.

---

## 2 — Create your Telegram bot

1. Open Telegram, search **@BotFather**, open it, press **Start**.
2. Send `/newbot`.
3. It asks for a **name** — the display name. Anything: `DirectionalBot`.
4. It asks for a **username** — must be unique across all of Telegram and must
   end in `bot`. e.g. `elijah_directional_bot`. Keep trying if taken.
5. It replies with a token that looks like:

   ```
   8123456789:AAFm3kQ9xZ-vB2nLpQr7sT4uVwXyZaBcDeF
   ```

Copy that token somewhere safe for the next few minutes.

> **The token is a password.** Anyone who has it controls your bot completely.
> Never paste it into a chat, a screenshot, a public repo, or a support forum.
> If it leaks, send `/revoke` to BotFather immediately and use the new one.

---

## 3 — Get your Telegram user ID

Your ID is a number, not your @username. The bot uses it to know you're the
admin.

1. In Telegram, search **@userinfobot**.
2. Open it, press **Start**.
3. It replies with your details. Copy the **Id** — a number like `847291056`.

That's it. You can block the bot afterwards; you only need it once.

---

## 4 — Get the code and run it

In PowerShell:

```powershell
cd $HOME
git clone https://github.com/risenbymind-cell/claude-tgb.git
cd claude-tgb
powershell -ExecutionPolicy Bypass -File scripts\run-windows.ps1 -Demo
```

> `-ExecutionPolicy Bypass` applies to that one command only. It does not change
> anything on your system.

The script will:

1. Find your Python and build an isolated environment for the bot
2. Install the dependencies (a minute or two, one time only)
3. Ask for your **bot token**, then your **user ID** — paste each and press Enter
4. Generate your `MASTER_KEY` and write it into `.env`
5. Run the preflight
6. Start the bot, and **keep it running**

You'll know it worked when you see:

```
Starting the bot. Leave this window open.
Kalshi: DEMO environment -- no real money can move.
Connected to Telegram as @your_bot_username
```

**Leave that window open.** Closing it stops the bot. Minimize it instead.

> **Back up your `.env` file now.** Copy it somewhere safe. `MASTER_KEY` is the
> only thing that can decrypt stored Kalshi credentials, and nobody — not me,
> not Kalshi — can recover it for you.

### What the switches do

| | |
|---|---|
| `-Demo` | Kalshi's demo servers. Written into `.env`, so it sticks. |
| `-Live` | Back to Kalshi production. |
| `-Recorder` | Run the market recorder instead of the bot. |
| `-Once` | Don't auto-restart. For debugging. |
| `-SkipChecks` | Skip the preflight. |

The script restarts the bot if it crashes, backing off 2s → 4s → 8s up to a
minute. It will **not** restart on a bad `.env`, because retrying cannot fix a
typo. A failing preflight does *not* stop it starting — on a home connection
that's usually just the network.

---

## 5 — Give yourself access

Open Telegram, find your bot, press **Start**. You'll get a welcome message and
a dashboard.

You're the admin, so mint yourself a key and redeem it:

```
/genkeys lifetime 1
```

It replies with a key. Then:

```
/redeem THE-KEY-IT-GAVE-YOU
```

Now press **Start trading** on the dashboard and pick your coins.

You are in **paper mode** by default — it simulates fills against the real live
order book and places no orders anywhere. That is where you want to be.

Tiers for `/genkeys` are `daily`, `weekly`, `monthly`, `lifetime`.

---

## 6 — Start the recorder

This is the single most important step, and it is the one most people skip.

Open a **second** PowerShell window:

```powershell
cd $HOME\claude-tgb
powershell -ExecutionPolicy Bypass -File scripts\run-windows.ps1 -Recorder
```

Leave it running alongside the bot. It writes compressed order-book snapshots to
`research\` — about 25 MB a day for all coins.

Why it matters: without recorded data there is no way to answer "does this
strategy actually make money" except by losing money finding out. The recorder
is what makes that question answerable offline, for free, in a weekend.

Start it today. Data you didn't record is gone forever.

---

## 7 — Stop the PC sleeping

A laptop that sleeps mid-window leaves a position unmanaged.

**Settings → System → Power & battery → Screen and sleep** → set **"When plugged
in, put my device to sleep after"** to **Never**.

The screen can still turn off. That's fine — only sleep matters.

---

## 8 — Start it automatically at login

Optional, but it means a reboot doesn't silently stop everything.

Press `Win+R`, type `shell:startup`, press Enter. A folder opens. Create two
shortcuts in it.

Right-click → **New → Shortcut**, and for the location paste:

```
powershell.exe -ExecutionPolicy Bypass -File "C:\Users\elijah\claude-tgb\scripts\run-windows.ps1"
```

Name it `DirectionalBot`. Repeat with `-Recorder` on the end for a second
shortcut named `DirectionalBot Recorder`.

Replace `elijah` with your actual Windows username if it differs — check with
`echo $HOME` in PowerShell.

---

## 9 — What to do for the next two weeks

Nothing. That is the whole instruction, and it is the hardest part.

Leave the bot in paper mode and the recorder running. Check in occasionally:

```
/status      is it seeing live books?
/pnl 7       last 7 days, paper and live split out
/positions   what's open right now
```

**Do not connect real money during this period.** The bundled strategies are
reference implementations, not a validated edge — I built the tuner, hit a 92%
win rate two different ways, and both were worthless. One had a 73.5% hit rate
but only 6.5% of trades actually cleared fees, netting **−$852**. The other was
an artifact of synthetic data that the calibration tool caught.

A high win rate is the easiest possible way to mistake a losing system for a
winning one.

### After about 11 days of recording

That number isn't arbitrary — it's what the statistics require. To resolve a
10¢-per-trade edge at 95% confidence and 80% power you need roughly
`n ≈ 7.85·σ²/μ²` trades, which at ~384 trades/day across all coins lands near
11 days.

Then, in a third PowerShell window:

```powershell
cd $HOME\claude-tgb
.venv\Scripts\python -m kbot.research days
.venv\Scripts\python -m kbot.research replay --strategy drift --size 10
.venv\Scripts\python -m kbot.research sweep --strategy all --targets 5,8,12,20,30
```

Read the **net** column, not the win rate. `replay` prints how many more trades
you'd need for the result to be statistically meaningful — if it says the sample
is too small, the honest answer is *unknown*, not *bad*, and you keep recording.

If net is negative after a real sample: the strategy doesn't work. That is a
successful outcome of this process. You found out for free.

---

## 10 — Then, and only then: going live

Only if the data says so.

1. Kalshi → Account → **API Keys** → create one. Download the private key file.
2. In the bot: `/connect`. Paste the key ID, then the private key.
   It's verified against Kalshi, encrypted before storage, and your message is
   deleted from the chat.
3. **Set your risk caps before flipping the switch** — daily loss limit, max
   exposure, balance floor, contracts per signal. On the dashboard.
4. Switch the runner to production:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\run-windows.ps1 -Live
   ```
5. Flip **Live** on the dashboard, press **Start trading**, and watch the first
   several trades all the way through.

Start with the smallest position size the bot allows. Fees are charged on entry
*and* exit, costing about 4¢ of round-trip movement near mid-book — `/pnl`
breaks out gross, fees, and net so you can see it.

---

## Daily operation

| Command | |
|---|---|
| `/dashboard` | the main screen — coins, mode, risk caps |
| `/status` | are books live, is the feed healthy |
| `/positions` | what's open now |
| `/pnl 7` | last 7 days, paper and live separately |
| `/stats` | strategy breakdown |
| `/stop` | stop trading, keep the bot running |
| `/connect` `/disconnect` | manage Kalshi credentials |
| `/genkeys` `/grant` `/keystats` | admin: access keys |
| `/help` | all of it |

To restart everything: close both windows, re-run the two commands from steps 4
and 6.

To update:

```powershell
cd $HOME\claude-tgb
git pull
```

Then restart the windows.

---

## Troubleshooting

**`'python' is not recognized`** — the PATH tickbox was missed during install.
Re-run the Python installer, choose *Modify*, tick "Add python.exe to PATH".
Open a **new** PowerShell window afterwards.

**`running scripts is disabled on this system`** — you left off
`-ExecutionPolicy Bypass`. Include it.

**`MASTER_KEY is required`** — your `.env` is incomplete. Newer versions of the
script repair this automatically; run `git pull` and try again.

**The bot starts but never trades** — that's normal and correct. The filters
skip wide spreads, thin books, the first and last minute of each window, and
setups where the signal components disagree. Check `/status` to confirm books
are live.

**`markets` finds nothing** — Kalshi renames series. Run
`.venv\Scripts\python -m kbot.tools series`, which prints a ready-to-paste
`KALSHI_SERIES=` line for your `.env`.

**Profits look smaller than the price move** — they should. Two lots of fees per
round trip, ~4¢ near mid-book. Targets below the fee floor are raised
automatically rather than booked as losing "wins".

**Feed says "REST polling"** — no platform Kalshi credentials set. It works;
the websocket is just faster to see the book move.

**Trading stopped by itself** — the bot disables trading and tells you why: a
stored key stopped authenticating, live mode with no credentials, or a risk cap
was hit.

---

## The one thing worth repeating

This bot is well-tested plumbing. 292 tests cover the fee maths, the order
translation, the risk caps, and the units boundary where a mistake means real
money.

None of that makes the *strategy* profitable. That is an open question, and the
recorder plus `replay` is how you answer it. Answer it before you fund it.
