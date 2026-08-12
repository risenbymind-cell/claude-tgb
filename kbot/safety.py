"""Trading mode and the kill switch.

Two mechanisms, both of which exist because the same accident keeps happening
in trading systems: a single boolean somewhere decides whether orders are real,
and something flips it that nobody expected to.

**Mode** is a three-valued choice -- PAPER, DEMO_LIVE, PRODUCTION_LIVE -- and it
is the only thing that selects an API host. Previously the host came from
`KALSHI_DEMO` while whether orders were real came from a per-user boolean, so
the two could disagree: production credentials, production host, one button
press, real money. They are now the same decision, and reaching the production
value requires a second environment variable whose content is a sentence rather
than a boolean, because `TRADING_MODE=production-live` is one typo away from
`paper` and `I_UNDERSTAND_PRODUCTION_ORDERS_ARE_REAL=yes...` is not.

**The kill switch** is checked immediately before every order and can be tripped
from four independent places, so whichever one is reachable in an emergency
works: an environment variable, a file on disk, a database flag, or a Telegram
command. File and database are checked on every call rather than cached, since
a cached kill switch is not a kill switch.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

#: What `ALLOW_PRODUCTION_ORDERS` has to contain, exactly, before real orders
#: are possible. Long and specific so it cannot be set by accident, by a
#: templating default, or by copying a boolean from another variable.
PRODUCTION_ACK = "I_UNDERSTAND_THESE_ARE_REAL_ORDERS"

#: Default location of the file kill switch, relative to the data directory.
KILL_FILE_NAME = "KILL"


class TradingMode(str, Enum):
    """Where orders go. The only thing that picks an API host."""

    PAPER = "paper"
    DEMO_LIVE = "demo-live"
    PRODUCTION_LIVE = "production-live"

    @property
    def places_real_orders(self) -> bool:
        """True when an order reaches an exchange at all, demo included.

        Demo orders are not real money, but they are real API calls with real
        rate limits and real credentials, so everything that guards submission
        applies to them too.
        """
        return self is not TradingMode.PAPER

    @property
    def risks_real_money(self) -> bool:
        return self is TradingMode.PRODUCTION_LIVE

    @property
    def uses_demo_host(self) -> bool:
        """Paper mode reads the production book, because simulating against
        the demo book would simulate against liquidity that is not there."""
        return self is TradingMode.DEMO_LIVE

    @property
    def label(self) -> str:
        return {
            TradingMode.PAPER: "PAPER (simulated fills, no orders sent)",
            TradingMode.DEMO_LIVE: "DEMO-LIVE (real orders, Kalshi demo, no real money)",
            TradingMode.PRODUCTION_LIVE: "PRODUCTION-LIVE (real orders, real money)",
        }[self]


class ModeError(RuntimeError):
    """The requested mode is not safely reachable from this configuration."""


def resolve_mode(
    raw: str | None,
    *,
    ack: str | None = None,
    legacy_demo: bool | None = None,
) -> TradingMode:
    """Turn TRADING_MODE into a mode, refusing anything ambiguous.

    `legacy_demo` is the old `KALSHI_DEMO` flag. It is still honoured when no
    mode is set, so existing deployments keep working, but a configuration that
    sets both to conflicting values is rejected rather than silently resolved --
    picking a winner there is how a bot ends up trading production while its
    operator reads "demo" on the dashboard.
    """
    text = (raw or "").strip().lower()

    if not text:
        # No explicit mode. Old configurations only ever chose a host, never
        # whether orders were real, so the safe reading of both is PAPER.
        return TradingMode.PAPER

    aliases = {
        "paper": TradingMode.PAPER,
        "demo": TradingMode.DEMO_LIVE,
        "demo-live": TradingMode.DEMO_LIVE,
        "demo_live": TradingMode.DEMO_LIVE,
        "live": TradingMode.PRODUCTION_LIVE,
        "production": TradingMode.PRODUCTION_LIVE,
        "production-live": TradingMode.PRODUCTION_LIVE,
        "production_live": TradingMode.PRODUCTION_LIVE,
        "prod": TradingMode.PRODUCTION_LIVE,
    }
    mode = aliases.get(text)
    if mode is None:
        raise ModeError(
            f"TRADING_MODE must be one of paper, demo-live, production-live "
            f"(got {raw!r})"
        )

    if legacy_demo is not None:
        conflict = legacy_demo != mode.uses_demo_host
        # PAPER reads the production book by design, so KALSHI_DEMO=false with
        # TRADING_MODE=paper is consistent, not a conflict.
        if conflict and mode is not TradingMode.PAPER:
            raise ModeError(
                f"KALSHI_DEMO={str(legacy_demo).lower()} contradicts "
                f"TRADING_MODE={mode.value}. Remove KALSHI_DEMO -- TRADING_MODE "
                "now selects the host on its own."
            )

    if mode is TradingMode.PRODUCTION_LIVE:
        if (ack or "").strip() != PRODUCTION_ACK:
            raise ModeError(
                "TRADING_MODE=production-live also requires\n"
                f"  ALLOW_PRODUCTION_ORDERS={PRODUCTION_ACK}\n"
                "Two independent settings, so no single edit can start sending "
                "real orders. Until both are set, the bot runs in paper mode."
            )

    return mode


@dataclass(frozen=True)
class KillState:
    engaged: bool
    source: str | None = None
    reason: str | None = None
    since: float | None = None

    def describe(self) -> str:
        if not self.engaged:
            return "clear"
        parts = [f"ENGAGED via {self.source}"]
        if self.reason:
            parts.append(self.reason)
        return " - ".join(parts)


class KillSwitch:
    """Four independent ways to stop trading, checked before every order.

    Nothing here is cached. A kill switch that answers from memory is a kill
    switch that keeps trading after someone has pulled it, which is the only
    failure mode that matters.
    """

    def __init__(
        self,
        *,
        path: Path | None = None,
        env_var: str = "KILL_SWITCH",
    ) -> None:
        self.path = Path(path) if path else None
        self.env_var = env_var
        #: Set by the Telegram admin command and by circuit breakers. Held in
        #: memory *and* mirrored to the file, so it survives a restart.
        self._runtime: KillState | None = None

    # ---------------- reads ----------------

    def state(self) -> KillState:
        raw = os.getenv(self.env_var, "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return KillState(True, source="environment", reason=f"{self.env_var} set")

        if self.path is not None:
            try:
                if self.path.exists():
                    reason = self.path.read_text(encoding="utf-8", errors="replace")
                    return KillState(
                        True,
                        source="file",
                        reason=(reason.strip() or None),
                        since=self.path.stat().st_mtime,
                    )
            except OSError as exc:
                # An unreadable kill file is treated as engaged. The failure
                # here has to be toward not trading.
                log.warning("Kill file unreadable, assuming engaged: %s", exc)
                return KillState(True, source="file", reason="kill file unreadable")

        if self._runtime is not None and self._runtime.engaged:
            return self._runtime

        return KillState(False)

    @property
    def engaged(self) -> bool:
        return self.state().engaged

    # ---------------- writes ----------------

    def engage(self, reason: str, *, source: str = "command") -> KillState:
        state = KillState(True, source=source, reason=reason, since=time.time())
        self._runtime = state
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(f"{source}: {reason}\n", encoding="utf-8")
            except OSError as exc:
                # Memory still holds it; say so rather than reporting success.
                log.error("Could not persist kill switch to %s: %s", self.path, exc)
        log.warning("KILL SWITCH ENGAGED (%s): %s", source, reason)
        return state

    def release(self) -> None:
        """Clear the runtime and file switches.

        The environment variable is deliberately *not* cleared: a process
        cannot un-set its own configuration in any way that survives, and
        pretending otherwise would report a released switch that re-engages on
        the next read.
        """
        self._runtime = None
        if self.path is not None:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                log.error("Could not remove kill file %s: %s", self.path, exc)
        log.warning("Kill switch released")
