# Testing the best option: track the settlement index

`WHAT_BOTS_ACTUALLY_DO.md` concluded the only structural edge was to track the
index Kalshi settles on. This is that test, carried out. **It did not produce a
demonstrable edge.**

---

## What the markets actually specify

Every 15-minute crypto market publishes its own terms, and they are more useful
than expected:

```
title       BTC price up in next 15 mins?
strike      63828.24          <- floor_strike, published in advance
rules       If the simple average of the sixty seconds of CF Benchmarks' BRTI
            before 11:30 AM EDT is at least the simple average of the sixty
            seconds of BRTI before 11:15 AM EDT, resolves Yes.
result      yes
exp value   64001.62          <- the realised settlement
```

So the **target is a published number** (the previous window's settlement) and
the **settlement value is published afterwards**. I verified the rule holds:
`result == (expiration_value >= floor_strike)` on **200/200** markets.

That makes the market fully specified: a known strike, a known settlement
source, and a known 60-second averaging window.

## Data used

| | |
|---|---|
| Kalshi settled BTC markets | 200, with strike and settlement value |
| Kalshi minute candles (bid/ask) | 150 markets in common |
| Coinbase BTC-USD minute closes | 3,001 minutes |
| Span | 2026-08-15 13:30 → 2026-08-17 15:30 |

BRTI itself requires a licensed API key. Coinbase is a CF Benchmarks
constituent exchange and its candles are public, so it stands in as a proxy —
this is a real limitation, addressed at the end.

---

## Result 1: Kalshi's price already contains spot

How often each signal implies the correct side, at the same instant:

| time to close | n | spot vs strike | Kalshi mid | they agree | spot right, Kalshi wrong |
|---|---|---|---|---|---|
| T−13m | 150 | 60.0% | **63.3%** | 71.3% | 12.7% |
| T−10m | 150 | **68.7%** | 67.3% | 77.3% | 12.0% |
| T−5m | 150 | 72.7% | **76.7%** | 82.7% | 6.7% |
| T−2m | 150 | 84.7% | **86.0%** | 89.3% | 4.7% |
| T−1m | 113 | 84.1% | **85.0%** | 77.9% | 10.6% |

Comparable at every horizon, and Kalshi wins at four of five. Where they
disagree, spot is right roughly **53%** of the time — a coin flip.

There is no lag to exploit. The market is not slow to the index.

## Result 2: a spot-based model is a worse forecast than the price

Standardising distance to the strike as `z = log(spot/strike) / (σ·√minutes)`
and comparing Kalshi's implied probability against a driftless normal model,
over 2,060 observations:

| | weighted MAE | Brier score |
|---|---|---|
| **Kalshi** | **5.13pp** | **0.1743** |
| spot model | 7.46pp | 0.1785 |

**Kalshi is the better probability forecast.** Lower error, better Brier score.
Any strategy that trades my model against Kalshi's price is trading a worse
estimate against a better one.

---

## The one thread that looked real, and why I am not reporting it as an edge

The bucketed errors showed a consistent pattern: Kalshi sits **too close to 50%**
when spot disagrees — +12.0pp error at z=−0.25, −6.5pp at z=+0.25. Underreaction
near the money.

Trading it (buy the side spot favours, hold to settlement, net of fees):

| \|z\| band | n | win% | net | per trade | t |
|---|---|---|---|---|---|
| 0.25–1.0 | 117 | 70.9% | +$116.88 | +$0.999 | +2.36 |
| **0.25–3.0** | **128** | **73.4%** | **+$156.06** | **+$1.219** | **+3.08** |
| 1.0–3.0 | 64 | 84.4% | +$70.03 | +$1.094 | +2.30 |

**t = +3.08 is the kind of number that gets real money deployed.** It is also
the only positive result in this entire project, and it does not survive a
chronological split:

| | n | win% | per trade | t |
|---|---|---|---|---|
| first half | 58 | 82.8% | **+$2.207** | **+4.32** |
| second half | 59 | 59.3% | **−$0.189** | −0.30 |

The entire result lives in the first half.

**I checked the obvious explanation and it does not hold.** A sustained uptrend
would make "trade the side spot favours" win by construction, but both halves
had near-identical YES rates (53.0% vs 52.0%) and the *second* half had more
drift (+1.58% vs +0.07%).

So I cannot attribute the first-half result, and I am not going to invent a
reason. What is certain: **it does not persist, n is 117–128, and per-trade
variance is roughly $4.** A t-statistic of +4.32 on 58 observations is well
within what noise produces when you have examined several bands. This is the
same failure mode as the original 100%-win-rate plan, one level deeper.

---

## Why the proxy matters, and what would settle it

Settlement is the average of **60 one-second BRTI samples**. I have **one-minute
Coinbase closes**. Three distinct handicaps:

1. **Resolution.** I cannot compute the settlement input. A minute close is one
   sample where the rule uses sixty.
2. **Constituents.** BRTI aggregates several exchanges; Coinbase is one.
3. **Latency.** Historical candles, not a live feed.

Professional market makers on these contracts have licensed BRTI at one-second
resolution. In the final minute they can compute the running settlement average
directly while I can only estimate it. That is precisely where the information
advantage lives, and it is the part I cannot reach with public data.

**What would settle the open question:** licensed BRTI second-level data, and
several weeks rather than two days. The underreaction thread would then be
testable at n in the thousands instead of the hundreds. Without that, this is
not a strategy — it is a hypothesis with one favourable half.

---

## Status

**Not profitable.** Nothing here is safe to trade.

- Kalshi's 15-minute crypto prices efficiently incorporate the settlement index.
- A public-data model of that index is a worse forecast than the market price.
- The one positive signal does not survive an out-of-sample split.

The infrastructure to keep testing exists and is committed: strike and
settlement harvesting, spot alignment, calibration and Brier scoring, and the
chronological split that caught this. If licensed index data becomes available,
the test can be re-run in an afternoon.
