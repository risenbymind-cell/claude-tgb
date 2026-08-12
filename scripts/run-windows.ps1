# Run DirectionalBot on a Windows PC, forever.
#
#   powershell -ExecutionPolicy Bypass -File scripts\run-windows.ps1 -Demo
#
# Builds the venv, runs the setup wizard the first time, runs the preflight,
# then supervises the bot: if it crashes or the network drops, it restarts.
# Safe to re-run -- an existing .env is left alone.
#
#   -Demo       TRADING_MODE=demo-live: real orders on Kalshi's demo exchange,
#               spending demo funds. Tests the order code, not the strategy.
#   -Live       back to TRADING_MODE=paper. It does NOT mean production --
#               sending real orders needs two variables set by hand, and a
#               command-line switch must not be able to do it.
#   -Recorder   run the market recorder instead of the bot
#   -Once       don't restart on exit; run a single time
#
# Run it twice in two windows -- once plain, once with -Recorder -- if you want
# data collecting while the bot trades.

param(
    [switch]$Recorder,
    [switch]$SkipChecks,
    [switch]$Demo,
    [switch]$Live,
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Windows PowerShell 5.1 reads a BOM-less script as Windows-1252, so a UTF-8
# em dash arrives as three characters ending in a curly right quote -- which
# PowerShell accepts as a string delimiter, closing a string early and taking
# the rest of the file down with it. This script is therefore kept pure ASCII;
# tests/test_windows_runner.py enforces that.
#
# The Python side has the mirror problem: a legacy console codepage cannot
# encode the check marks the wizard and preflight print, and that is a crash
# when output is redirected. UTF-8 mode costs nothing and removes it.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# Say which commit this is, and whether it is the current one. Running a stale
# copy after a `git clone` that failed with "already exists" looks exactly like
# a bug in the current code, and costs a round-trip to work out that it is not.
try {
    $head = (git rev-parse --short HEAD 2>$null)
    if ($LASTEXITCODE -eq 0 -and $head) {
        Write-Host "DirectionalBot $head" -ForegroundColor DarkGray
        git fetch --quiet 2>$null
        $behind = (git rev-list --count "HEAD..@{u}" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $behind -and [int]$behind -gt 0) {
            Write-Host ""
            Write-Host "You are $behind commit(s) behind. Run 'git pull' -- the fix for" -ForegroundColor Yellow
            Write-Host "whatever you are about to hit may already be there." -ForegroundColor Yellow
            Write-Host ""
        }
    }
} catch {
    # No git, no network, no upstream: none of that should stop the bot.
}

function Fail($message) {
    Write-Host ""
    Write-Host $message -ForegroundColor Red
    exit 1
}

# --- Python -----------------------------------------------------------------

# Each candidate is exe plus its own fixed arguments -- `py` needs `-3` to pick
# an interpreter, the others take none. Kept as arrays so nothing has to be
# re-split later, which is where a stray empty argument would creep in.
$candidates = @(
    @{ exe = "py";      args = @("-3") },
    @{ exe = "python";  args = @() },
    @{ exe = "python3"; args = @() }
)

$python = $null
foreach ($candidate in $candidates) {
    if (-not (Get-Command $candidate.exe -ErrorAction SilentlyContinue)) { continue }
    $probe = @($candidate.args) + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
    $version = & $candidate.exe @probe 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $version) { continue }
    if ([version]$version -ge [version]"3.10") {
        $python = $candidate
        break
    }
}

if (-not $python) {
    Fail @"
Python 3.10 or newer was not found.

Install it from https://www.python.org/downloads/ and tick
"Add python.exe to PATH" on the first screen of the installer.
Then close this window, open a new one, and run this script again.
"@
}

Write-Host ("Python: $($python.exe) $($python.args -join ' ')").Trim() -ForegroundColor DarkGray

# --- venv -------------------------------------------------------------------

$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating the virtual environment..." -ForegroundColor Cyan
    $venvArgs = @($python.args) + @("-m", "venv", ".venv")
    & $python.exe @venvArgs
    if ($LASTEXITCODE -ne 0) { Fail "Could not create .venv" }
}

Write-Host "Installing dependencies..." -ForegroundColor Cyan
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "Dependency install failed" }

# --- config -----------------------------------------------------------------

$envPath = Join-Path $root ".env"

function Get-EnvValue($name) {
    if (-not (Test-Path $envPath)) { return $null }
    foreach ($line in Get-Content $envPath) {
        if ($line -match "^\s*$([regex]::Escape($name))\s*=\s*(.*?)\s*$") {
            return $Matches[1]
        }
    }
    return $null
}

# -Demo and -Live rewrite one line of .env rather than setting a process
# variable, so the choice survives a restart of this script and shows up in
# `doctor` and `/status` -- there is no way to be running against production
# while believing you are on demo.
function Remove-EnvValue($name) {
    if (-not (Test-Path $envPath)) { return }
    $pattern = "^\s*#?\s*$([regex]::Escape($name))\s*="
    Set-Content -Path $envPath -Encoding UTF8 -Value (
        @(Get-Content $envPath) | Where-Object { $_ -notmatch $pattern }
    )
}

function Set-EnvValue($name, $value) {
    $lines = @()
    if (Test-Path $envPath) { $lines = @(Get-Content $envPath) }
    $pattern = "^\s*#?\s*$([regex]::Escape($name))\s*="
    if ($lines -match $pattern) {
        $lines = $lines | ForEach-Object {
            if ($_ -match $pattern) { "$name=$value" } else { $_ }
        }
    } else {
        $lines += "$name=$value"
    }
    Set-Content -Path $envPath -Value $lines -Encoding UTF8
}

# A .env can exist and still be unusable -- copied from .env.example, or left
# behind by a wizard that was interrupted. Existence is therefore not the test;
# having a token is, because that is the one value nothing here can invent.
$token = Get-EnvValue "TELEGRAM_BOT_TOKEN"
$haveToken = $token -and $token -notmatch "your-bot-token"

if (-not $haveToken) {
    Write-Host ""
    # The wizard refuses to overwrite an existing .env without --force, which is
    # right when the file is someone's real configuration and wrong here, where
    # we have already established it has no token in it. Keep a copy either way
    # rather than deciding on their behalf that nothing in it mattered.
    $setupArgs = @("-m", "kbot.tools", "setup")
    if (Test-Path $envPath) {
        $backup = "$envPath.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
        Copy-Item $envPath $backup
        $setupArgs += "--force"
        Write-Host "The .env here has no bot token yet -- let's finish it." -ForegroundColor Cyan
        Write-Host "The old one is saved as $(Split-Path -Leaf $backup)." -ForegroundColor DarkGray
    } else {
        Write-Host "First run -- let's write the configuration." -ForegroundColor Cyan
    }
    Write-Host "You need two things: your bot token from @BotFather, and your"
    Write-Host "Telegram user ID from @userinfobot. Everything else is generated."
    Write-Host ""
    & $venvPython @setupArgs
    if ($LASTEXITCODE -ne 0) { Fail "Setup did not complete. Nothing was written." }
}

# MASTER_KEY is generated, never typed, so an empty one is a gap to fill rather
# than a question to ask. Only ever written when absent: overwriting it would
# strand every Kalshi credential already encrypted under the old one.
if (-not (Get-EnvValue "MASTER_KEY")) {
    $generated = (& $venvPython -m kbot.tools genkey).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $generated) { Fail "Could not generate a MASTER_KEY." }
    Set-EnvValue "MASTER_KEY" $generated
    Write-Host ""
    Write-Host "Generated a MASTER_KEY and wrote it to .env." -ForegroundColor Cyan
    Write-Host "Back that file up. It is the only thing that can decrypt stored" -ForegroundColor Yellow
    Write-Host "Kalshi credentials, and nobody can recover it for you." -ForegroundColor Yellow
}

# --- environment ------------------------------------------------------------

if ($Demo -and $Live) { Fail "Pick one: -Demo or -Live." }

# TRADING_MODE is the variable that selects a host now. KALSHI_DEMO is
# deprecated, and leaving a stale one behind is a startup error once
# TRADING_MODE disagrees with it -- so clear it whenever the mode is set here.
if ($Demo) {
    Set-EnvValue "TRADING_MODE" "demo-live"
    Remove-EnvValue "KALSHI_DEMO"
}
if ($Live) {
    # Deliberately paper, not production. -Live used to mean "the production
    # host", which is where paper reads from anyway. Sending real orders takes
    # TRADING_MODE=production-live plus ALLOW_PRODUCTION_ORDERS, set by hand,
    # and a command-line switch must not be able to do it.
    Set-EnvValue "TRADING_MODE" "paper"
    Remove-EnvValue "KALSHI_DEMO"
}

$modeValue = (Get-EnvValue "TRADING_MODE")
if (-not $modeValue) { $modeValue = "paper" }
$onDemo = $modeValue -eq "demo-live"

# --- preflight --------------------------------------------------------------

if (-not $SkipChecks) {
    Write-Host ""
    # Captured as well as shown, so the failing lines can be repeated at the
    # moment we start. The preflight output scrolls off the top of the window
    # the instant the bot begins logging, and a problem you cannot see is a
    # problem you cannot fix.
    $doctorOutput = & $venvPython -m kbot.tools doctor 2>&1
    $doctor = $LASTEXITCODE
    $doctorOutput | ForEach-Object { Write-Host $_ }
    $doctorProblems = @($doctorOutput | Where-Object { $_ -match "^\s+- " })

    # Exit 2 is a bad .env, which no amount of waiting fixes -- stop.
    if ($doctor -eq 2) {
        Fail "The configuration in .env is invalid -- fix the line named above and re-run."
    }

    # Exit 1 is "something is broken", and on a home connection that is most
    # often the network: a blip, or a boot that got here before Wi-Fi did.
    # Refusing to start would be the wrong call, because the restart loop below
    # is what handles a transient failure. Say so and carry on.
    if ($doctor -ne 0) {
        Write-Host ""
        Write-Host "Preflight found a problem:" -ForegroundColor Yellow
        foreach ($problem in $doctorProblems) {
            Write-Host "  $($problem.ToString().Trim())" -ForegroundColor Red
        }
        Write-Host ""
        Write-Host "Starting anyway -- if that is the network it will sort itself" -ForegroundColor Yellow
        Write-Host "out. If it names your Telegram token, it will not: fix .env and" -ForegroundColor Yellow
        Write-Host "restart. Ctrl+C to stop." -ForegroundColor Yellow
        Start-Sleep -Seconds 5
    }
}

# --- run, and keep running --------------------------------------------------

if ($Recorder) {
    $what = "market recorder"
    $argv = @("-m", "kbot.research", "record", "--interval", "1")
    $where = "Data lands in research\."
} else {
    $what = "bot"
    $argv = @("-m", "kbot")
    $where = "Message your bot /start on Telegram."
}

Write-Host ""
Write-Host "Starting the $what. Leave this window open." -ForegroundColor Green
if ($onDemo) {
    Write-Host "Mode: DEMO-LIVE -- real orders on Kalshi's demo exchange, demo funds." -ForegroundColor Cyan
} elseif ($modeValue -eq "production-live") {
    Write-Host "Mode: PRODUCTION-LIVE -- REAL ORDERS, REAL MONEY." -ForegroundColor Red
} else {
    Write-Host "Mode: PAPER -- nothing is sent to Kalshi." -ForegroundColor Cyan
}
Write-Host "Stop it with Ctrl+C. $where" -ForegroundColor DarkGray
Write-Host ""

# Restart on any exit except a config error (2) -- restarting cannot fix a typo,
# and a loop that hammers Telegram on a bad token gets the token rate-limited.
# The backoff caps at a minute so a network outage doesn't turn into a spin.
$delay = 2
while ($true) {
    $startedAt = Get-Date
    & $venvPython @argv
    $code = $LASTEXITCODE

    if ($Once) { exit $code }

    # A run that lasted is evidence the problem was transient, so start the
    # backoff over. Without this, one bad week leaves every later restart
    # waiting a full minute.
    if (((Get-Date) - $startedAt).TotalMinutes -ge 5) { $delay = 2 }

    if ($code -eq 2) {
        Fail "Configuration error -- fix the line named above and re-run. Not restarting."
    }
    if ($code -eq 0) {
        Write-Host ""
        Write-Host "The $what exited cleanly. Not restarting." -ForegroundColor DarkGray
        exit 0
    }

    Write-Host ""
    Write-Host "The $what exited with code $code. Restarting in ${delay}s (Ctrl+C to stop)." -ForegroundColor Yellow
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, 60)
}
