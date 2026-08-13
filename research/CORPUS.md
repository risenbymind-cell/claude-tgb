# What 445 trading transcripts actually say

Extracted from two channel dumps — **456 videos, 445 transcripts, ~945,000
words**. 432 had a usable transcript. The extractor located 863 sentences that
name an indicator *and* either state a condition or carry a number; everything
below comes from reading those.

The point of this document is to separate the falsifiable from the
motivational, and then to say honestly which parts survive translation to
Kalshi's 15-minute crypto binaries.

---

## 1. What the corpus is actually about

The two channels teach almost disjoint things, which the numbers make obvious:

| | The Rumers | DaviddTech |
|---|---|---|
| Videos | 116 | 340 |
| Rule sentences found | 189 | 674 |
| Top indicators | EMA (115), CandlePattern (20), VWAP (17), ATR (13), OpeningRange (12) | ATR (234), EMA (185), RSI (93), ADX (73), MACD (42) |
| Subject | One discretionary setup, taught repeatedly | Building and validating bots, mostly with AI |

**The Rumers is one strategy restated 116 ways.** DaviddTech is a workflow for
generating and testing many strategies. They are useful for different things:
the first for *what to trade*, the second for *how to know whether it works*.

### Indicator frequency, whole corpus

| Indicator | Videos | Mentions |
|---|---:|---:|
| RSI | 175 | 571 |
| ATR | 157 | 616 |
| MACD | 120 | 363 |
| ADX | 88 | 372 |
| EMA | 86 | 592 |
| Candle patterns | 50 | 228 |
| SuperTrend | 29 | 118 |
| Fibonacci | 29 | 136 |
| Bollinger | 25 | 115 |
| Opening range | 16 | 97 |
| VWAP | 14 | 149 |

VWAP appears in only 14 videos but 149 times — it is concentrated, not casual.
Order blocks, fair value gaps and market structure barely register (10, 8 and 4
videos). Whatever this corpus is, it is not an ICT corpus.

---

## 2. One correction before anything else

Your notes say **8 EMA**. The corpus says **9 EMA**, 129 times. "8 EMA" appears
**once** in 945,000 words.

```
9 EMA   129        200 EMA  120        50 EMA  21
8 EMA     1         20 EMA    6         21 EMA   3
```

Not a quibble — if we implement it, the length is the parameter. The 200 EMA is
the other heavily-used one, as a slower regime filter.

---

## 3. The Rumers' actual thesis

Read across all 189 rule sentences, this is not three strategies. It is **one
hypothesis stated three ways**:

> Price extended far from a fair-value anchor, where "far" is measured in units
> of that instrument's own daily range, reverts toward the anchor.

Each named setup is that idea with a different anchor and a different trigger.

### The anchor: VWAP

The VWAP material is explicitly reversion, not trend — which is the opposite of
how your notes framed it:

> *"Notice that every single time price gets below the VWAP, it reconnects.
> Every time it gets above the VWAP, it comes back and connects with the VWAP."*

> *"If the instrument is extended above or below the VWAP by 20% of that number,
> that's adequate enough."*

"That number" is the daily ATR. So the VWAP rule and the ATR rule are the same
rule.

The "never long below VWAP" filter in your notes is real, but it is a
*directional* filter layered on top — it says which side of the reversion to
take, not that the move continues.

### The threshold: 20% of daily ATR

The single most repeated number in the corpus.

> *"If this 15-minute candlestick that we boxed in is more than 20% of that
> daily ATR, you're looking at a manipulation candle."*

> *"If that opening range move exceeds 20% of that daily range, you are under
> manipulation and that stands to be reversed."*

And the claim that size scales with edge:

> *"That is almost 90% of the daily ATR chewed up in one 15-minute candle."*
> *"That asset went straight up and reached 70% of the average true range."*
> *"$5.49 is almost 50% of the daily ATR."*

So: **20% is the trigger, and higher is better.** That is a continuous variable,
not a binary condition — which is good, because it means it can be *scored*
rather than thresholded, and scored things can be tested.

### The trigger, the stop, the target

- **Entry:** *"we always need one candle to go over another candle"* — the next
  candle takes out the signal candle's extreme. Same trigger across every setup
  he names.
- **Stop:** anchored to the signal candle's wick. Local and defined.
- **Target:** the opposite side of the opening-range box.
- **Trailing exit:** the 9 EMA. Hold while price stays its side; exit on a close
  through it.
- **Confluence:** pre-market high/low, prior-day levels, and a "sucker move" —
  several candles in a row straight into resistance.

### Why he says it works

> *"They only get these events at one specific time, which is the opening range,
> between the first 15 and 20 minutes of each day, because that little pocket of
> time has the highest [liquidity]."*

Institutions have overnight order flow to fill, and the open is where the
liquidity to fill it exists. The aggressive move is the filling; the reversal is
what happens once it is done.

**This is the part that does not survive translation, and it is load-bearing.**

---

## 4. Translation to Kalshi 15-minute crypto

| Component | Transfers? | Notes |
|---|---|---|
| Extension from VWAP reverts | **Yes** | General microstructure claim, testable on any asset |
| Extension measured in daily-ATR units | **Yes** | Normalises across coins — BTC and DOGE become comparable |
| 20% threshold, higher is better | **Test, don't assume** | Calibrated on US equities; crypto ATR behaves differently |
| 9 EMA trailing exit | **Yes, on the underlying** | Not on the contract — see below |
| Never long below VWAP | **Yes** | Direct filter on YES/NO side |
| Pre-market high/low | **No** | Crypto has no pre-market |
| Opening-range box at 9:30 | **No** | Crypto is 24/7; there is no open |
| "Institutions filling overnight orders" | **No** | This is the *reason* the timing works, and it does not exist here |
| Entry: next candle breaks signal candle | **Yes** | Mechanical, works on any series |
| Stop at the signal wick | **Partly** | A binary contract cannot be stopped at a price on the underlying |

### The load-bearing problem

The opening-range strategy works — if it works — because of *a specific
mechanism at a specific time*: overnight order accumulation released into the
9:30 liquidity pocket. Crypto trades continuously. There is no accumulation, no
release, and no pocket.

So we cannot lift the strategy. What we can lift is the **measurement**:
extension from VWAP, normalised by ATR, as a predictor of short-horizon
reversion. That is a general claim about markets, and it is testable on the data
we can already record.

If it holds on crypto, we have something. If it only held because of the 9:30
mechanism, it will not, and we will find that out in about a week.

### What a binary contract changes

Two things his framework assumes that Kalshi breaks:

1. **You cannot place a stop on the underlying.** The position is a contract
   whose price is a probability. A 0.3% BTC move against you might move the
   contract 40¢ or 4¢ depending on time remaining. Risk has to be expressed in
   contract terms, not underlying terms.
2. **Time decay is the dominant term.** With 90 seconds left, a contract at 80¢
   is nearly settled regardless of what BTC does. His setups have no expiry;
   every Kalshi position does. Any translation has to carry time-to-close as a
   first-class input.

---

## 5. Where this meets what I measured

Recording live Kalshi books for ~20 minutes today, the signal analysis returned
**negative** information coefficients on every drift term:

```
drift_20s   IC -0.2358    t -2.31
drift_60s   IC -0.1975    t -1.78
```

Negative drift IC means short-horizon **mean reversion** — price moves tend to
give back. That is the same direction as this corpus's central claim, arrived at
independently.

**It is not evidence.** One 15-minute window, `n_eff` ≈ 92, and the tool
withheld a verdict for that reason. Nine coins in the same quarter hour is close
to one observation, not nine. But it is a reason to prioritise testing reversion
over momentum, and to note that our bundled `DriftStrategy` currently trades
momentum — possibly backwards.

---

## 6. What the bot channel contributes

DaviddTech is not a source of setups. It is a source of **process**, and its
process is better than most:

| Term | Mentions |
|---|---:|
| drawdown | 186 |
| overfit | 62 |
| slippage | 34 |
| commission | 15 |
| out of sample | 6 |
| walk forward | 4 |
| Sharpe | 2 |
| Monte Carlo | 0 |

> *"Otherwise we're going to end up with something that we have overfit to
> historical data and most probably useless when we put it on real markets."*

> *"If the trading strategy fails, it will move on to the next one and not spend
> hours trying to optimize something that maybe looks overfit."*

That second one is the discipline that matters most, and it is the one I already
failed once on this project — I hit a 92% win rate twice by tuning, and both
were artifacts.

Its architectural contribution is concrete and directly applicable:

**Take the signal from the underlying exchange, and use the prediction market
only for execution and pricing.** Binance OHLCV → indicators → compare against
the Yes/No price → trade only when the two agree.

---

## 7. The gap this exposes in our bot

`kbot/engine/spot.py` exists. `MarketContext` carries `spot` and
`spot_change_pct`. And:

```
$ grep -n "spot" kbot/strategy/directional.py
$
```

**Nothing.** Every strategy we ship reads the Kalshi order book and nothing
else. The bot has never looked at the price of Bitcoin.

A Kalshi 15-minute market is *"will BTC be higher in 15 minutes"*. Every source
in this corpus derives its signal from the underlying. We infer it from contract
depth. That is the single largest structural gap in the system, and it is what
this reading changes.

---

## 8. What is worth building, in order

Nothing here should be built as a strategy before it is measured as a signal.

1. **Binance OHLCV feed.** Public klines, no key. 1-minute bars for the seven
   coins, plus a daily bar for ATR.
2. **Features on the underlying, not the contract:** session VWAP, distance from
   VWAP in ATR units, daily ATR, 9 and 200 EMA, realised volatility, and
   time-to-close.
3. **Score them with `signals`.** The tool already discounts overlapping
   horizons and refuses a verdict below 200 windows. Add these features and let
   it say whether extension-from-VWAP predicts anything on crypto.
4. **Only then** a fair-value layer: turn *"BTC is 0.4% above session VWAP,
   1.2 ATR extended, 6 minutes left"* into an implied probability, and compare
   it to what Kalshi charges. That comparison is the edge, if there is one.
5. **Walk-forward validation**, per DaviddTech: fit on one period, validate on a
   second, and keep a third untouched until the end.

The binding constraint is unchanged: **~200 recorded windows**, roughly two
days of continuous recording across the coins that trade around the clock. None
of the above can be evaluated without it.

---

## Method

`scratchpad/mine.py`. Parses both dumps into per-video records, matches ~29
indicator families against the speech-to-text spellings, and keeps sentences
carrying both an indicator and either a rule cue (`never`, `only enter`,
`wait for`, `stop loss`, `above the`, …) or a number with a unit. Co-occurrence
and parameter frequencies come from the same pass. No interpretation happens in
the script — it locates passages; the reading and every claim above is mine, and
every quotation is verbatim.
