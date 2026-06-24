# install_claude_code.ps1
# -----------------------------------------------------------------------------
# One-time setup script: installs Claude Code on this Windows VPS so it can
# read/monitor/debug the XAUUSD MT5 EA running in this folder.
#
# Run from PowerShell (no Administrator needed):
#   cd "C:\Users\Administrator\Desktop\RoBotTrading man 0 USV9"
#   powershell -ExecutionPolicy Bypass -File install_claude_code.ps1
#
# What it does:
#   1. Checks for Node.js 18+ (required by the npm install method).
#   2. Installs Claude Code globally via npm.
#   3. Verifies the "claude" command is on PATH.
#   4. Prints the next manual steps (login + first launch).
# It does NOT touch your bot's code, config, or running EA process.
# -----------------------------------------------------------------------------

$ErrorActionPreference = "Stop"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Write-Ok($msg) {
    Write-Host "    [OK] $msg" -ForegroundColor Green
}

function Write-WarnMsg($msg) {
    Write-Host "    [!] $msg" -ForegroundColor Yellow
}

# ---- 1. Check Node.js ------------------------------------------------------
Write-Step "Checking for Node.js 18+..."

$nodeOk = $false
try {
    $nodeVersionRaw = (node --version) 2>$null
    if ($nodeVersionRaw) {
        $major = [int]($nodeVersionRaw.TrimStart("v").Split(".")[0])
        if ($major -ge 18) {
            Write-Ok "Found Node.js $nodeVersionRaw"
            $nodeOk = $true
        } else {
            Write-WarnMsg "Found Node.js $nodeVersionRaw, but version 18+ is required."
        }
    }
} catch {
    # node not found - handled below
}

if (-not $nodeOk) {
    Write-WarnMsg "Node.js 18+ was not found on this machine."
    Write-Host ""
    Write-Host "    Install it first, then re-run this script:" -ForegroundColor Yellow
    Write-Host "      1. Download the LTS installer from https://nodejs.org/" -ForegroundColor Yellow
    Write-Host "      2. Run the installer (default options are fine)" -ForegroundColor Yellow
    Write-Host "      3. Close and reopen PowerShell, then re-run this script" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

# ---- 2. Install Claude Code -------------------------------------------------
Write-Step "Installing Claude Code globally via npm (this can take a minute)..."

npm install -g @anthropic-ai/claude-code
if ($LASTEXITCODE -ne 0) {
    Write-WarnMsg "npm install failed. See the error above."
    Write-Host "    Common fix: do NOT run this script as Administrator / with sudo." -ForegroundColor Yellow
    Write-Host "    If you see a permissions error, see: https://code.claude.com/docs/en/setup" -ForegroundColor Yellow
    exit 1
}
Write-Ok "Claude Code installed."

# ---- 3. Verify the command is available ------------------------------------
Write-Step "Verifying the claude command..."

try {
    $claudeVersion = (claude --version) 2>$null
    if ($claudeVersion) {
        Write-Ok "claude is on PATH: $claudeVersion"
    } else {
        Write-WarnMsg "Installed, but claude --version returned nothing. Try closing and reopening PowerShell."
    }
} catch {
    Write-WarnMsg "claude command not found on PATH yet. Close and reopen PowerShell, then try: claude --version"
}

# ---- 4. Next steps -----------------------------------------------------------
Write-Step "Setup script finished. Next steps (manual, one time):"
Write-Host ""
Write-Host "  1. cd into the bot folder. Example (adjust the path to match this VPS):" -ForegroundColor White
$botFolderHint = "C:\Users\Administrator\Desktop\RoBotTrading man 0 USV9"
Write-Host "       cd " -NoNewline -ForegroundColor White
Write-Host $botFolderHint -ForegroundColor Gray
Write-Host ""
Write-Host "  2. Launch Claude Code:" -ForegroundColor White
Write-Host "       claude" -ForegroundColor White
Write-Host ""
Write-Host "  3. First run opens a browser login prompt - log in with your" -ForegroundColor White
Write-Host "     Claude account (same one you use at claude.ai)." -ForegroundColor White
Write-Host ""
Write-Host "  4. Once inside, try asking it:" -ForegroundColor White
Write-Host "       read the latest 50 lines of xauusd_mt5_strategy.log and tell me if anything looks wrong" -ForegroundColor White
Write-Host ""
Write-Host "  See claude_code_vps_setup.md in this folder for the full checklist." -ForegroundColor White
Write-Host ""
