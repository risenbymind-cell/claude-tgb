# The 100%-win-rate plan, tested

A search over ~4,000 strategy plans on 18 locally recorded markets returned a
best plan of **100% win rate, +$23.74**:

```
T-600s follow@0.75 +40c/- any
```

Enter 600 seconds before the close when price sits at least 75% of the way into
the window's own range, in the direction of that extreme, target +40¢, no stop.

It does not work. This is the arithmetic and the out-of-sample test.

---

## 1. Six trades could never have supported the claim

The plan traded **6 times** on the 18 markets and won all 6.

| | |
|---|---|
| Exact (Clopper-Pearson) 95% interval for 6/6 | **[54.1%, 100%]** |

A perfect record over six trades is consistent with a true win rate anywhere
from 54% upward. It cannot distinguish an excellent strategy from a coin flip
with a slight lean.

**And the search made even that generous.** At the true rate measured
out-of-sample below (58.7%), a single plan posts 6-for-6 with probability
0.587⁶ = **4.09%**. Across 4,000 plans that predicts about **164 perfect
records from chance alone**. The search found 28 plans at ≥90% — *fewer* than
chance predicts. The winners were not survivors of a filter; they were the
expected debris of a large search over a small sample.

---

## 2. Out of sample, on markets it was never fitted to

Kalshi serves settled markets and one-minute candlesticks publicly.
`kbot/research/history.py` harvests both and converts them into the same
`search.Market` shape the grid already scores — so the plan is judged by
exactly the code that found it.

**360 markets, 9 coins** (172 yes / 188 no):

| | fitted (18 markets) | out of sample (360) |
|---|---|---|
| trades | 6 | 189 |
| win rate | 100% | **58.7%** |
| net | +$23.74 | **−$83.39** |
| per trade | — | −$0.441 |

**600 markets, 4 coins, 37 hours** (301 yes / 299 no — near-perfectly balanced):

| target | trades | win rate | net | per trade | t |
|---|---|---|---|---|---|
| +5¢ | 355 | **81.1%** | **−$312.75** | −0.881 | **−7.08** |
| +15¢ | 355 | 72.7% | −$210.27 | −0.592 | −3.62 |
| +40¢ | 355 | 63.1% | −$58.21 | −0.164 | −0.76 |

The +5¢ and +15¢ variants lose money **with overwhelming statistical
significance**. An 81% win rate losing $312 is the cleanest possible
demonstration of why the net column is the only one worth reading.

The +40¢ variant — the original — has a 95% interval on expectancy of
**[−$0.58, +$0.26]**, which still spans zero. At n=355 it is refuted as
"+$23.74 and 100%" and *not* resolved as "slightly negative versus flat."

---

## 3. What it would have done to an account

189 trades, 10 contracts each (~$50 at risk per trade):

```
Gross P&L        -$44.77
Fees paid        -$38.62
Net P&L          -$83.39

Final equity     -$83.39
Peak equity       $0.00      <- never once in profit
Max drawdown     -$84.45
Worst streak      5 losses in a row
```

Fees were **46% of the total loss**. The equity curve never crossed zero: it
lost on the first trade and never recovered.

---

## 4. Why no variant of it can work

The decisive table. `need%` is the win rate required to break even at that
variant's own measured payoffs:

| target | win% | need% | gap | avg win | avg loss |
|---|---|---|---|---|---|
| +5¢ | 79.4 | 96.4 | **+17.0** | +$0.20 | −$5.33 |
| +10¢ | 74.6 | 88.1 | +13.5 | +$0.71 | −$5.30 |
| +15¢ | 70.4 | 80.8 | +10.4 | +$1.23 | −$5.17 |
| +25¢ | 60.8 | 70.4 | +9.6 | +$2.16 | −$5.15 |
| +40¢ | 58.7 | 64.4 | **+5.6** | +$2.79 | −$5.04 |

Every entry time and both directions were checked too. **Every configuration
has a positive gap.** None clears break-even.

The reason is in the last two columns:

> **The average loss barely moves with the target (−5.33 → −5.04). The average
> win scales almost linearly with it (+0.20 → +2.79).**

A loss means the market went against you and the position was flattened near
zero — you surrender most of the stake regardless of what target you set. The
target only controls the upside. So small targets win often and win nothing;
large targets win meaningfully and win rarely.

At +40¢, winning 58.7% of the time, each win would need to be worth **$3.55**
to offset the losses. It is worth **$2.79** — a 27% shortfall, and that is the
*closest* any variant gets.

### Both obvious fixes fail, for measured reasons

**Raise the target.** Only 20 of 189 trades ever reached +40¢; the other 152
were flattened. A larger target is reached less often still, and the extra size
does not compensate.

**Add a stop.** Measured on recorded books: **73.9% of markets have no bid at
all on the losing side in the final 20 seconds.** A stop does not obtain a fair
exit there, it fills into the collapse. Adding stops moved results from −$3.54
to between −$16 and −$20.

---

## 5. Nothing else in the grid worked either

The full regime-free grid on the 360-market set: **0 of 620 plans** with ≥30
trades finished positive. Eight shuffled-settlement trials all beat the best
real plan (null mean +$3.00 versus real −$5.05).

---

## Caveats, stated plainly

Both point the same way — these results **flatter** the plan:

- **One-minute bars.** A target registers as reached more easily than it would
  against a once-per-second book, and slippage between decision and fill is
  invisible.
- **No depth.** A candle has a bid and an ask but no size behind them, so every
  fill is assumed available. On a thin 15-minute book that is optimistic.

So these are upper bounds. A plan that loses here loses more live — which makes
this data suitable for refutation and unsuitable for proof.

One further limit: `_classify` needs 20+ observations to label a regime and a
15-minute window yields ~14 candles, so regime-filtered plans cannot be tested
this way. The plan under test is `any`, so it is unaffected.

---

## Reproducing

```bash
python -m kbot.research history                 # the named plan, out of sample
python -m kbot.research history --target 5      # any variant
```

Exits 2 when the plan loses money.

**What would actually be needed** is a genuine directional edge — being right
more than ~64% of the time about which way a 15-minute window resolves. Across
4,000 plans and 960 markets, the best observed was 58.7%.
