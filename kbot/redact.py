"""Keep secrets out of logs, exceptions and messages.

Nothing here is a substitute for not logging a secret in the first place. It
exists because the leak is rarely a line someone wrote deliberately: the
Telegram token is *inside* the API URL, so every request httpx logs at INFO
carries it, and a traceback from a failed call carries it again in the
exception text. Neither of those is a log statement anyone in this project
wrote.

Two layers:

* `Redactor` holds the exact secret strings this process knows about and
  removes them from any text.
* `install()` attaches it to the root logger as a filter, so it applies to
  third-party libraries too -- which is the whole point, since that is where
  the leaks are.

Patterns are matched as well as exact strings, so a token from somewhere this
process never saw (a user pasting one into a chat, a key in an error body from
an upstream service) is caught too.
"""

from __future__ import annotations

import logging
import re

#: Shapes worth catching even when the exact value is unknown to us.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Telegram bot tokens: digits, colon, 35 URL-safe characters.
    ("TELEGRAM_TOKEN", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")),
    # PEM private keys, including the newline-escaped form that survives .env.
    (
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN[^-]*PRIVATE KEY-----.*?-----END[^-]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # Fernet keys: 43 urlsafe-base64 characters and a trailing '='. A \b
    # boundary is no use on either end -- '-' and '_' are not word characters
    # and '=' is not either -- so the edges are asserted explicitly.
    ("MASTER_KEY", re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_-])")),
    # Authorization headers, whatever the scheme. Everything to end of line:
    # \S+ stops at the scheme and leaves the credential itself in the log.
    ("AUTHORIZATION", re.compile(r"(?i)(authorization\s*[:=]\s*).+")),
)

#: What a redacted value is replaced with. Distinctive so it is obvious in a
#: log that redaction happened, rather than looking like a truncation.
def _mask(label: str) -> str:
    return f"[REDACTED:{label}]"


class Redactor:
    """Removes known secrets, and secret-shaped text, from arbitrary strings."""

    #: Below this length an "exact secret" is too generic to blanket-replace --
    #: redacting a 3-character value would corrupt unrelated log lines.
    MIN_EXACT = 8

    def __init__(self) -> None:
        self._exact: list[tuple[str, str]] = []

    def add(self, secret: str | None, label: str = "SECRET") -> None:
        if not secret:
            return
        secret = secret.strip()
        if len(secret) < self.MIN_EXACT:
            return
        if any(secret == existing for existing, _ in self._exact):
            return
        self._exact.append((secret, label))
        # Longest first, so a token is not partially replaced by a shorter
        # secret that happens to be a substring of it.
        self._exact.sort(key=lambda pair: len(pair[0]), reverse=True)

    def scrub(self, text: str) -> str:
        if not text:
            return text
        for secret, label in self._exact:
            if secret in text:
                text = text.replace(secret, _mask(label))
        for label, pattern in PATTERNS:
            if label == "AUTHORIZATION":
                text = pattern.sub(lambda m: m.group(1) + _mask(label), text)
            else:
                text = pattern.sub(_mask(label), text)
        return text

    def __call__(self, text: str) -> str:
        return self.scrub(text)


class RedactingFilter(logging.Filter):
    """Scrubs the formatted message and any exception text on every record.

    Attached to the root logger, so it covers libraries as well as this
    package -- httpx logging a request URL that contains the Telegram token is
    the leak that motivated all of this, and no amount of care inside kbot
    would have prevented it.
    """

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format must not lose the line
            return True

        cleaned = self.redactor.scrub(message)
        if cleaned != message:
            # Replace the arguments as well, or the next formatter re-expands
            # the original and undoes this.
            record.msg = cleaned
            record.args = ()

        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            scrubbed_args = tuple(
                self.redactor.scrub(a) if isinstance(a, str) else a
                for a in getattr(exc, "args", ())
            )
            if scrubbed_args != getattr(exc, "args", ()):
                try:
                    exc.args = scrubbed_args
                except Exception:  # noqa: BLE001 - some exceptions are immutable
                    pass
        return True


#: Process-wide, because the filter has to be reachable from wherever a secret
#: is first learned -- a user connecting a Kalshi key mid-session, for example.
redactor = Redactor()


def install(*, secrets: dict[str, str | None] | None = None) -> Redactor:
    """Attach redaction to the root logger. Safe to call more than once."""
    for label, value in (secrets or {}).items():
        redactor.add(value, label)

    root = logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for f in root.filters):
        root.addFilter(RedactingFilter(redactor))

    # A filter on the logger does not run for records emitted by *child*
    # loggers, only for those logged directly to root -- so it goes on the
    # handlers too, which every record passes through.
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter(redactor))
    return redactor
