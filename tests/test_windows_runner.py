"""The Windows runner has to survive Windows PowerShell 5.1.

Every check here is a failure that already happened, or one whose failure mode
is a parse error before a single line executes -- which is the worst kind,
because it points at the wrong line and cascades.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPTS = sorted(Path("scripts").glob("*.ps1"))


def test_there_is_a_windows_runner():
    assert SCRIPTS, "scripts/run-windows.ps1 is what Windows users are told to run"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_the_script_is_pure_ascii(script: Path):
    """A BOM-less file is read as Windows-1252 by PowerShell 5.1.

    A UTF-8 em dash (E2 80 94) decodes there as three characters, the last of
    which is U+201D -- a curly right double quote, which PowerShell honours as
    a string delimiter. One em dash inside a double-quoted string closes it
    early and every brace after it is reported as unbalanced.
    """
    raw = script.read_bytes()
    offenders = sorted({b for b in raw if b > 0x7F})
    assert not offenders, (
        f"{script} contains non-ASCII bytes {[hex(b) for b in offenders]}. "
        "Windows PowerShell will mis-decode them and fail to parse the file."
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_quotes_and_braces_balance(script: Path):
    """A cheap structural check -- it catches exactly the cascade above."""
    text = script.read_text(encoding="ascii")
    # Strip here-strings and comments, neither of which nest.
    text = re.sub(r'@"[\s\S]*?"@', "", text)
    text = re.sub(r"(?m)^\s*#.*$", "", text)

    assert text.count('"') % 2 == 0, f"{script} has an odd number of double quotes"
    assert text.count("{") == text.count("}"), f"{script} has unbalanced braces"
    assert text.count("(") == text.count(")"), f"{script} has unbalanced parens"


def test_the_runner_restarts_but_not_on_a_config_error():
    """Nothing supervises a home PC, so the script must supervise itself --
    except on exit 2, where restarting only rate-limits the Telegram token."""
    text = Path("scripts/run-windows.ps1").read_text(encoding="ascii")
    assert "while ($true)" in text, "the bot must be restarted when it dies"
    assert "$code -eq 2" in text, "a config error must stop the loop"
    assert "Start-Sleep" in text, "restarts must back off"


def test_demo_is_written_to_the_env_file_not_just_exported():
    """A process variable would leave `doctor` and /status reporting
    production while the operator believes they are on demo."""
    text = Path("scripts/run-windows.ps1").read_text(encoding="ascii")
    assert 'Set-EnvValue "KALSHI_DEMO" "true"' in text
    assert 'Set-EnvValue "KALSHI_DEMO" "false"' in text


def test_python_runs_in_utf8_mode():
    """The preflight prints check marks; a legacy codepage cannot encode them."""
    text = Path("scripts/run-windows.ps1").read_text(encoding="ascii")
    assert 'PYTHONUTF8 = "1"' in text
