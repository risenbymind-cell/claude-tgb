# Running the recorder

This is the first thing to deploy and the only thing that has to run for days.
Everything else in the project is downstream of the question it answers.

**It needs no credentials.** Kalshi's order books are public: no API key, no
Telegram token, no `MASTER_KEY`, no `.env`. Verified — it starts and captures
with a completely empty environment. It also opens no inbound port and has no
order path, so there is nothing to secure and nothing it can spend.

---

## Start it

**Docker, anywhere:**

```bash
docker compose -f docker-compose.recorder.yml up -d
```

**Directly:**

```bash
python -m kbot.research --dir ./data/recordings record --interval 1
```

**As a service** (survives reboots):

```bash
sudo cp deploy/directionalbot-recorder.service /etc/systemd/system/
sudo systemctl enable --now directionalbot-recorder
```

**Render / fly.io:** the recorder is already defined in `render.yaml` and
`fly.recorder.toml`.

---

## The only requirement that matters: it must not sleep

Free tiers on Render, Fly, Railway and similar idle out after a period with no
inbound traffic. The recorder *has* no inbound traffic — it makes outbound
requests only — so it looks idle permanently and will be stopped within the
hour.

This is not hypothetical. The recordings currently in this repo show:

```
capture rate  1%
uptime        2% of the covered span
gaps          1, totalling 33.8h with nothing recorded
```

The recorder was started, ran briefly, stopped, and was not running for the
following day and a half. That is why `render.yaml` specifies `plan: starter`
rather than free, and why `fly.recorder.toml` sets
`auto_stop_machines = false`.

A $5 VPS or a Raspberry Pi on your desk both work fine. So does a laptop, as
long as it does not sleep.

---

## Check it is working

```bash
python -m kbot.research --dir ./data/recordings inventory
```

```
  toward a verdict  [####....................................] 9%
  18/200 settled markets

  capture rate      1% of what 9 coins over 2 day(s) could have produced.
  uptime            2% of the covered span
  gaps              1, totalling 33.8h with nothing recorded
```

Exit code is `1` while the data cannot support a verdict and `0` once it can,
so it works as a gate:

```bash
python -m kbot.research inventory && python -m kbot.research search
```

The desk's **Research** tab shows the same thing, and says whether the
recorder is capturing *right now*.

### In the logs

Every five minutes:

```
INFO  recording: 9 markets live, 24,323 records total (+2,840 in the last 5m, 9.5/s), up 14.2h
```

If nothing was captured since the last check, that line is an `ERROR` saying
`NOTHING CAPTURED` instead. A process that only logs on failure is
indistinguishable, in a platform's log viewer, from one that is not running.

---

## How long, and how much disk

Roughly 25 MB/day for 9 coins, so 10 GB is over a year.

Kalshi opens four 15-minute windows an hour per coin: 96 settled windows per
coin per day. Recording 9 coins continuously reaches the 200-window threshold
in **well under a day**. The `inventory` command reports the estimate from
your *observed* rate rather than that ideal, because the ideal assumes the
recorder never stops and the whole point of this page is that it usually does.

---

## Count is not the only requirement

200 settled markets is the minimum for a verdict to carry information. It is
not sufficient on its own: a sample where most markets settled the same way is
one directional move counted many times rather than many independent
observations.

The current 18 are 16 `no` / 2 `yes`. The tooling withholds a verdict on that
shape too — see the `one_sided` check in `inventory.py`, which shares
`calibrate`'s own 75% threshold. Both the count and the balance have to be
there, which in practice means recording across a stretch that contains more
than one kind of market.
