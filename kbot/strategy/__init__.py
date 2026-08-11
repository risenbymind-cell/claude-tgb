from .base import MarketContext, Signal, Strategy
from .directional import DirectionalStrategy, FadeStrategy, Filters

REGISTRY: dict[str, Strategy] = {
    s.name: s for s in (DirectionalStrategy(), FadeStrategy())
}


def get_strategy(name: str) -> Strategy:
    """Look up a strategy, falling back to the default rather than raising.

    A user's saved strategy name can outlive the strategy itself after a deploy;
    falling back keeps their bot trading instead of silently erroring every tick.
    """
    return REGISTRY.get(name, REGISTRY["directional"])


def strategy_names() -> list[str]:
    return list(REGISTRY)


__all__ = [
    "REGISTRY",
    "DirectionalStrategy",
    "FadeStrategy",
    "Filters",
    "MarketContext",
    "Signal",
    "Strategy",
    "get_strategy",
    "strategy_names",
]
