"""Secrets must not reach logs, exceptions, or messages.

The leak that motivated this is not a log statement anyone wrote: the Telegram
token is inside the API URL, and httpx logs request URLs. So the tests that
matter most are the ones covering text this project never formatted.
"""

from __future__ import annotations

import logging

import pytest

from kbot.redact import Redactor, RedactingFilter, install

TOKEN = "8123456789:AAFm3kQ9xZ-vB2nLpQr7sT4uVwXyZaBcDeF"
FERNET = "hcFEd3yCJ0RRfW6PY4kJ1YQ3-uWzYPr8GaTbTQpVtNc="
PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAx7Fake+Key/Body==\n"
    "-----END RSA PRIVATE KEY-----"
)


@pytest.fixture()
def redactor():
    return Redactor()


def test_a_known_secret_is_removed(redactor):
    redactor.add(TOKEN, "TELEGRAM_TOKEN")
    out = redactor.scrub(f"calling https://api.telegram.org/bot{TOKEN}/sendMessage")
    assert TOKEN not in out
    assert "REDACTED:TELEGRAM_TOKEN" in out


def test_a_telegram_token_is_caught_without_being_registered(redactor):
    """A user pasting someone else's token into a chat is still a token in a
    log line, and this process has no way to know its value in advance."""
    out = redactor.scrub(f"user sent {TOKEN} in a message")
    assert TOKEN not in out


def test_a_private_key_is_caught_by_shape(redactor):
    out = redactor.scrub(f"failed to parse: {PEM}")
    assert "PRIVATE KEY-----\nMIIE" not in out
    assert "REDACTED:PRIVATE_KEY" in out


def test_an_escaped_newline_private_key_is_caught(redactor):
    """The form that survives a .env file."""
    escaped = PEM.replace("\n", "\\n")
    out = redactor.scrub(f"KALSHI_PRIVATE_KEY={escaped}")
    assert "MIIEowIBAAKCAQEA" not in out


def test_a_fernet_key_is_caught_by_shape(redactor):
    out = redactor.scrub(f"MASTER_KEY={FERNET}")
    assert FERNET not in out


def test_an_authorization_header_is_stripped(redactor):
    out = redactor.scrub("Authorization: Bearer abcdef123456789")
    assert "abcdef123456789" not in out
    assert "Authorization" in out, "the header name is useful; the value is not"


def test_a_short_value_is_not_blanket_replaced(redactor):
    """Redacting a 3-character secret would corrupt unrelated lines."""
    redactor.add("abc", "SHORT")
    assert redactor.scrub("abc is a normal word fragment") == (
        "abc is a normal word fragment"
    )


def test_the_longest_secret_wins(redactor):
    """A shorter secret that is a substring of a longer one must not
    partially replace it, leaving the remainder visible."""
    redactor.add(TOKEN, "TELEGRAM_TOKEN")
    redactor.add(TOKEN[:20], "PREFIX")
    out = redactor.scrub(TOKEN)
    assert TOKEN not in out
    assert "AAFm3kQ9xZ" not in out


def test_ordinary_text_is_untouched(redactor):
    redactor.add(TOKEN, "TELEGRAM_TOKEN")
    text = "Recorded 2142 snapshots, 9 markets live, feed REST"
    assert redactor.scrub(text) == text


# ---------------- the logging integration ----------------


def test_the_filter_scrubs_a_formatted_log_record(caplog):
    r = Redactor()
    r.add(TOKEN, "TELEGRAM_TOKEN")
    log = logging.getLogger("test.redact")
    log.addFilter(RedactingFilter(r))

    with caplog.at_level(logging.INFO, logger="test.redact"):
        log.info("GET https://api.telegram.org/bot%s/getMe", TOKEN)

    assert TOKEN not in caplog.text
    assert "REDACTED" in caplog.text


def test_arguments_are_cleared_so_the_formatter_cannot_re_expand_them():
    """Scrubbing the message but leaving record.args means the next formatter
    rebuilds the original and undoes the whole thing."""
    r = Redactor()
    r.add(TOKEN, "TELEGRAM_TOKEN")
    f = RedactingFilter(r)
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "url=%s", (TOKEN,), None
    )
    f.filter(record)
    assert TOKEN not in record.getMessage()


def test_exception_text_is_scrubbed():
    """A traceback from a failed Telegram call carries the URL, and therefore
    the token, without anything having logged it deliberately."""
    r = Redactor()
    r.add(TOKEN, "TELEGRAM_TOKEN")
    f = RedactingFilter(r)
    try:
        raise ValueError(f"401 from https://api.telegram.org/bot{TOKEN}/getMe")
    except ValueError as exc:
        record = logging.LogRecord(
            "x", logging.ERROR, __file__, 1, "failed", (), (type(exc), exc, None)
        )
        f.filter(record)
        assert TOKEN not in str(exc)


def test_install_is_idempotent():
    root = logging.getLogger()
    before = len(root.filters)
    install(secrets={"A": TOKEN})
    install(secrets={"A": TOKEN})
    after = len(root.filters)
    assert after - before <= 1


def test_a_record_with_broken_formatting_is_not_dropped():
    """Losing a log line to the redactor would be its own kind of failure."""
    f = RedactingFilter(Redactor())
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%d", ("nope",), None)
    assert f.filter(record) is True
