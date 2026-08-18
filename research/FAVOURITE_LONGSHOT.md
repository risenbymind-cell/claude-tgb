# The favourite-longshot bias on Kalshi's 15-minute crypto markets

**Status: tested and refuted.** This document first recorded it as "the most
promising candidate found, and not yet believable." A wider test says it is not
a candidate at all. The original numbers are kept below, because how this one
failed is more useful than the fact that it did.

Reproduce with:

```bash
python -m kbot.research --dir data/history_big bands --scan
```

---

## The claim

Not a forecast. The favourite-longshot bias, documented in betting markets since
Griffith 1949, says cheap contracts win less often than their price implies and
expensive ones more often. If Kalshi's 15-minute markets carried it, you could
buy the favourite at the ask, hold to settlement, and profit without any view on
the coin at all.

Holding matters: settlement is free, so a held position pays one fee, not two.

## What it looked like

Buying at the ask ten minutes before close, on 3,000 settled markets across five
coins:

| ask band | n | actual win% | implied% | edge | per trade | t |
|---|---|---|---|---|---|---|
| 90–98¢ | 179 | **97.2%** | 92.3% | **+4.9** | **+$0.432** | **+3.54** |
| 80–90¢ | 549 | 85.4% | 84.0% | +1.5 | +$0.049 | +0.33 |
| 70–80¢ | 733 | 74.6% | 74.2% | +0.4 | −$0.099 | −0.62 |
| 60–70¢ | 866 | 65.7% | 64.4% | +1.3 | −$0.039 | −0.24 |
| 50–60¢ | 804 | 55.5% | 54.3% | +1.2 | −$0.061 | −0.35 |
| 30–50¢ | 1659 | 36.4% | 39.3% | −2.9 | −$0.457 | −3.88 |
| 15–30¢ | 934 | 19.0% | 22.4% | −3.5 | −$0.474 | −3.71 |
| 5–15¢ | 256 | 4.7% | 10.7% | **−6.0** | −$0.585 | −5.12 |

A monotonic gradient in exactly the direction the literature predicts. The
favourite band cleared t = +3.54, and a percentile bootstrap — not a normal
interval, which lies about a distribution this skewed — put the mean at
[+0.165, +0.650], excluding zero. It survived a chronological split: +$0.335 in
the first half, +$0.574 in the second.

Every check it was given, it passed.

## What killed it

The entry time was never tested. Ten minutes before close was picked first and
never revisited. Buying the same 90–98¢ band at other times:

| entry | n | win% | implied% | edge | per trade | t |
|---|---|---|---|---|---|---|
| T−780s | 18 | 100.0% | 91.7% | +8.3 | +$0.767 | +19.65 |
| T−600s | 179 | 97.2% | 92.3% | +4.9 | +$0.432 | +3.54 |
| T−450s | 472 | 94.5% | 93.0% | +1.5 | +$0.096 | +0.91 |
| T−300s | 1026 | 92.5% | 93.9% | −1.4 | −$0.185 | **−2.26** |
| T−180s | 1060 | 93.3% | 94.6% | −1.3 | −$0.169 | **−2.21** |
| T−120s | 827 | 93.0% | 94.8% | −1.9 | −$0.224 | **−2.53** |

The edge decays as the sample grows and then **reverses**, significantly, three
times. The cheap end does the same thing in mirror: −6.0pp at T−600 becomes
+2.0pp at T−120.

The samples are not nested — only 50% of the T−600 band members are still in the
band at T−300, and 19% of T−300's are in T−120's — so the negative results are
largely independent measurements, not the same markets re-scored.

A claim like "a 92¢ contract is underpriced" says nothing about the clock. One
that only holds at one time of day, on the smallest sample, is not a claim about
prices.

### The count that settles it

Scoring all eight bands at all six entry times gives **46 cells with n ≥ 100**:

```
significantly positive : 1
significantly negative : 19
expected by chance     : 1.2 in each direction

all cells pooled: 29,825 trades, -$6,690.43, -$0.2243 per trade
```

**One significant positive is exactly what 46 tests at 95% confidence produce
from noise.** It was the cell that got looked at first. The nineteen significant
negatives are sixteen times the chance expectation, and they all point the same
way: buying at the ask and holding loses about 22¢ per trade, which is roughly
the spread plus the fee.

That is not a bias in the price. That is the cost of crossing it.

---

## What to take from this

**The methodological point, which is the valuable part.** Every safeguard applied
to the original result was a real safeguard, correctly applied, and none of them
could have caught this:

* a bootstrap interval instead of a normal one — right, and irrelevant
* a chronological split — passed, and irrelevant
* seven of nine coins positive — true, and irrelevant
* a monotonic gradient across eight bands — real, and irrelevant

All four validate a result *within* a cell. None of them counts the cells. The
grid had been searched before the first number was computed, by the act of
choosing an entry time, and nothing done afterwards could undo that.

`kbot/research/bands.py` now reports the cell count and the chance expectation
alongside any result, so the next candidate cannot be read without them.

**The empirical point.** This is the fourth false positive in this project, after
a 100%-win-rate plan that lost $83 out of sample, a +$1.22/trade signal that
lived entirely in one half of the data, and 1,454 "arbitrages" that were a
misreading of bucket-market strike types. All four were found by widening the
test rather than by deepening it.

The conclusion in `STATUS.md` is unchanged, and now has one more failed avenue
behind it: **no edge has been found in these markets, and the price bias is not
one either.**

---

## Confirmed dead on held-out data

The refutation above is internal to the original sample. A later harvest of
**2,494 BTC markets over 26.4 days** — of which **1,894 the original sample
never contained** — settles it from outside:

| dataset | n | win% | per trade | t |
|---|---|---|---|---|
| original BTC slice (6.3d) | 18 | 94.4% | +$0.215 | +0.39 |
| **deep BTC, held out (20d)** | **183** | **91.8%** | **−$0.105** | **−0.51** |
| deep BTC, all (26.4d) | 201 | 92.0% | −$0.076 | −0.40 |

Held-out P&L: **−$19.13 over 183 trades with 15 losses**, bootstrap 95%
[−0.529, +0.270]. Yesterday's five losses were the quiet week; the real rate is
fifteen in twenty days.

Scanning all 46 cells on the 26.4-day window gives **0 significantly positive**
against a chance expectation of 1.2, 11 significantly negative, and
**−$0.1717 per trade over 23,467 trades.**

## One more thing worth not re-learning

`HOW_TO_FIND_ONE.md` already contained this result, written before any of the
above:

> **Mispricing.** Binned by implied probability at T-600s, realised frequency
> matched implied in every bucket — all |z| ≤ 1.7 over 727 observations. There
> is no favourite-longshot bias here to harvest. Kalshi prices these honestly.

The candidate contradicted a finding this project had already established, and
that was not checked before reporting it. Prior negative results are evidence.
Reading them first would have cost five minutes.
