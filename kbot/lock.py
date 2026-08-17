"""One trader per account, enforced.

Two copies of this process against the same Kalshi account do not conflict in
any way either of them can see. Each discovers the same markets, each fires on
the same signal, and each places its own order — so the account ends up with
double the intended position, twice the fees, and twice the exposure, while
both processes report exactly what they were asked to do. Nothing in the
ledger, the reconciliation or the risk caps notices, because each instance's
own books balance perfectly.

That is the worst shape a bug can take in a system that spends money: correct
from every vantage point inside it.

The guard is an exclusive lock on a file next to the database, held for the
life of the process:

* **`flock`** rather than a PID file. A PID file is a record of an intention;
  a lock is a fact held by the kernel. It is released automatically when the
  process dies, however it dies, so a crash cannot leave a stale lock that
  needs manual clearing at exactly the moment someone is trying to restart.
* **Next to the database**, because the database is what defines "the same
  deployment". Two processes pointed at different data directories are two
  deployments and may legitimately both run.
* **Advisory only.** It stops this program from racing itself. It is not a
  defence against a determined operator, and does not pretend to be.

Paper and sandbox instances take a *different* lock name from live ones: two
simulations racing each other cost nothing, and refusing to run a paper desk
because a live bot is up would be an obstacle with no safety value.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    """Another instance holds the lock for this data directory."""


class InstanceLock:
    """An exclusive, self-releasing lock on one data directory.

    Usable as a context manager, or held for the process lifetime by simply
    not closing it — the kernel releases it when the process exits.
    """

    def __init__(self, path: Path, *, label: str = "trader") -> None:
        self.path = Path(path)
        self.label = label
        self._fh = None

    @classmethod
    def for_data_dir(
        cls, db_path: Path, *, places_real_orders: bool
    ) -> "InstanceLock":
        """The lock a process should take, given where its data lives.

        Simulated and real instances are deliberately separated: two paper
        desks racing cost nothing, and blocking one because a live bot is
        running would be friction with no safety benefit.
        """
        directory = Path(db_path).parent
        label = "live" if places_real_orders else "paper"
        return cls(directory / f".{label}.lock", label=label)

    def acquire(self) -> None:
        # POSIX only. On Windows the runner is a single console process and
        # the failure this guards against needs two daemons, so the lock is
        # skipped rather than faked with something weaker.
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows
            log.warning(
                "No flock on this platform; not guarding against a second "
                "instance. Do not run two copies against one account."
            )
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Opened for append so the file is created if missing and never
        # truncated -- truncation would destroy the previous holder's note
        # before we know whether we can have the lock.
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.seek(0)
            holder = self._fh.read().strip() or "unknown"
            self._fh.close()
            self._fh = None
            raise AlreadyRunning(
                f"Another {self.label} instance is already running for this "
                f"data directory ({holder}).\n\n"
                "Two instances against one Kalshi account would double every "
                "position, and neither would notice: each one's own ledger "
                "would balance perfectly.\n\n"
                f"Stop the other process, or point this one at a different "
                f"DB_PATH.\n"
                f"Lock file: {self.path}"
            ) from None

        # Record who holds it, for the message the *next* process will print.
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"pid {os.getpid()}")
        self._fh.flush()
        log.info("Holding the %s instance lock at %s", self.label, self.path)

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        finally:
            self._fh.close()
            self._fh = None

    # The lock file is deliberately NOT deleted on release. Unlinking it races
    # with another process that has already opened it and is waiting -- that
    # process would end up holding a lock on a file no longer at this path,
    # and a third could then take the new one. An empty file costs nothing.

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
