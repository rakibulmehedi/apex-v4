#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Remove the APEX V4 Windows service.

.DESCRIPTION
    Gracefully stops and removes the APEX_V4 service installed by nssm_install.ps1.
    Does not delete application files or logs.

.EXAMPLE
    .\nssm_uninstall.ps1
#>

$ServiceName = "APEX_V4"
$ErrorActionPreference = "Stop"

Write-Host "=== APEX V4 Service Uninstaller ===" -ForegroundColor Cyan

# Check NSSM is available
$nssm = Get-Command nssm -ErrorAction SilentlyContinue
if (-not $nssm) {
    Write-Host "ERROR: nssm not found in PATH." -ForegroundColor Red
    exit 1
}

# Check service exists
$status = & nssm status $ServiceName 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Service $ServiceName is not installed." -ForegroundColor Yellow
    exit 0
}

# Stop if running
if ($status -match "SERVICE_RUNNING") {
    Write-Host "Stopping $ServiceName (graceful shutdown, up to 30s)..." -ForegroundColor Yellow
    & nssm stop $ServiceName
    Start-Sleep -Seconds 2
}

# Remove service
Write-Host "Removing $ServiceName service..." -ForegroundColor Yellow
& nssm remove $ServiceName confirm

Write-Host "`n[OK] Service removed. Application files and logs are preserved." -ForegroundColor Green
