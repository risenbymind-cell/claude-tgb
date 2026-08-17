"""Checks that run against the live process, on demand.

The unit tests prove the code is correct. They say nothing about whether *this
deployment* can reach Kalshi, whether its clock is close enough for a
signature to be accepted, whether the disk it was pointed at is writable, or
whether the key someone pasted last week still works. Those are the failures
that happen to a running system, and every one of them is silent until an
order is refused.

So each check here does the real thing rather than inspecting configuration: it
makes the request, writes the file, signs the message. A check that reads a
setting and reports OK is worse than no check, because it converts an unknown
into a false reassurance.

Ordered cheapest and most local first, so an obvious local fault is not
reported underneath a network timeout.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: A check that has not answered in this long is a failure in itself -- the
#: point of the panel is a quick verdict, and a hung probe gives none.
CHECK_TIMEOUT_S = 15.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    #: True when this failing does not stop the desk working -- an unset
    #: optional credential, say. Kept distinct from ok so the summary can be
    #: honest without being alarming.
    advisory: bool = False
    duration_ms: float = 0.0

    @property
    def status(self) -> str:
        if self.ok:
            return "pass"
        return "warn" if self.advisory else "fail"


@dataclass
class SelfTestReport:
    checks: list[Check] = field(default_factory=list)
    started_at: float = 0.0
    duration_ms: float = 0.0

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.advisory]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.advisory]

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        passed = sum(1 for c in self.checks if c.ok)
        if self.ok and not self.warnings:
            return f"All {len(self.checks)} checks passed."
        parts = [f"{passed}/{len(self.checks)} passed"]
        if self.failures:
            parts.append(f"{len(self.failures)} failed")
        if self.warnings:
            parts.append(f"{len(self.warnings)} advisory")
        return ", ".join(parts) + "."

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "summary": self.summary(),
            "duration_ms": round(self.duration_ms, 1),
            "checks": [
                {
                    "name": c.name, "status": c.status, "ok": c.ok,
                    "advisory": c.advisory, "detail": c.detail,
                    "duration_ms": round(c.duration_ms, 1),
                }
                for c in self.checks
            ],
        }


async def _timed(name: str, coro, *, advisory: bool = False) -> Check:
    """Run one check, converting a hang or a crash into a result.

    A self-test that can itself raise is a self-test that reports nothing at
    the moment it is most needed.
    """
    started = time.perf_counter()
    try:
        ok, detail = await asyncio.wait_for(coro, timeout=CHECK_TIMEOUT_S)
    except asyncio.TimeoutError:
        ok, detail = False, f"timed out after {CHECK_TIMEOUT_S:.0f}s"
    except Exception as exc:  # noqa: BLE001 - a failed check is a result
        ok, detail = False, f"{type(exc).__name__}: {exc}"[:200]
    return Check(
        name=name, ok=ok, detail=detail, advisory=advisory,
        duration_ms=(time.perf_counter() - started) * 1000,
    )


# ---------------- the checks ----------------


async def _check_strategies() -> tuple[bool, str]:
    from ..strategy import get_strategy, strategy_names

    names = strategy_names()
    if not names:
        return False, "no strategies are registered"
    for name in names:
        get_strategy(name)
    return True, f"{len(names)} loaded: {', '.join(names)}"


async def _check_data_dir(desk) -> tuple[bool, str]:
    """Actually write something. A directory that exists is not a directory
    that can be written to -- read-only volumes and full disks both look
    perfectly normal until the first write."""
    path = desk.settings.db_path.parent
    probe = path / ".selftest"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text(str(time.time()))
        probe.unlink()
    except OSError as exc:
        return False, f"{path} is not writable: {exc}"
    return True, f"{path} is writable"


async def _check_disk_space(desk) -> tuple[bool, str]:
    import shutil

    # The directory may not exist yet on a fresh deployment, so measure the
    # nearest parent that does. Reporting "no disk" because a directory has
    # not been created is a false alarm about the wrong thing.
    target = desk.settings.db_path.parent.resolve()
    while not target.exists() and target != target.parent:
        target = target.parent

    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        return False, f"could not stat the filesystem: {exc}"
    free_mb = usage.free / (1024 * 1024)
    # Recordings run about 25 MB/day, so a few hundred MB is days of runway.
    if free_mb < 100:
        return False, f"{free_mb:.0f} MB free -- recording will stop soon"
    return True, f"{free_mb / 1024:.1f} GB free"


async def _check_kill_switch(desk) -> tuple[bool, str]:
    """Read it, which is the operation that matters.

    An unreadable kill file is treated as engaged, so a fault here fails
    toward not trading -- but it should be visible rather than discovered
    when someone reaches for it.
    """
    state = desk.kill.state()
    if state.engaged:
        return True, f"readable, currently ENGAGED ({state.describe()})"
    return True, "readable, currently clear"


async def _check_kalshi_reachable(desk) -> tuple[bool, str]:
    """A real request to the host this process would actually trade on."""
    started = time.perf_counter()
    resp = await desk._http.get(f"{desk.settings.rest_base}/exchange/status")
    ms = (time.perf_counter() - started) * 1000
    if resp.status_code >= 500:
        return False, f"{desk.settings.rest_base} returned {resp.status_code}"
    return True, f"{resp.status_code} from {desk.settings.rest_base} in {ms:.0f}ms"


async def _check_clock(desk) -> tuple[bool, str]:
    """Kalshi signs each request with a timestamp and rejects one too far from
    its own clock. Drift does not fail at boot -- it fails mid-session as an
    authentication error, which reads like a bad key."""
    from ..safety import measure_clock_drift

    check = await measure_clock_drift(desk.settings.rest_base, client=desk._http)
    return check.ok, check.detail


async def _check_markets(desk) -> tuple[bool, str]:
    """Discovery returning nothing is the failure that produced nine hours of
    empty recordings while Kalshi had markets open."""
    markets = await desk.discovery.refresh(desk.settings.coins)
    if not markets:
        return False, (
            "no live markets found. Kalshi opens a fresh 15-minute set every "
            "quarter hour; if this persists, check `python -m kbot.tools markets`"
        )
    return True, f"{len(markets)} live: {', '.join(sorted(markets))}"


async def _check_credentials(desk) -> tuple[bool, str]:
    """Sign something, rather than checking that a key is present."""
    if desk._signer is None:
        return False, "none connected -- market data only, no orders possible"
    try:
        headers = desk._signer.headers("GET", "/trade-api/v2/portfolio/balance")
    except Exception as exc:  # noqa: BLE001
        return False, f"key present but cannot sign: {exc}"
    if not headers.get("KALSHI-ACCESS-SIGNATURE"):
        return False, "signing produced no signature"
    return True, "key signs correctly"


async def _check_order_path(desk) -> tuple[bool, str]:
    """What would happen if a signal fired right now.

    Never places an order. It reports the same gate the trading path consults,
    so the answer here and the behaviour there cannot disagree.
    """
    blocked = desk.blocked_reason()
    if desk.sandbox:
        return True, "sandbox: simulated fills only, no order path by design"
    if not desk.settings.mode.places_real_orders:
        return True, "paper: fills simulated, nothing sent to Kalshi"
    if blocked:
        return False, f"orders blocked: {blocked}"
    return True, f"{desk.settings.mode.value}: orders would reach Kalshi"


async def _check_recordings(desk) -> tuple[bool, str]:
    """Advisory: the desk runs fine without recordings, but nothing can be
    concluded without them."""
    payload = desk.research()
    if not payload.get("available"):
        return False, payload.get("error", "no recordings directory")
    settled = payload.get("settled", 0)
    if settled == 0:
        return False, "nothing recorded yet -- start the recorder"
    if not payload.get("recording_now"):
        age = payload.get("last_sample_age_s") or 0
        return False, (
            f"{settled} settled markets, but nothing captured for "
            f"{age / 3600:.1f}h -- the recorder is not running"
        )
    return True, f"recording now, {settled} settled markets so far"


# ---------------- the run ----------------


async def run_selftest(desk) -> SelfTestReport:
    """Every check, in order, against the live process."""
    report = SelfTestReport(started_at=time.time())
    started = time.perf_counter()

    # Local and instant first, so an obvious local fault is not reported
    # underneath a network timeout.
    report.checks.append(await _timed("strategies", _check_strategies()))
    report.checks.append(await _timed("data directory", _check_data_dir(desk)))
    report.checks.append(await _timed("disk space", _check_disk_space(desk)))
    report.checks.append(await _timed("kill switch", _check_kill_switch(desk)))
    report.checks.append(await _timed("order path", _check_order_path(desk)))
    report.checks.append(
        await _timed("credentials", _check_credentials(desk), advisory=True)
    )

    # Then the network.
    report.checks.append(await _timed("kalshi reachable", _check_kalshi_reachable(desk)))
    report.checks.append(await _timed("clock sync", _check_clock(desk)))
    report.checks.append(await _timed("market discovery", _check_markets(desk)))
    report.checks.append(
        await _timed("recordings", _check_recordings(desk), advisory=True)
    )

    report.duration_ms = (time.perf_counter() - started) * 1000
    return report
