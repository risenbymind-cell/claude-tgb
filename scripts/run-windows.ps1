# Run DirectionalBot on a Windows PC.
#
#   powershell -ExecutionPolicy Bypass -File scripts\run-windows.ps1
#
# Builds the venv, runs the setup wizard the first time, runs the preflight,
# then starts the bot. Safe to re-run — an existing .env is left alone.
#
# Add -Recorder to start the market recorder instead of the bot. Run both, in
# two windows, if you want data collecting while the bot trades.

param(
    [switch]$Recorder,
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Fail($message) {
    Write-Host ""
    Write-Host $message -ForegroundColor Red
    exit 1
}

# --- Python -----------------------------------------------------------------

$python = $null
foreach ($candidate in @("py -3", "python", "python3")) {
    $exe, $exeArgs = $candidate.Split(" ", 2)
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        $version = & $exe $exeArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$version -ge [version]"3.10") {
            $python = $candidate
            break
        }
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

Write-Host "Python: $python" -ForegroundColor DarkGray

# --- venv -------------------------------------------------------------------

$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating the virtual environment..." -ForegroundColor Cyan
    $exe, $exeArgs = $python.Split(" ", 2)
    & $exe $exeArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create .venv" }
}

Write-Host "Installing dependencies..." -ForegroundColor Cyan
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "Dependency install failed" }

# --- config -----------------------------------------------------------------

if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Host ""
    Write-Host "First run — let's write the configuration." -ForegroundColor Cyan
    Write-Host "You need two things: your bot token from @BotFather, and your"
    Write-Host "Telegram user ID from @userinfobot. Everything else is generated."
    Write-Host ""
    & $venvPython -m kbot.tools setup
    if ($LASTEXITCODE -ne 0) { Fail "Setup did not complete. Nothing was written." }
}

# --- preflight --------------------------------------------------------------

if (-not $SkipChecks) {
    Write-Host ""
    & $venvPython -m kbot.tools doctor
    $doctor = $LASTEXITCODE
    if ($doctor -eq 2) {
        Fail "The configuration in .env is invalid — fix the line named above and re-run."
    }
    if ($doctor -ne 0) {
        Write-Host ""
        Write-Host "Preflight found a problem. Fix it, or re-run with -SkipChecks to start anyway." -ForegroundColor Yellow
        exit 1
    }
}

# --- run --------------------------------------------------------------------

Write-Host ""
if ($Recorder) {
    Write-Host "Starting the market recorder. Leave this window open." -ForegroundColor Green
    Write-Host "Stop it with Ctrl+C. Data lands in research\." -ForegroundColor DarkGray
    Write-Host ""
    & $venvPython -m kbot.research record --interval 1
} else {
    Write-Host "Starting the bot. Leave this window open." -ForegroundColor Green
    Write-Host "Stop it with Ctrl+C. Message your bot /start on Telegram." -ForegroundColor DarkGray
    Write-Host ""
    & $venvPython -m kbot
}
