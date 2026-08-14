"""Authentication for a desk that is reachable from the internet.

The local desk authenticated with a token in the URL. On a home network that
is defensible: the operating system and the router are the real boundary, and
the token only stops a housemate. On the public internet it is not, and the
reasons are specific rather than theoretical -- a URL is the single worst place
to keep a credential. It lands in browser history, in `Referer` headers on any
outbound link, in proxy and CDN access logs, in screenshots, and in the "copy
link" a user sends to ask a question about the page. It also cannot expire, and
it defeats the `SameSite` cookie protection, because a request carrying its
credential in the query string is authenticated no matter which site caused it.

So a hosted desk gets the boring, well-understood construction instead:

**A password**, never stored -- only a scrypt hash of it, compared in constant
time. scrypt rather than a bare SHA because a password is low-entropy by
nature and the only defence against an offline guess is making each guess
expensive in memory as well as time.

**Sessions**, held server-side and referenced by a random cookie. The cookie is
`HttpOnly` (no script can read it), `SameSite=Strict` (no other site can cause
an authenticated request), and `Secure` whenever the connection is TLS. The
session id is rotated on login, so a fixation attempt cannot pre-plant one.

**A lockout**, because a password with no rate limit is a password that will be
guessed. Failures are counted per client address with an escalating delay.

**A CSRF token** for every state-changing request. `SameSite=Strict` already
covers this in every browser that honours it, and this is the belt to that
suspenders: the desk moves money, so it does not rest on one mechanism.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: scrypt parameters. n=2**15 with r=8 costs roughly 32 MB and ~100 ms per
#: guess on ordinary hardware -- unnoticeable on a login, ruinous for an
#: attacker working through a wordlist.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LEN = 32
#: OpenSSL refuses scrypt above a 32 MB working set by default, and these
#: parameters need 128*N*r = exactly 32 MiB plus overhead -- so the limit has
#: to be raised explicitly or every hash fails with "memory limit exceeded".
SCRYPT_MAXMEM = 128 * 1024 * 1024

#: A session outlives a phone screen locking, but not a laptop left in a cafe.
SESSION_TTL_S = 12 * 60 * 60
#: Idle sessions die sooner than absolute ones.
SESSION_IDLE_S = 2 * 60 * 60

#: Lockout. Five wrong guesses buys a minute, and it doubles from there, so a
#: script gets nowhere while a person who fat-fingered their password waits
#: about as long as it takes to notice.
LOCKOUT_AFTER = 5
LOCKOUT_BASE_S = 60.0
LOCKOUT_MAX_S = 15 * 60.0

#: Below this a password is not worth hashing -- say so at startup rather than
#: after someone has been brute-forced.
MIN_PASSWORD_CHARS = 12


class AuthError(RuntimeError):
    """A configuration problem the operator has to fix before serving."""


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash a password into a self-describing string.

    Format is `scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>`, so the parameters
    travel with the hash and can be raised later without invalidating what is
    already stored.
    """
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash, in constant time."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(hash_hex)),
            maxmem=SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError, MemoryError):
        # A malformed hash must fail closed, and must not be distinguishable
        # by timing from a wrong password.
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


@dataclass
class Session:
    token: str
    created_at: float
    last_seen: float
    csrf: str
    #: Recorded for the audit log, not for authorisation -- an address is
    #: trivially spoofable and pinning a session to one breaks every mobile
    #: network that rotates addresses mid-session.
    address: str = ""

    def expired(self, now: float) -> bool:
        return (
            now - self.created_at > SESSION_TTL_S
            or now - self.last_seen > SESSION_IDLE_S
        )


@dataclass
class Attempts:
    count: int = 0
    locked_until: float = 0.0


class Authenticator:
    """Password login, sessions, lockout and CSRF for one desk.

    Holds no plaintext password at any point: it is given a hash, and the
    plaintext it receives on login is compared and discarded.
    """

    def __init__(self, password_hash: str | None) -> None:
        self.password_hash = password_hash
        self.sessions: dict[str, Session] = {}
        self._attempts: dict[str, Attempts] = {}

    @property
    def enabled(self) -> bool:
        """False when no password is configured -- localhost-only operation."""
        return bool(self.password_hash)

    # ---------------- login ----------------

    def lockout_remaining(self, address: str, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        record = self._attempts.get(address)
        if record is None:
            return 0.0
        return max(0.0, record.locked_until - now)

    def login(
        self, password: str, address: str = "", now: float | None = None
    ) -> tuple[Session | None, str | None]:
        """Verify a password and mint a session.

        Returns `(session, error)`. The error text deliberately does not say
        whether a password was close, long enough, or the right shape.
        """
        now = now if now is not None else time.time()
        if not self.enabled:
            return None, "no password is configured"

        remaining = self.lockout_remaining(address, now)
        if remaining > 0:
            return None, f"too many attempts -- try again in {int(remaining) + 1}s"

        if not verify_password(password, self.password_hash or ""):
            record = self._attempts.setdefault(address, Attempts())
            record.count += 1
            if record.count >= LOCKOUT_AFTER:
                # Escalates from the first lockout, not from the first failure,
                # so the delay grows across repeated rounds of guessing.
                rounds = record.count - LOCKOUT_AFTER
                delay = min(LOCKOUT_BASE_S * (2**rounds), LOCKOUT_MAX_S)
                record.locked_until = now + delay
                log.warning(
                    "Desk login locked out for %s after %d failures (%.0fs)",
                    address or "unknown", record.count, delay,
                )
            return None, "incorrect password"

        self._attempts.pop(address, None)
        # A fresh id on every login: a session planted before authentication
        # must not survive it.
        session = Session(
            token=secrets.token_urlsafe(32),
            created_at=now, last_seen=now,
            csrf=secrets.token_urlsafe(32),
            address=address,
        )
        self.sessions[session.token] = session
        return session, None

    def logout(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)

    # ---------------- session checks ----------------

    def session_for(self, token: str | None, now: float | None = None) -> Session | None:
        """Return a live session, sweeping expired ones as it goes."""
        now = now if now is not None else time.time()
        self.sweep(now)
        if not token:
            return None
        session = self.sessions.get(token)
        if session is None:
            return None
        if session.expired(now):
            del self.sessions[session.token]
            return None
        session.last_seen = now
        return session

    def sweep(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        dead = [t for t, s in self.sessions.items() if s.expired(now)]
        for token in dead:
            del self.sessions[token]
        return len(dead)

    def check_csrf(self, session: Session | None, presented: str | None) -> bool:
        if session is None:
            return False
        if not presented:
            return False
        return hmac.compare_digest(presented, session.csrf)


def password_hash_from_env() -> str | None:
    """Read the configured password, hashing a plaintext one if that is what
    was supplied.

    Two variables rather than one, because they have different handling:
    `DESK_PASSWORD_HASH` is what a careful deploy sets (the plaintext never
    reaches the host), while `DESK_PASSWORD` is what a one-click deploy can
    actually manage, and refusing to support it would only push people toward
    running with no password at all.
    """
    stored = (os.getenv("DESK_PASSWORD_HASH") or "").strip()
    if stored:
        if not stored.startswith("scrypt$"):
            raise AuthError(
                "DESK_PASSWORD_HASH is not a hash produced by this app. "
                "Generate one with: python -m kbot.webui hash-password"
            )
        return stored

    plain = os.getenv("DESK_PASSWORD") or ""
    if not plain.strip():
        return None
    if len(plain) < MIN_PASSWORD_CHARS:
        raise AuthError(
            f"DESK_PASSWORD is too short ({len(plain)} characters). Use at "
            f"least {MIN_PASSWORD_CHARS}. This password is the only thing "
            "between the internet and a port that places trades."
        )
    return hash_password(plain)
