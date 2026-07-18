# 8-hour first-minute strategy run on Windows.
# Prevents system sleep while the bot runs; restores dry-run gates on exit.
#
# Usage (from repo root, in PowerShell):
#   powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-8h.ps1
#   powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-8h.ps1 -Hours 8 -BuyUsd 1

param(
    [double]$Hours = 8,
    [double]$BuyUsd = 1
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $ProjectRoot

$EnvFile = Join-Path $ProjectRoot ".env"
$ConfigFile = Join-Path $ProjectRoot "config.yaml"
$Polybot = Join-Path $ProjectRoot ".venv\Scripts\polybot.exe"
$LogDir = Join-Path $ProjectRoot "logs"
$OutLog = Join-Path $LogDir "polybot-8h.out.log"
$ErrLog = Join-Path $LogDir "polybot-8h.err.log"

if (-not (Test-Path $EnvFile)) {
    throw ".env not found at $EnvFile - copy your secrets there first (never commit it)."
}
if (-not (Test-Path $Polybot)) {
    throw "polybot not found. Run: python -m venv .venv; .\.venv\Scripts\pip install -e ."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Keep Windows awake for the duration of this process.
if (-not ("Win32.Sleep" -as [type])) {
    Add-Type -Namespace Win32 -Name Sleep -MemberDefinition @"
[DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
"@
}
$ES_CONTINUOUS = [Convert]::ToUInt32("80000000", 16)
$ES_SYSTEM_REQUIRED = [Convert]::ToUInt32("00000001", 16)
$ES_AWAYMODE_REQUIRED = [Convert]::ToUInt32("00000040", 16)
[Win32.Sleep]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED -bor $ES_AWAYMODE_REQUIRED) | Out-Null

function Set-EnvTrading([bool]$Enabled) {
    $raw = Get-Content $EnvFile -Raw
    $value = if ($Enabled) { "true" } else { "false" }
    if ($raw -match "(?m)^POLYBOT_ENABLE_TRADING=") {
        $raw = [regex]::Replace($raw, "(?m)^POLYBOT_ENABLE_TRADING=.*$", "POLYBOT_ENABLE_TRADING=$value")
    } else {
        $raw = $raw.TrimEnd() + "`r`nPOLYBOT_ENABLE_TRADING=$value`r`n"
    }
    Set-Content -Path $EnvFile -Value $raw -NoNewline
}

function Set-ConfigDryRun([bool]$DryRun) {
    $raw = Get-Content $ConfigFile -Raw
    $value = if ($DryRun) { "true" } else { "false" }
    $raw = [regex]::Replace($raw, "(?m)^dry_run:\s*(true|false)\s*$", "dry_run: $value")
    if ($BuyUsd -gt 0) {
        $raw = [regex]::Replace($raw, "(?m)^(\s*buy_usd:\s*)([0-9.]+)\s*$", ('${1}' + $BuyUsd))
    }
    Set-Content -Path $ConfigFile -Value $raw -NoNewline
}

function Restore-Gates {
    try {
        Set-ConfigDryRun $true
        Set-EnvTrading $false
        Write-Host "Restored gates: dry_run=true POLYBOT_ENABLE_TRADING=false"
    } catch {
        Write-Warning "Failed to restore gates: $_"
    }
    try {
        [Win32.Sleep]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
    } catch {}
}

Write-Host "Project: $ProjectRoot"
Write-Host "Duration: $Hours hour(s) | buy_usd=$BuyUsd | sleep-inhibit=ON"
Write-Host "Logs: $OutLog / $ErrLog"

Set-ConfigDryRun $false
Set-EnvTrading $true

$seconds = [int]([math]::Round($Hours * 3600))
$env:POLYBOT_ENABLE_TRADING = "true"

try {
    $proc = Start-Process -FilePath $Polybot `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog `
        -PassThru `
        -WindowStyle Hidden

    Write-Host "polybot pid=$($proc.Id) - running for $seconds seconds"
    Write-Host "Tail logs with: Get-Content $OutLog -Wait"

    if (-not $proc.WaitForExit($seconds * 1000)) {
        Write-Host "Time limit reached; stopping polybot..."
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        try { $proc.WaitForExit(15000) | Out-Null } catch {}
    } else {
        Write-Host "polybot exited early with code $($proc.ExitCode)"
    }
} finally {
    Restore-Gates
}

Write-Host "Done. Review logs under $LogDir"
