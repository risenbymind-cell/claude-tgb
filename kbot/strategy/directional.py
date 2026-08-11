"""Baseline strategies for the 15-minute crypto markets.

READ THIS BEFORE TRADING REAL MONEY
-----------------------------------
These are *reference implementations*, not a validated edge. They encode
ordinary microstructure reasoning — book imbalance, short-horizon drift in fair
value, spread and liquidity filters — with every threshold exposed as a named
constant so you can fit them to your own research. Nothing here has been
backtested for you. Run them in paper mode, measure, and replace the scoring
function with your own before you risk a dollar.

The `Strategy` protocol is the extension point: add a class, register it, and it
shows up in the bot's strategy picker with no other changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import MarketContext, Signal


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass
class Filters:
    """Conditions that disqualify a market regardless of signal strength.

    Prices are in deci-cents (1000 = $1.00), matching the order book.
    """

    max_spread: int = 60  # 6c; wider and the round trip eats the edge
    min_depth: float = 20  # contracts resting across the top levels
    min_seconds_left: float = 90.0  # no entries into the close
    max_seconds_left: float = 780.0  # let the window establish a direction first
    min_price: int = 200  # 20c — avoid lottery tickets
    max_price: int = 800  # 80c — and avoid paying near-certainty prices
    max_book_age_s: float = 8.0  # a stale book is not a live book
    min_samples: int = 5  # need history before trusting momentum

    def reject(self, ctx: MarketContext) -> str | None:
        book = ctx.book
        if book.is_stale:
            return "no book yet"
        if book.age() > self.max_book_age_s:
            return "stale book"
        if ctx.seconds_to_close < self.min_seconds_left:
            return "too close to expiry"
        if ctx.seconds_to_close > self.max_seconds_left:
            return "window too young"
        spread = book.spread
        if spread is None or spread > self.max_spread:
            return "spread too wide"
        if book.depth("yes") + book.depth("no") < self.min_depth:
            return "not enough depth"
        if ctx.samples < self.min_samples:
            return "warming up"
        return None


class DirectionalStrategy:
    """Momentum: trade with the book, not against it.

    Score combines three normalised, independent reads:

      * imbalance  — which side of the book is carrying real size
      * drift      — how fair value has moved over the last ~20s
      * impulse    — whether that move is still happening right now (last ~5s)

    They must agree in sign. A market whose depth says UP while its price is
    drifting DOWN is exactly the ambiguous setup worth skipping.
    """

    name = "directional"
    description = "Momentum — trades with book imbalance and short-horizon drift."

    # Normalisers: the move size at which a component counts as "full strength",
    # in deci-cents.
    DRIFT_FULL_SCALE = 40.0  # 4c over 20s
    IMPULSE_FULL_SCALE = 15.0  # 1.5c over 5s
    IMBALANCE_WEIGHT = 0.40
    DRIFT_WEIGHT = 0.35
    IMPULSE_WEIGHT = 0.25
    MIN_SCORE = 0.35  # below this the components are not really agreeing

    def __init__(self, filters: Filters | None = None) -> None:
        self.filters = filters or Filters()

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        if self.filters.reject(ctx) is not None:
            return None

        book = ctx.book
        imbalance = book.imbalance()
        if imbalance is None:
            return None
        drift = ctx.fv_change_20s
        impulse = ctx.fv_change_5s
        if drift is None or impulse is None:
            return None

        # A YES-heavy book and rising fair value both point UP.
        components = {
            "imbalance": _clamp(imbalance, -1, 1),
            "drift": _clamp(drift / self.DRIFT_FULL_SCALE, -1, 1),
            "impulse": _clamp(impulse / self.IMPULSE_FULL_SCALE, -1, 1),
        }

        score = (
            components["imbalance"] * self.IMBALANCE_WEIGHT
            + components["drift"] * self.DRIFT_WEIGHT
            + components["impulse"] * self.IMPULSE_WEIGHT
        )

        direction = 1 if score > 0 else -1
        # Require agreement: every non-flat component must share the sign.
        for value in components.values():
            if value != 0 and (value > 0) != (direction > 0):
                return None
        if abs(score) < self.MIN_SCORE:
            return None

        side = "yes" if direction > 0 else "no"
        price = book.best_ask(side)
        if price is None or not (self.filters.min_price <= price <= self.filters.max_price):
            return None

        confidence = _clamp(abs(score))
        return Signal(
            coin=ctx.coin,
            ticker=ctx.ticker,
            side=side,
            confidence=confidence,
            price_dc=price,
            reason=(
                f"imbalance {components['imbalance']:+.2f}, "
                f"20s drift {drift/10:+.1f}c, 5s impulse {impulse/10:+.1f}c"
            ),
            detail={
                "score": score,
                "imbalance": components["imbalance"],
                "drift_20s": drift,
                "impulse_5s": impulse,
                "spread": float(book.spread or 0),
                "seconds_left": ctx.seconds_to_close,
            },
        )


class FadeStrategy:
    """Mean reversion: fade an overextended move late in the window.

    The mirror image of `DirectionalStrategy`, included so the presets are not a
    single idea wearing two hats. It only acts when a sharp short-horizon move
    has run against a book that has *not* followed it — the classic thin-book
    overshoot — and only in the back half of the window.
    """

    name = "fade"
    description = "Mean reversion — fades overextended late-window moves."

    OVEREXTENSION_DC = 60.0  # a 6c move over 60s qualifies as stretched
    MAX_CONFIRMING_IMBALANCE = 0.15  # book must not agree with the move
    MIN_SCORE = 0.35

    def __init__(self, filters: Filters | None = None) -> None:
        # Fading needs the window to have developed, so require the back half.
        self.filters = filters or Filters(max_seconds_left=450.0)

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        if self.filters.reject(ctx) is not None:
            return None

        book = ctx.book
        move = ctx.fv_change_60s
        imbalance = book.imbalance()
        if move is None or imbalance is None:
            return None
        if abs(move) < self.OVEREXTENSION_DC:
            return None
        # If the book is leaning the same way as the move, it is not an
        # overshoot — it is a trend, and this is the wrong strategy for it.
        if imbalance * move > 0 and abs(imbalance) > self.MAX_CONFIRMING_IMBALANCE:
            return None

        score = _clamp(abs(move) / (2 * self.OVEREXTENSION_DC)) * (
            1 - _clamp(abs(imbalance))
        )
        if score < self.MIN_SCORE:
            return None

        # Fade: move up means buy NO.
        side = "no" if move > 0 else "yes"
        price = book.best_ask(side)
        if price is None or not (self.filters.min_price <= price <= self.filters.max_price):
            return None

        return Signal(
            coin=ctx.coin,
            ticker=ctx.ticker,
            side=side,
            confidence=_clamp(score),
            price_dc=price,
            reason=f"60s move {move/10:+.1f}c unconfirmed by book ({imbalance:+.2f})",
            detail={
                "score": score,
                "move_60s": move,
                "imbalance": imbalance,
                "seconds_left": ctx.seconds_to_close,
            },
        )
