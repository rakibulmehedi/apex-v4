#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install APEX V4 as a Windows service via NSSM.

.DESCRIPTION
    Configures NSSM to run the APEX V4 trading pipeline as a Windows service
    with auto-restart on failure, graceful shutdown, log rotation, and
    environment variable loading.

    Exit code semantics:
      0  = clean shutdown (market close, manual stop)  -> stays DOWN
      42 = kill switch SOFT/HARD                        -> stays DOWN
      43 = kill switch EMERGENCY                        -> stays DOWN
      *  = crash / error                                -> RESTART after 10s

.PARAMETER ApexHome
    Root directory of the APEX V4 installation. Default: C:\apex_v4

.EXAMPLE
    .\nssm_install.ps1
    .\nssm_install.ps1 -ApexHome "D:\trading\apex_v4"
#>

param(
    [string]$ApexHome = "C:\apex_v4"
)

$ServiceName = "APEX_V4"
$ErrorActionPreference = "Stop"

# ── Pre-flight checks ───────────────────────────────────────────────

Write-Host "=== APEX V4 Service Installer ===" -ForegroundColor Cyan

# Check NSSM is available
$nssm = Get-Command nssm -ErrorAction SilentlyContinue
if (-not $nssm) {
    Write-Host "ERROR: nssm not found in PATH." -ForegroundColor Red
    Write-Host "Download from https://nssm.cc/download and add to PATH."
    exit 1
}
Write-Host "[OK] nssm found: $($nssm.Source)" -ForegroundColor Green

# Check APEX_HOME exists
if (-not (Test-Path $ApexHome)) {
    Write-Host "ERROR: APEX_HOME not found: $ApexHome" -ForegroundColor Red
    exit 1
}
Write-Host "[OK] APEX_HOME: $ApexHome" -ForegroundColor Green

# Check venv exists
$PythonExe = Join-Path $ApexHome "venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    Write-Host "ERROR: Python venv not found: $PythonExe" -ForegroundColor Red
    Write-Host "Run: python -m venv $ApexHome\venv"
    exit 1
}
Write-Host "[OK] Python: $PythonExe" -ForegroundColor Green

# Check secrets.env exists
$SecretsEnv = Join-Path $ApexHome "config\secrets.env"
if (-not (Test-Path $SecretsEnv)) {
    Write-Host "WARNING: secrets.env not found: $SecretsEnv" -ForegroundColor Yellow
    Write-Host "Service will start but MT5/DB connections will fail."
}
else {
    Write-Host "[OK] secrets.env: $SecretsEnv" -ForegroundColor Green
}

# Create logs directory
$LogDir = Join-Path $ApexHome "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}
Write-Host "[OK] Log directory: $LogDir" -ForegroundColor Green

# Remove existing service if present
$status = & nssm status $ServiceName 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "Removing existing $ServiceName service..." -ForegroundColor Yellow
    & nssm stop $ServiceName 2>&1 | Out-Null
    & nssm remove $ServiceName confirm
}

# ── Install service ─────────────────────────────────────────────────

Write-Host "`nInstalling $ServiceName service..." -ForegroundColor Cyan

& nssm install $ServiceName $PythonExe
& nssm set $ServiceName AppDirectory $ApexHome
& nssm set $ServiceName AppParameters "-m src.pipeline"
& nssm set $ServiceName Description "APEX V4 Algorithmic Trading System"
& nssm set $ServiceName DisplayName "APEX V4 Trading Pipeline"

# ── Exit code mapping (restart policy) ──────────────────────────────
# Default: restart on any non-zero exit
# Exit 0:  clean shutdown — stay down
# Exit 42: kill switch SOFT/HARD — stay down
# Exit 43: kill switch EMERGENCY — stay down

& nssm set $ServiceName AppExit Default Restart
& nssm set $ServiceName AppExit 0 Exit
& nssm set $ServiceName AppExit 42 Exit
& nssm set $ServiceName AppExit 43 Exit
& nssm set $ServiceName AppRestartDelay 10000
& nssm set $ServiceName AppThrottle 300000

# ── Graceful shutdown (~30s total) ──────────────────────────────────
# 1. Send Ctrl+C (console), wait 5s
# 2. Send WM_CLOSE (window), wait 5s
# 3. Terminate threads, wait 20s
# Pipeline must handle Ctrl+C -> close positions -> exit(0)

& nssm set $ServiceName AppStopMethodSkip 0
& nssm set $ServiceName AppStopMethodConsole 5000
& nssm set $ServiceName AppStopMethodWindow 5000
& nssm set $ServiceName AppStopMethodThreads 20000

# ── Logging ─────────────────────────────────────────────────────────

& nssm set $ServiceName AppStdout (Join-Path $LogDir "apex_stdout.log")
& nssm set $ServiceName AppStderr (Join-Path $LogDir "apex_stderr.log")
& nssm set $ServiceName AppStdoutCreationDisposition 4
& nssm set $ServiceName AppStderrCreationDisposition 4
& nssm set $ServiceName AppRotateFiles 1
& nssm set $ServiceName AppRotateOnline 1
& nssm set $ServiceName AppRotateBytes 10485760

# ── Environment variables ───────────────────────────────────────────

$EnvVars = @(
    "PYTHONPATH=$ApexHome",
    "PYTHONUNBUFFERED=1",
    "PYTHONDONTWRITEBYTECODE=1",
    "APEX_HOME=$ApexHome"
)

# Load additional vars from secrets.env if it exists
if (Test-Path $SecretsEnv) {
    Get-Content $SecretsEnv | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#")) {
            $EnvVars += $line
        }
    }
}

# Load additional vars from apex_v4.env if it exists
$ApexEnv = Join-Path $ApexHome "ops\apex_v4.env"
if (Test-Path $ApexEnv) {
    Get-Content $ApexEnv | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#")) {
            $EnvVars += $line
        }
    }
}

& nssm set $ServiceName AppEnvironmentExtra $EnvVars

# ── Service startup ─────────────────────────────────────────────────

& nssm set $ServiceName Start SERVICE_AUTO_START

# ── Service dependencies ────────────────────────────────────────────
# Adjust service names to match your PostgreSQL/Redis installation.
# Common names: postgresql-x64-16, Redis, redis-server

& nssm set $ServiceName DependOnService "postgresql-x64-16" "Redis"

# ── Done ────────────────────────────────────────────────────────────

Write-Host "`n=== Installation Complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "Service: $ServiceName"
Write-Host "Python:  $PythonExe"
Write-Host "Home:    $ApexHome"
Write-Host "Logs:    $LogDir"
Write-Host ""
Write-Host "Commands:" -ForegroundColor Cyan
Write-Host "  Start:   nssm start $ServiceName"
Write-Host "  Stop:    nssm stop $ServiceName"
Write-Host "  Status:  nssm status $ServiceName"
Write-Host "  Logs:    Get-Content $LogDir\apex_stdout.log -Tail 50 -Wait"
Write-Host "  Remove:  .\nssm_uninstall.ps1"
Write-Host ""

# Show final status
& nssm status $ServiceName
