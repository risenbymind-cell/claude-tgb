from .base import MarketContext, Signal, Strategy
from .directional import (
    DriftStrategy,
    FadeStrategy,
    Filters,
    HammerStrategy,
    ReversionStrategy,
)

REGISTRY: dict[str, Strategy] = {
    s.name: s
    for s in (
        DriftStrategy(),
        FadeStrategy(),
        HammerStrategy(),
        ReversionStrategy(),
    )
}

#: Strategy names that have been renamed, so a user's saved setting survives.
ALIASES = {"directional": "drift"}


def get_strategy(name: str) -> Strategy:
    """Look up a strategy, falling back to the default rather than raising.

    A user's saved strategy name can outlive the strategy itself after a deploy;
    falling back keeps their bot trading instead of silently erroring every tick.
    """
    return REGISTRY.get(ALIASES.get(name, name), REGISTRY["drift"])


def strategy_names() -> list[str]:
    return list(REGISTRY)


__all__ = [
    "ALIASES",
    "REGISTRY",
    "DriftStrategy",
    "FadeStrategy",
    "HammerStrategy",
    "Filters",
    "MarketContext",
    "Signal",
    "Strategy",
    "get_strategy",
    "strategy_names",
]
