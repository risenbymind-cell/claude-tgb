# How to find a winning strategy

Everything searched so far lost money. This is what that ruled out, what the
evidence says the actual problem was, and where the remaining candidates are.

---

## What is ruled out, with evidence

**Directional prediction from the price path.** 4,000 plans across 960 settled
markets. Best out-of-sample win rate **58.7%** against a break-even of **64.4%**.
0 of 620 plans with ≥30 trades finished positive, and shuffled-settlement trials
beat the best real plan 8/8.

**Mispricing.** Binned by implied probability at T-600s, realised frequency
matched implied in every bucket — all |z| ≤ 1.7 over 727 observations. There is
no favourite-longshot bias here to harvest. Kalshi prices these honestly.

**A standing side bias.** "Always buy NO" looked nearly free overall
(−$0.012/trade) but flips sign between chronological halves (−$0.155 then
+$0.131, neither significant). Noise.

---

## The actual problem was structure, not signal

The cost of a taker round trip, measured over 6,236 observations:

| | per contract |
|---|---|
| median spread | **1.00¢** |
| fee, both legs | **3.40¢** |
| **round-trip cost** | **4.40¢** |

The fee is **77% of the cost**, not the spread. And Kalshi's fee is
`0.07 × C × P × (1−P)` — proportional to the *variance* of the bet, so it peaks
at 50¢:

| price | fee/contract | round trip |
|---|---|---|
| 20¢ | 1.12¢ | 2.24¢ |
| 50¢ | **1.75¢** | **3.50¢** |
| 80¢ | 1.12¢ | 2.24¢ |

**Every plan in the search scalped near 50¢ with a round trip** — the maximum
fee, paid twice, to capture the thinnest edge. The search explored parameters
inside a structurally doomed shape. The shape was the mistake.

### Proof that structure dominates

Same entries, same markets, same information — only the exit structure differs:

| structure | n | per trade | t |
|---|---|---|---|
| near 50¢, round trip (+15¢ target) | 471 | **−$0.988** | **−6.75** |
| near 50¢, hold to settlement | 471 | **−$0.296** | −1.30 |

**A 70% reduction in loss from changing nothing but the structure.** It moved
from decisively unprofitable to statistically indistinguishable from zero, with
no new signal at all. Settlement is free; an early exit is not.

This is the single most valuable number produced so far, and it says: *stop
looking for a better signal until the structure stops bleeding.*

---

## Where the remaining candidates are, in order

### 1. Hold to settlement, not round trips

Free. Halves the fee, and the evidence above shows what that is worth. The cost:
losses become total rather than partial, so it needs pairing with entries far
enough from 50¢ that the payoff is asymmetric in your favour.

The 5–20¢ band held to settlement showed **+$0.221/trade over 107 trades
(t = +0.60)** — positive, not significant, and the only positive number in this
whole document. It is where I would look next, and 107 trades is nowhere near
enough to believe it.

### 2. Provide liquidity instead of taking it

Worth the 1.00¢ spread per round trip — smaller than the fee, but it flips from
a cost to a credit, a 2.00¢ swing. `post_only` already exists in the REST
client. The risk changes character rather than disappearing: adverse selection,
where you are filled precisely when you are wrong. That is measurable, and the
audit and decision logs are already there to measure it.

### 3. Measure order-book features before building any more strategies

Everything above used one-minute candles: a bid, an ask, and nothing else. Two
of the classic short-horizon predictors are invisible in that data — **resting
depth imbalance** and **order-flow direction**. The desk already computes both
(`imbalance`, `mom20_dc`), and `kbot/research/signals.py` exists to score
candidate features by information coefficient *before* any of them becomes a
strategy.

That is the correct order of operations and it has never been run on adequate
data. It needs the recorder: one-second books with depth, which candles cannot
provide.

### 4. Accept that this market may not be beatable

A real possibility, and worth stating. 15-minute crypto binaries are liquid,
heavily arbitraged, and correctly priced by every test applied here. "No edge
exists at this horizon for these inputs" is a legitimate finding, not a failure
to search hard enough.

---

## The method, which matters more than any single idea

The search that produced "100% win rate" was not wrong because the parameters
were wrong. It was wrong because of how it was run.

**1. Compute break-even before running anything.** A +5¢ target needs a 96.4%
win rate. Knowing that first would have eliminated most of the grid before a
single backtest, and would have made an 81% win rate legible as the disaster it
is rather than a near miss.

**2. Measure features, then build strategies.** Score a candidate predictor's
information coefficient on its own. A feature with no IC cannot become a
profitable strategy no matter how it is wrapped, and searching wrappers around a
dead feature is how 4,000 plans get generated.

**3. Never believe a search without its null.** Run the identical grid against
shuffled outcomes. Whatever the best plan scores there is what "best of N tries"
is worth for free. `python -m kbot.research search` now does this automatically
and exits non-zero when the real result does not beat it.

**4. Out-of-sample or nothing.** `python -m kbot.research history` scores a
named plan on hundreds of markets it was never fitted to. Any plan that has not
survived that is a hypothesis, not a strategy.

**5. Read the net, never the win rate.** 81.1% win rate, −$312.75, t = −7.08.

---

## What I would do next, concretely

1. **Run the recorder for a week.** Not a day — a week, so the sample contains
   more than one market regime. Everything in section 3 depends on it.
2. **Run `signals` on it** and look at information coefficients for depth
   imbalance and order flow at several horizons. Only build strategies around
   features that show non-zero IC with enough windows behind them.
3. **Restrict the grid to structures that are not self-defeating**: hold to
   settlement, entries away from 50¢, maker orders where possible.
4. **Test whatever survives with `history`** on out-of-sample markets, then with
   the shuffled null.

The 5–20¢ hold-to-settlement result is the one thread worth pulling first, and
it needs roughly ten times the data before it means anything.
