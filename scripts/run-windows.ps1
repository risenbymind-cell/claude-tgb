# Run DirectionalBot on a Windows PC, forever.
#
#   powershell -ExecutionPolicy Bypass -File scripts\run-windows.ps1 -Demo
#
# Builds the venv, runs the setup wizard the first time, runs the preflight,
# then supervises the bot: if it crashes or the network drops, it restarts.
# Safe to re-run -- an existing .env is left alone.
#
#   -Demo       point Kalshi at its demo environment (no real money, ever)
#   -Live       point Kalshi back at production
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

if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Host ""
    Write-Host "First run -- let's write the configuration." -ForegroundColor Cyan
    Write-Host "You need two things: your bot token from @BotFather, and your"
    Write-Host "Telegram user ID from @userinfobot. Everything else is generated."
    Write-Host ""
    & $venvPython -m kbot.tools setup
    if ($LASTEXITCODE -ne 0) { Fail "Setup did not complete. Nothing was written." }
}

# --- environment ------------------------------------------------------------

# -Demo and -Live rewrite one line of .env rather than setting a process
# variable, so the choice survives a restart of this script and shows up in
# `doctor` and `/status` -- there is no way to be running against production
# while believing you are on demo.
function Set-EnvValue($name, $value) {
    $path = Join-Path $root ".env"
    $lines = @(Get-Content $path)
    $pattern = "^\s*#?\s*$([regex]::Escape($name))\s*="
    if ($lines -match $pattern) {
        $lines = $lines | ForEach-Object {
            if ($_ -match $pattern) { "$name=$value" } else { $_ }
        }
    } else {
        $lines += "$name=$value"
    }
    Set-Content -Path $path -Value $lines -Encoding UTF8
}

if ($Demo -and $Live) { Fail "Pick one: -Demo or -Live." }
if ($Demo) { Set-EnvValue "KALSHI_DEMO" "true" }
if ($Live) { Set-EnvValue "KALSHI_DEMO" "false" }

$onDemo = (Select-String -Path (Join-Path $root ".env") `
    -Pattern "^\s*KALSHI_DEMO\s*=\s*(true|1|yes|on)\s*$" -Quiet)

# --- preflight --------------------------------------------------------------

if (-not $SkipChecks) {
    Write-Host ""
    & $venvPython -m kbot.tools doctor
    $doctor = $LASTEXITCODE
    if ($doctor -eq 2) {
        Fail "The configuration in .env is invalid -- fix the line named above and re-run."
    }
    if ($doctor -ne 0) {
        Write-Host ""
        Write-Host "Preflight found a problem. Fix it, or re-run with -SkipChecks to start anyway." -ForegroundColor Yellow
        exit 1
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
    Write-Host "Kalshi: DEMO environment -- no real money can move." -ForegroundColor Cyan
} else {
    Write-Host "Kalshi: PRODUCTION environment." -ForegroundColor Yellow
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
