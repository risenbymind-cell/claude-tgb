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


def test_demo_writes_the_variable_that_actually_selects_a_host():
    """-Demo wrote KALSHI_DEMO, which stopped selecting anything the moment
    TRADING_MODE took over. The switch became a silent no-op."""
    text = Path("scripts/run-windows.ps1").read_text(encoding="ascii")
    assert 'Set-EnvValue "TRADING_MODE" "demo-live"' in text
    assert 'Remove-EnvValue "KALSHI_DEMO"' in text, (
        "a stale KALSHI_DEMO becomes a startup error once TRADING_MODE "
        "disagrees with it"
    )


def test_no_switch_can_reach_production():
    """Real orders take two variables set by hand. A command-line flag -- or a
    typo next to one -- must not be able to get there."""
    text = Path("scripts/run-windows.ps1").read_text(encoding="ascii")
    # Reading the value to label the banner is fine, and so is a comment
    # explaining the rule. Writing either variable is not.
    writes = re.findall(r"Set-EnvValue\s+\"([A-Z_]+)\"\s+\"([^\"]*)\"", text)
    for name, value in writes:
        assert name != "ALLOW_PRODUCTION_ORDERS", "the script must not grant the ack"
        assert value != "production-live", (
            f"Set-EnvValue {name}=production-live: a switch must not be able to "
            "turn on real orders"
        )


def test_an_incomplete_env_file_is_repaired_rather_than_skipped():
    """A .env copied from .env.example exists but has no token in it. Keying
    the wizard off existence alone left those users stuck on a config error
    with nothing telling them how to get out of it."""
    text = Path("scripts/run-windows.ps1").read_text(encoding="ascii")
    assert 'Get-EnvValue "TELEGRAM_BOT_TOKEN"' in text
    assert "kbot.tools genkey" in text, "a blank MASTER_KEY must be generated"
    assert 'if (-not (Get-EnvValue "MASTER_KEY"))' in text, (
        "MASTER_KEY must only be written when absent -- overwriting one strands "
        "every credential already encrypted under it"
    )


def test_the_wizard_is_forced_when_a_tokenless_env_is_in_the_way():
    """`kbot.tools setup` refuses to overwrite an existing .env without
    --force, so calling it plainly meant the repair path could never repair
    anything -- it printed 'Nothing was changed' and died."""
    text = Path("scripts/run-windows.ps1").read_text(encoding="ascii")
    assert '"--force"' in text, "the wizard cannot rewrite a .env without it"
    assert "Copy-Item $envPath $backup" in text, (
        "forcing over a config file requires keeping a copy of it first"
    )


def test_env_backups_cannot_be_committed():
    """A backup of a .env holds a live bot token."""
    ignored = Path(".gitignore").read_text().splitlines()
    assert ".env.*" in ignored, ".env.bak-* would otherwise be committable"
    assert "!.env.example" in ignored, ".env.example must stay tracked"


def test_the_failing_checks_are_repeated_where_they_can_be_read():
    """The preflight output scrolls off the top the instant the bot starts
    logging. "1 problem(s)" with the problem itself gone is the least useful
    thing this could tell someone whose bot is not working."""
    text = Path("scripts/run-windows.ps1").read_text(encoding="ascii")
    assert "$doctorOutput" in text, "preflight output must be captured, not just shown"
    assert "$doctorProblems" in text, "the failing lines must be repeated at start"


def test_python_runs_in_utf8_mode():
    """The preflight prints check marks; a legacy codepage cannot encode them."""
    text = Path("scripts/run-windows.ps1").read_text(encoding="ascii")
    assert 'PYTHONUTF8 = "1"' in text


def test_a_degraded_preflight_does_not_stop_the_bot_starting():
    """`doctor` exits 1 when it cannot reach Kalshi, and on a home connection
    that is usually a blip or a boot that beat the Wi-Fi. Refusing to start
    would hand a transient failure to the operator; the restart loop is what
    is supposed to absorb it. Only exit 2 -- a bad .env -- stops us."""
    text = Path("scripts/run-windows.ps1").read_text(encoding="ascii")
    gate = text.split("--- preflight")[1].split("--- run")[0]
    assert "$doctor -eq 2" in gate, "a bad .env must still stop the script"
    assert "exit 1" not in gate, (
        "a preflight warning must not prevent the bot from starting"
    )


PS7_ONLY = {
    "null-coalescing (??)": r"\?\?",
    "pipeline chain (&&, ||)": r"&&|\|\|",
    "ternary (? :)": r"\)\s*\?\s+\S+\s+:\s",
    "utf8NoBOM encoding": r"utf8NoBOM",
    "ForEach-Object -Parallel": r"ForEach-Object\s+-Parallel",
}


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_powershell_7_only_syntax(script: Path):
    """Windows ships 5.1. Every one of these parses on the developer's machine
    and fails on the user's -- `||` is what broke the first setup attempt."""
    text = script.read_text(encoding="ascii")
    found = [name for name, pattern in PS7_ONLY.items() if re.search(pattern, text)]
    assert not found, f"{script} uses PowerShell 7-only syntax: {found}"
