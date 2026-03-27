#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Backup APEX V4 PostgreSQL database.

.DESCRIPTION
    Creates a timestamped pg_dump backup of the apex_v4 database.
    Retains the last 30 backups and deletes older ones.

    Schedule via Windows Task Scheduler for daily execution.

.PARAMETER BackupDir
    Directory for backup files. Default: C:\apex_v4\backups

.PARAMETER PgDumpPath
    Path to pg_dump.exe. Default: auto-detected from PATH.

.EXAMPLE
    .\backup_db.ps1
    .\backup_db.ps1 -BackupDir "D:\backups\apex_v4"
#>

param(
    [string]$BackupDir = "C:\apex_v4\backups",
    [string]$PgDumpPath = "",
    [int]$RetainDays = 30
)

$ErrorActionPreference = "Stop"

# ── Find pg_dump ───────────────────────────────────────────────────
if (-not $PgDumpPath) {
    $pg = Get-Command pg_dump -ErrorAction SilentlyContinue
    if (-not $pg) {
        Write-Host "ERROR: pg_dump not found in PATH." -ForegroundColor Red
        Write-Host "Set -PgDumpPath or add PostgreSQL bin to PATH."
        exit 1
    }
    $PgDumpPath = $pg.Source
}

# ── Load secrets.env for credentials ──────────────────────────────
$ApexHome = $env:APEX_HOME
if (-not $ApexHome) { $ApexHome = "C:\apex_v4" }
$SecretsEnv = Join-Path $ApexHome "config\secrets.env"

if (Test-Path $SecretsEnv) {
    Get-Content $SecretsEnv | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $key, $val = $line -split "=", 2
            [Environment]::SetEnvironmentVariable($key.Trim(), $val.Trim(), "Process")
        }
    }
}

# ── Build connection params ───────────────────────────────────────
$DbHost = if ($env:POSTGRES_HOST) { $env:POSTGRES_HOST } else { "localhost" }
$DbPort = if ($env:POSTGRES_PORT) { $env:POSTGRES_PORT } else { "5432" }
$DbName = if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { "apex_v4" }
$DbUser = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "apex" }

# pg_dump uses PGPASSWORD env var for authentication
if ($env:POSTGRES_PASSWORD) {
    $env:PGPASSWORD = $env:POSTGRES_PASSWORD
}

# ── Create backup directory ───────────────────────────────────────
if (-not (Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
}

# ── Run pg_dump ───────────────────────────────────────────────────
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupFile = Join-Path $BackupDir "apex_v4_${Timestamp}.sql.gz"
$TempFile = Join-Path $BackupDir "apex_v4_${Timestamp}.sql"

Write-Host "=== APEX V4 Database Backup ===" -ForegroundColor Cyan
Write-Host "  Host:   $DbHost:$DbPort"
Write-Host "  DB:     $DbName"
Write-Host "  User:   $DbUser"
Write-Host "  Output: $BackupFile"
Write-Host ""

try {
    & $PgDumpPath -h $DbHost -p $DbPort -U $DbUser -d $DbName -F c -f $TempFile
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: pg_dump failed with exit code $LASTEXITCODE" -ForegroundColor Red
        exit 1
    }
    # Rename to final name (pg_dump -F c produces custom format, not SQL)
    $BackupFile = Join-Path $BackupDir "apex_v4_${Timestamp}.dump"
    Move-Item $TempFile $BackupFile -Force
    $Size = (Get-Item $BackupFile).Length / 1MB
    Write-Host "[OK] Backup complete: $BackupFile ($([math]::Round($Size, 2)) MB)" -ForegroundColor Green
}
catch {
    Write-Host "ERROR: Backup failed: $_" -ForegroundColor Red
    exit 1
}

# ── Cleanup old backups ───────────────────────────────────────────
$Cutoff = (Get-Date).AddDays(-$RetainDays)
$Old = Get-ChildItem $BackupDir -Filter "apex_v4_*" | Where-Object { $_.LastWriteTime -lt $Cutoff }
if ($Old) {
    Write-Host "Removing $($Old.Count) backup(s) older than $RetainDays days..." -ForegroundColor Yellow
    $Old | Remove-Item -Force
}

Write-Host "=== Backup Complete ===" -ForegroundColor Green
