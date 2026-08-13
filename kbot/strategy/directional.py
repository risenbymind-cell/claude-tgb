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
        if ctx.book_age_s > self.max_book_age_s:
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


class DriftStrategy:
    """Momentum: trade with the book, not against it.

    Score combines three normalised, independent reads:

      * imbalance  — which side of the book is carrying real size
      * drift      — how fair value has moved over the last ~20s
      * impulse    — whether that move is still happening right now (last ~5s)

    They must agree in sign. A market whose depth says UP while its price is
    drifting DOWN is exactly the ambiguous setup worth skipping.
    """

    name = "drift"
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

    The mirror image of `DriftStrategy`, included so the presets are not a
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


class HammerStrategy:
    """Follow a decisive one-sided sweep.

    Drift asks whether price has been moving; Fade asks whether it has moved too
    far. Hammer asks a different question: has one side just been *taken out*?

    The setup is a sharp impulse in fair value that coincides with the opposite
    ladder thinning — someone lifting offers rather than price drifting on thin
    volume. That combination is short-lived, so this preset only fires on a
    strong recent impulse and requires the book to have visibly emptied on the
    side being run over.

    Like the others: a documented reference implementation, not a validated
    edge. Every threshold below is a knob.
    """

    name = "hammer"
    description = "Sweep-follow — trades a sharp impulse that clears one side of the book."

    IMPULSE_DC = 20.0  # a 2c move in 5s is a sweep, not drift
    MIN_IMBALANCE = 0.30  # the book must clearly favour the sweep direction
    MIN_SCORE = 0.40

    def __init__(self, filters: Filters | None = None) -> None:
        # A sweep is worth following only with time left for it to carry.
        self.filters = filters or Filters(min_seconds_left=150.0)

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        if self.filters.reject(ctx) is not None:
            return None

        book = ctx.book
        impulse = ctx.fv_change_5s
        drift = ctx.fv_change_20s
        imbalance = book.imbalance()
        if impulse is None or drift is None or imbalance is None:
            return None

        if abs(impulse) < self.IMPULSE_DC:
            return None
        # The impulse must be the sharp end of the move, not the tail of one
        # that has already played out.
        if drift != 0 and abs(impulse) < abs(drift) * 0.5:
            return None

        direction = 1 if impulse > 0 else -1
        # Depth must sit behind the sweep, not against it.
        if imbalance * direction < self.MIN_IMBALANCE:
            return None

        score = _clamp(abs(impulse) / (2 * self.IMPULSE_DC)) * _clamp(abs(imbalance))
        if score < self.MIN_SCORE:
            return None

        side = "yes" if direction > 0 else "no"
        price = book.best_ask(side)
        if price is None or not (self.filters.min_price <= price <= self.filters.max_price):
            return None

        return Signal(
            coin=ctx.coin,
            ticker=ctx.ticker,
            side=side,
            confidence=_clamp(score),
            price_dc=price,
            reason=f"5s sweep {impulse/10:+.1f}c with book {imbalance:+.2f} behind it",
            detail={
                "score": score,
                "impulse_5s": impulse,
                "drift_20s": drift,
                "imbalance": imbalance,
                "seconds_left": ctx.seconds_to_close,
            },
        )


class ReversionStrategy:
    """Fade a contract that has extended from its own session consensus.

    This is the one hypothesis the transcript corpus keeps restating, moved
    onto Kalshi's own data (see research/CORPUS.md):

        Price extended from a fair-value anchor, where "far" is measured in
        units of the instrument's own realised range, reverts toward the anchor.

    Anchor is the window's depth-weighted VWAP; the unit is the window's own
    high-low range. Both come from the contract, because there is no external
    feed -- see kalshi/session.py for why that translation is defensible and
    what it costs.

    Three things the corpus insists on that are kept here:

    * **Size matters, monotonically.** 20% of the daily range is the threshold
      it names, and it treats 70-90% as far better rather than merely also
      qualifying. Confidence therefore scales with extension instead of
      switching on at a boundary.
    * **Aggression, not drift.** A move that got there fast is the one it fades.
      Velocity must agree in sign with the extension.
    * **Room to revert.** Its target is the opposite side of the box, which
      takes time. A binary contract with ninety seconds left cannot deliver
      that however wrong the price is, so the entry window closes early.

    NOTHING HERE IS KNOWN TO BE PROFITABLE. The reasoning is sound and the
    hypothesis is testable; that is all. Score it with `kbot.research signals`
    and `replay` on 200+ recorded windows before it goes near real money.
    """

    name = "reversion"
    description = "Fades price extended from its own session VWAP."

    #: Extension at which a fade is worth considering, in session-range units.
    #: The corpus's threshold is 20% of the *daily* range; a 15-minute window's
    #: own range is a much smaller unit, so this is deliberately larger and is
    #: a starting point for a sweep, not a fitted value.
    MIN_EXTENSION = 0.30

    #: Extension treated as full strength, for scaling confidence.
    FULL_EXTENSION = 0.75

    #: The move has to have been going the way it is extended.
    MIN_VELOCITY_DC = 5.0

    #: Reverting to VWAP takes time; below this there is not enough of it.
    MIN_SECONDS_LEFT = 150.0

    def __init__(self, filters: Filters | None = None) -> None:
        # A fade needs a wider spread tolerance than a momentum entry -- it is
        # deliberately trading a market that just moved, and those are wider.
        self.filters = filters or Filters(max_spread=80, min_seconds_left=150.0)

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        if self.filters.reject(ctx):
            return None
        if ctx.seconds_to_close < self.MIN_SECONDS_LEFT:
            return None

        extension = ctx.extension
        if extension is None or abs(extension) < self.MIN_EXTENSION:
            return None

        velocity = ctx.velocity_dc
        if velocity is None:
            return None
        # Extended up on a move that is going up -- that is the aggression the
        # corpus fades. Extended up while already falling back is a reversion
        # already under way, and the edge in joining it late is the part that
        # has been given away.
        if extension > 0 and velocity < self.MIN_VELOCITY_DC:
            return None
        if extension < 0 and velocity > -self.MIN_VELOCITY_DC:
            return None

        # Fade it: extended above consensus means buy NO.
        side = "no" if extension > 0 else "yes"
        book = ctx.book
        price_dc = book.yes_ask if side == "yes" else book.no_ask
        if price_dc is None:
            return None
        if not (self.filters.min_price <= price_dc <= self.filters.max_price):
            return None

        strength = _clamp(
            (abs(extension) - self.MIN_EXTENSION)
            / max(1e-9, self.FULL_EXTENSION - self.MIN_EXTENSION)
        )
        # Confidence is bounded well below certainty on purpose. This is an
        # untested hypothesis, and a number near 1.0 would licence position
        # sizes the evidence does not support.
        confidence = 0.50 + 0.35 * strength

        return Signal(
            coin=ctx.coin,
            ticker=ctx.ticker,
            side=side,
            confidence=confidence,
            price_dc=price_dc,
            reason=(
                f"{abs(extension):.2f}x session range "
                f"{'above' if extension > 0 else 'below'} VWAP"
            ),
            detail={
                "extension": round(extension, 3),
                "vwap_dc": round(ctx.vwap_dc or 0.0, 1),
                "range_dc": round(ctx.session_range_dc or 0.0, 1),
                "velocity_dc": round(velocity, 1),
                "seconds_left": round(ctx.seconds_to_close),
            },
        )


class TimedScalpStrategy:
    """Acts at one chosen point in the window, not whenever a threshold trips.

    Every other strategy here is opportunistic: it watches continuously and
    fires the moment its condition is met. That means its entries land at
    whatever time-to-close the market happens to produce, and time-to-close is
    the single largest determinant of what a 15-minute binary is worth.

    This one inverts that. It has an appointment. At `entry_at_s` before the
    close -- and only in the seconds around it -- it looks at the window's
    price path so far and decides one thing: enter, or skip until the next
    window.

    Why a fixed clock is worth testing at all, measured on recorded books
    rather than assumed:

        seconds left   mean |mid - outcome|   Brier
             780-900          48.3c           0.245
             300-420          20.8c           0.093
              90-180           8.5c           0.028
               30-90           2.1c           0.002

    A 15-minute window is not one regime. Early it is close to a coin flip;
    by ninety seconds out the market has essentially resolved. Any edge lives
    somewhere on that curve, and a strategy with no clock samples the whole of
    it indiscriminately -- averaging a regime where the price is uninformative
    together with one where it is nearly perfect.

    Fixing the entry time makes the position on that curve a *parameter*
    instead of an accident, which is the only way to find out where, if
    anywhere, the edge is.

    The direction comes from the path: the window's own range so far, and
    where the current price sits inside it. `fade_extremes` chooses whether
    to buy the side the path has run away from (reversion, which is what the
    transcript corpus argues for) or the side it has run toward.

    NOTHING HERE IS KNOWN TO BE PROFITABLE. The recorded sample is 3 windows
    and 16 of 18 settled the same way, which is one crypto move counted many
    times. Sweep `entry_at_s` on 200+ windows before believing any of it.
    """

    name = "timed"
    description = "Enters at a fixed point in the window, direction from the path."

    #: Seconds before the close at which the decision is made.
    ENTRY_AT_S = 300.0
    #: Half-width of the appointment. Wider than the scan interval so a slow
    #: tick cannot miss the window entirely.
    ENTRY_TOLERANCE_S = 15.0

    #: Where in the window's own range the price has to sit to count as
    #: extended. 0.5 is the midpoint; 0.75 means the top quarter.
    EXTREME = 0.72

    #: Trade against the extreme rather than with it.
    FADE_EXTREMES = True

    def __init__(
        self,
        entry_at_s: float | None = None,
        *,
        extreme: float | None = None,
        fade: bool | None = None,
        filters: Filters | None = None,
    ) -> None:
        self.entry_at_s = self.ENTRY_AT_S if entry_at_s is None else entry_at_s
        self.extreme = self.EXTREME if extreme is None else extreme
        self.fade = self.FADE_EXTREMES if fade is None else fade
        # min_seconds_left has to admit the appointment itself, or the filter
        # rejects every entry this strategy exists to take.
        self.filters = filters or Filters(
            max_spread=80,
            min_seconds_left=min(90.0, self.entry_at_s - self.ENTRY_TOLERANCE_S),
            max_seconds_left=900.0,
        )

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        if self.filters.reject(ctx):
            return None

        # The appointment. Outside it there is nothing to do -- and the engine
        # dedupes one signal per market window, so the first tick inside the
        # window is the one that counts.
        if abs(ctx.seconds_to_close - self.entry_at_s) > self.ENTRY_TOLERANCE_S:
            return None

        rng = ctx.session_range_dc
        vwap = ctx.vwap_dc
        book = ctx.book
        mid = book.mid
        if rng is None or vwap is None or mid is None or rng < 20:
            return None

        # Where the price sits inside the window's own path, as a fraction.
        # Derived from VWAP and range rather than stored highs so it uses the
        # same session state everything else does.
        position = 0.5 + (mid - vwap) / (2 * rng)
        position = _clamp(position)

        distance = abs(position - 0.5) * 2  # 0 at the middle, 1 at an extreme
        if distance < (self.extreme - 0.5) * 2:
            return None

        high_side = position > 0.5
        # Fading an extreme high means buying NO.
        want_yes = (not high_side) if self.fade else high_side
        side = "yes" if want_yes else "no"

        price_dc = book.yes_ask if side == "yes" else book.no_ask
        if price_dc is None:
            return None
        if not (self.filters.min_price <= price_dc <= self.filters.max_price):
            return None

        confidence = 0.50 + 0.30 * _clamp(
            (distance - (self.extreme - 0.5) * 2) / max(1e-9, 1 - (self.extreme - 0.5) * 2)
        )
        return Signal(
            coin=ctx.coin,
            ticker=ctx.ticker,
            side=side,
            confidence=confidence,
            price_dc=price_dc,
            reason=(
                f"{'fade' if self.fade else 'follow'} at "
                f"T-{self.entry_at_s:.0f}s, {position:.0%} of range"
            ),
            detail={
                "entry_at_s": self.entry_at_s,
                "seconds_left": round(ctx.seconds_to_close),
                "position_in_range": round(position, 3),
                "range_dc": round(rng, 1),
                "vwap_dc": round(vwap, 1),
            },
        )
