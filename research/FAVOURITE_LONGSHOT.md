# The favourite-longshot bias on Kalshi's 15-minute crypto markets

**Status: the most promising candidate found, and not yet believable.**

Measured on **2,250 settled markets across 9 coins**, spanning a week. This is
the first candidate in the project that survives a chronological split. It is
still not statistically established, and the reason is worth understanding
before anyone trades it.

---

## The bias is real

Buying at the ask 10 minutes before close and holding to settlement, grouped by
what the market charged:

| ask band | n | actual win% | implied% | edge | per trade | t |
|---|---|---|---|---|---|---|
| 90–98¢ | 99 | **96.0%** | 92.3% | **+3.7** | **+$0.314** | +1.58 |
| 80–90¢ | 358 | 85.5% | 83.8% | +1.7 | +$0.070 | +0.38 |
| 70–80¢ | 522 | 72.8% | 74.3% | −1.5 | −$0.285 | −1.46 |
| 60–70¢ | 679 | 64.8% | 64.4% | +0.4 | −$0.122 | −0.66 |
| 50–60¢ | 702 | 53.3% | 54.4% | −1.1 | −$0.292 | −1.55 |
| 30–50¢ | 1335 | 38.4% | 39.9% | −1.5 | −$0.326 | −2.47 |
| 15–30¢ | 659 | 20.5% | 22.5% | −2.0 | −$0.331 | −2.12 |
| 5–15¢ | 141 | **5.7%** | 10.8% | **−5.1** | −$0.585 | −2.99 |

The gradient is monotonic at the extremes and in the direction the literature
predicts: **longshots are overpriced, favourites are underpriced.** A 5–15¢
contract wins barely half as often as its price implies. This is not a subtle
statistical artefact — the longshot end is significant at t = −2.99.

That the bias exists is well established. That it is *tradeable here* is not.

---

## Why it is not a strategy yet

The only band with a positive expectation is 90–98¢:

```
n            99
win rate     96.0%
per trade    +$0.314
t            +1.58
95% CI       [-$0.077, +$0.706]     <- spans zero
```

**Chronological split** — and this is the part no previous candidate survived:

| | n | per trade | t |
|---|---|---|---|
| first half | 49 | **+$0.323** | +1.14 |
| second half | 50 | **+$0.306** | +1.08 |

Almost identical. Seven of nine coins have a positive mean. Everything about
the *shape* of this result is what a real effect looks like.

### The reason to distrust it anyway

```
wins 95, losses 4
one loss costs  ~$9.53
total profit     $31.13
4 more losses would erase the entire edge
```

The whole result rests on **four observations**. A 96% win rate with a 30:1
loss-to-win ratio is the classic shape of picking up pennies in front of a
steamroller: the payoff is dominated by a tail that 99 samples barely touches.
The confidence interval spanning zero is not a technicality here — it is the
correct reading of a sample that has seen four of the events that matter.

**n = 99 is not enough for a strategy whose P&L is decided by rare losses.**

---

## What would make it believable

1. **Roughly 1,000 trades in the band**, i.e. ~40 losses rather than 4. At the
   observed rate that is several thousand more settled markets — reachable
   from Kalshi's history, or a few weeks of recorder uptime.
2. **A held-out period.** The split above is in-sample in the sense that the
   band boundaries were chosen after looking at the data. Fix 90–98¢ now and
   test it on markets harvested later.
3. **Depth checking.** These results assume a fill at the quoted ask. At 90–98¢
   the resting size is thin, and a strategy that cannot get filled at size is
   not a strategy. None of the analysis so far models queue position.
4. **Fee sensitivity.** The measured edge is ~3.7pp gross against a ~0.6pp fee
   at these prices. That margin is real but narrow, and it is the reason only
   the extreme band clears.

---

## Honest position

This is **not** a profitable strategy. It is the first candidate that has not
failed, which is a different and much weaker claim.

Two prior candidates in this project looked far better and were wrong: a
100%-win-rate plan that lost $83 out of sample, and a +$1.22/trade signal at
t = +3.08 whose entire result lived in one half of the data. This one is
weaker on paper than both and more structurally motivated than either — it
matches a documented market phenomenon, shows a monotonic gradient across eight
independent bands, and is stable across time.

That combination is worth pursuing. It is not worth funding.

## Reproducing

The pooled dataset is built by `kbot/research/history.py`; the band analysis is
in this document's git history. To gather more:

```bash
python -m kbot.research history --per-series 600
```
