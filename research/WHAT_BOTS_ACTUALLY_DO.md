# What crypto and prediction-market bots actually do

Research into how working bots make money, and what it implies for this
project. The short version: **the thing that determines these markets is not on
Kalshi**, and that single fact explains every negative result in
`FINDINGS.md`.

---

## The finding that reframes everything

Kalshi's 15-minute crypto markets **do not settle on Kalshi's order book.** They
settle on the **CF Benchmarks Real-Time Index** — an aggregated price sampled
once per second from a basket of major exchanges — and specifically on the
**average of the 60 one-second samples in the final minute before the close**.

CF Benchmarks is FCA-regulated and independent of Kalshi. The last trade on
Kalshi's book is irrelevant to settlement.

Two consequences follow immediately.

**1. The Kalshi book is a derivative, not the source.** Every strategy in this
repo reads the book — a reflection of what index-watchers already know. Reading
it puts you structurally second.

**2. The outcome becomes progressively determined during the final minute.** If
settlement is the mean of 60 samples, then 30 seconds in you already know half
of the final answer with certainty. Anyone tracking the index has an
information advantage over anyone who is not, and it grows as the window closes.

### The market prices this efficiently

How often the Kalshi mid already implies the correct side, measured on 750
settled markets:

| time to close | n | mid implies right side |
|---|---|---|
| T−13min | 695 | 60.1% |
| T−10min | 729 | **69.4%** |
| T−5min | 744 | 79.2% |
| T−2min | 746 | 87.0% |
| T−1min | 613 | **93.1%** |

This is what a well-functioning market looks like: a forecast that sharpens as
information arrives, ending near-certain.

**And it is the most damning number in the project.** At T−600s the market's own
mid was **69.4%** accurate. The best plan the 4,000-strategy search produced was
**58.7%** accurate at that same moment. The search spent thousands of
hypotheses to find a signal *less informative than the price it was trading
against.*

---

## What working bots actually do

None of the documented profitable approaches are "find a pattern in the order
book."

### Cross-venue arbitrage

The dominant strategy. Bots watch the same contract on Kalshi and Polymarket and
trade the difference: reported pre-cost spreads of **1.5%–4.5% per trade**, with
windows lasting **2–7 seconds**. Requires no forecast at all — only speed and
two accounts.

**14 of the 20 most profitable Polymarket wallets are bots.**

### Within-market arbitrage (YES + NO < $1.00)

Buying both sides for under a dollar is riskless. **I tested this on 23,482 of
our own book snapshots: zero occurrences.** The cheapest both-sides cost
observed was 100.1¢, median 101.0¢. It is the easiest inefficiency to police, so
it is policed — by the bots above, in milliseconds.

### Statistical arbitrage against a better forecast

The clearest documented win: a bot trading Kalshi *weather* markets using
**31-member GFS ensemble forecasts** — external, superior information about the
settlement variable. Reported ~$1,800 profit.

This is the template that works, and note its shape: not a chart pattern, but a
better estimate of the thing being settled.

### Market making, plus Kalshi's liquidity incentives

Kalshi has filed with the CFTC for a **liquidity incentive programme** paying
traders for tight, deep, two-sided quotes — scored on second-by-second snapshots
weighted by size and distance from best. Polymarket reportedly distributes
~$300k/month on comparable programmes.

This is a real revenue line **independent of whether you can forecast anything**.
Specific widths, sizes and eligible markets were not public in what I could find,
and whether 15-minute crypto qualifies is unknown — worth asking Kalshi directly.

**A caution against my own earlier suggestion:** the peer-reviewed crypto
microstructure work below found **maker strategies underperformed takers**,
particularly during stress. Adverse selection is real: you get filled precisely
when you are wrong. Incentive payments may cover that; quote capture alone may
not.

---

## What the academic literature says about order-book signals

**Order-flow imbalance is the strongest known short-horizon predictor.** Cont,
Kukanov and Stoikov established a near-linear relationship with short-horizon
price changes, strongest within tens of seconds. Reported directional accuracy
on liquid instruments is **65–70%** — which would clear our measured 64.4%
break-even, if it transferred.

A 2026 study of crypto microstructure across five assets ranked features on
3-second returns:

1. **Order-flow imbalance** — most important
2. Bid-ask spread — wider spread means *less* predictability
3. VWAP-to-mid deviation — the reversion signal this repo already computes

Its net-of-cost trading results are the sobering part:

| | annualised return | information ratio |
|---|---|---|
| BTC, taker | 0.13 | 0.25 |
| altcoins (ETC/ENJ/ROSE), taker | 4–7% | significant at p<0.05 |
| maker | underperformed | — |

So the effect is real and survives costs — **thinly**, on a 3-second horizon,
in the underlying spot market, using tick data with the aggressor side
identified.

Our situation is worse on every axis: a 15-minute horizon, a derivative
instrument, a fee of 3.4¢/round trip against a 1¢ spread, and no trade-aggressor
data at all.

---

## Honest implications for this project

**The Kalshi-only constraint is what makes this unwinnable.** Not a limitation
of effort or of strategy search — a structural one. The variable that decides
these markets is an external index, published once per second, that Kalshi's own
price already tracks to 69% accuracy ten minutes out and 93% one minute out.
Competing on order-book pattern recognition means competing with worse
information than the price already contains.

Three coherent paths, in order of how much they actually rely on forecasting:

**1. Track the settlement index.** Consume the CF Benchmarks RTI (or its
constituent exchange feeds) and compute the running final-minute average. This
is not "using Binance to predict Kalshi" — it is reading the exact input Kalshi
settles on. It is the only avenue where the information advantage is structural
rather than hoped-for. It would need re-opening the Kalshi-only constraint.

**2. Get paid for liquidity rather than for being right.** Pursue the incentive
programme. Ask Kalshi whether 15-minute crypto is eligible and what the
requirements are. This is a business question before it is a code question, and
the honest answer may be that these markets do not qualify.

**3. Pick a market where a better forecast is obtainable.** The weather-bot
template. Kalshi lists many markets whose settlement variable has a public,
model-based forecast better than the crowd's. 15-minute crypto is close to the
worst case: the settlement variable is *already* a real-time public price, so
nobody can forecast it better than everybody.

**And the fourth option, stated plainly:** stop. Nothing found here is
profitable, the reason is structural, and the honest response to a market you
cannot get an information edge in is to not trade it.

---

## Sources

- [Order-Flow Imbalance and Short-Horizon Return Predictability in Cryptocurrency Markets (SSRN)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6938742)
- [Explainable Patterns in Cryptocurrency Microstructure (arXiv 2602.00776)](https://arxiv.org/html/2602.00776v1)
- [Order Flow Imbalance — A High Frequency Trading Signal (Dean Markwick)](https://dm13450.github.io/2022/02/02/Order-Flow-Imbalance.html)
- [Prediction Markets Are Turning Into a Bot Playground (Finance Magnates)](https://www.financemagnates.com/trending/prediction-markets-are-turning-into-a-bot-playground/)
- [Kalshi's new liquidity incentives](https://ufoholdings.substack.com/p/kalshis-new-liquidity-incentives)
- [Crypto Markets — Kalshi Help Center](https://help.kalshi.com/en/articles/13823838-crypto-markets)
- [How Kalshi BTC Markets Settle: BRRNY, Reference Prices (Kalshi BackTest)](https://kalshibacktest.com/resources/kalshi-settlement-mechanics)
- [polymarket-kalshi-weather-bot (GitHub)](https://github.com/suislanchez/polymarket-kalshi-weather-bot)
- [polymarket-arbitrage (GitHub)](https://github.com/ImMike/polymarket-arbitrage)
