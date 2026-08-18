# The other side of the spread

**Status: the best-supported direction in this project, and still unproven.**

Not a signal. Everything in `FINDINGS.md` and `FAVOURITE_LONGSHOT.md` tried to
out-forecast Kalshi's price and lost. This does not forecast anything.

Reproduce with:

```bash
python -m kbot.research --dir data/history_big maker
python -m kbot.research --dir data/history_big maker --no-maker-discount
```

---

## Why look here

The measured cost of crossing the spread, over 36,000 two-sided quotes:

```
pay half-spread          -0.789 c
pay taker fee            -1.093 c
                         --------
predicted                -1.882 c
OBSERVED                 -2.243 c     (29,825 trades, -$6,690)
```

Transaction costs explain **84%** of every loss recorded in this project. The
forecast was never the binding problem. A resting order is paid that
half-spread instead of paying it, and is charged a lower fee, so the sign of
the arithmetic flips before any prediction is involved.

## What a resting quote earns

Per fill, before adverse selection. Split by liquidity, because that split
turns out to matter more than the price band:

| band | BTC/ETH | SOL/XRP/DOGE |
|---|---|---|
| 15–30¢ | +0.190¢ | **+0.690¢** |
| 30–50¢ | +0.080¢ | +0.580¢ |
| 50–60¢ | +0.060¢ | +0.560¢ |
| 70–80¢ | +0.160¢ | +0.660¢ |
| 80–90¢ | **+0.270¢** | **+0.770¢** |
| 90–98¢ | +0.000¢ | +0.350¢ |

Median spreads, which drive all of this:

```
coin    wing    middle
BTC     0.10c    1.00c
ETH     0.20c    1.00c
DOGE    0.70c    2.00c
SOL     0.60c    2.00c
XRP     0.60c    2.00c
```

**The thinner coins pay twice the spread.** A maker is paid for liquidity, so
the illiquid markets are the ones worth quoting — the opposite of where a taker
wants to be.

## Three things this does not prove

**1. Adverse selection is unmeasured, and it decides everything.** A resting
quote is not filled at random; it is filled when someone wants the other side,
which is disproportionately when the price is moving through it. So the numbers
above are reported as a *threshold*, not a profit:

| cell | absorbs | as % of half-spread |
|---|---|---|
| SOL/XRP/DOGE 80–90¢ | 0.770¢ | 77% |
| BTC/ETH 80–90¢ | 0.270¢ | 54% |
| BTC/ETH 50–60¢ | 0.060¢ | 12% |

Adverse selection above those numbers and the strategy is negative. Candlestick
data has neither depth nor queue position, so nothing harvestable can measure
it. **This needs the recorder** — see `RECORDER.md`.

**2. The maker fee is not confirmed.** `MAKER_FEE_FRACTION = 0.25` comes from
three secondary sources that agree. Kalshi's own schedule PDF rate-limits
automated fetches, `docs.kalshi.com` 404s on every fee path except rounding,
and `GET /series/{ticker}` returns only `fee_type: quadratic` and
`fee_multiplier: 1` with no maker field. Run `--no-maker-discount`: **every band
turns negative.** A single real maker fill on a live account settles this.

**3. Fill volume is unknown, and points the wrong way.** Wide spreads usually
mean thin markets. The cells with the best per-fill economics are the ones most
likely to have nobody trading against them. Fill rate does not change the sign
— it changes whether this is worth running at all.

## Inventory is the strategy, not a detail

`STATUS.md` records that **73.9% of books have no bid on the losing side in the
final 20 seconds.** A maker holding stock near expiry may be unable to flatten
at any price, and an unflattened contract settles at 0 or 100.

What one stuck contract costs, in clean fills:

| cell | fills lost per stuck contract |
|---|---|
| SOL/XRP/DOGE 80–90¢ | 12 |
| BTC/ETH 80–90¢ | 35 |
| BTC/ETH 50–60¢ | **305** |

At mid prices on liquid coins, one contract you cannot exit erases three
hundred good fills. Inventory limits and forced flattening are not risk
management layered on top of this strategy — they are most of it.

## What would settle it

1. **Confirm the maker fee.** Decides the sign. One live maker fill.
2. **Run the recorder.** Order-book depth over time is the only way to measure
   fill probability and adverse selection. It needs no credentials and one
   command; it has been at 2% uptime for want of a host that does not idle out.
3. **Then** build post-only quoting with hard inventory caps. `UPGRADE.md` §6
   records the execution engine as unbuilt: no post-only, no cancel/replace, no
   queue awareness.

Steps 1 and 2 are cheap and neither has been done. Nothing should be built
until both are.
