# APEX V4 — Operations Guide

> **Audience:** System operators and on-call engineers
> **Companion:** ops/RUNBOOK.md (quick reference), ops/INCIDENT_RESPONSE.md (P0-P3 playbook)
> **Last Updated:** 2026-03-29

---

## 1. Daily Monitoring Routine

### Morning Checks (before market open)

```powershell
# 1. Service status
nssm status APEX_V4                    # must be SERVICE_RUNNING

# 2. Kill switch state
redis-cli GET kill_switch              # must return (nil) — no active kill switch

# 3. Recent logs (last 100 lines)
Get-Content C:\apex_v4\logs\apex_stdout.log -Tail 100

# 4. Database connectivity
psql -U apex -d apex_v4 -c "SELECT COUNT(*) FROM trade_outcomes WHERE created_at > NOW() - INTERVAL '24 hours';"

# 5. Reconciliation — no mismatches in last 24h
psql -U apex -d apex_v4 -c "
SELECT COUNT(*) as total,
       SUM(CASE WHEN mismatch_detected THEN 1 ELSE 0 END) as mismatches
FROM reconciliation_log
WHERE created_at > NOW() - INTERVAL '24 hours';"
```

### Grafana Dashboard Checks

Open http://localhost:3000 and verify:

| Panel | Expected Range |
|---|---|
| Pipeline Cycle Duration | < 500ms (alert fires at > 1000ms) |
| Kill Switch State | NONE |
| Portfolio VaR % | < 3% (soft limit), < 5% (hard limit) |
| Current Drawdown | < 5% (reduce threshold), < 8% (hard stop) |
| Covariance Condition Number | < 15 (normal), < 30 (crisis threshold) |
| Open Positions | Expected based on regime conditions |

### End-of-Day Checks

```powershell
# 1. Trade count for the day
psql -U apex -d apex_v4 -c "
SELECT strategy, regime, COUNT(*) as trades,
       AVG(r_multiple) as avg_r,
       SUM(CASE WHEN won THEN 1 ELSE 0 END)::float / COUNT(*) as win_rate
FROM trade_outcomes
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY strategy, regime
ORDER BY strategy, regime;"

# 2. Kill switch events
psql -U apex -d apex_v4 -c "
SELECT timestamp_ms, level, previous_state, new_state, reason
FROM kill_switch_events
WHERE created_at > NOW() - INTERVAL '24 hours'
ORDER BY timestamp_ms DESC;"

# 3. Backup database
C:\apex_v4\ops\backup_db.ps1
```

---

## 2. Reading Logs

Logs are in `C:\apex_v4\logs\` and written in structured JSON format via structlog.

### Log Files

| File | Content |
|---|---|
| `apex_stdout.log` | Main pipeline log (INFO and above) |
| `apex_stderr.log` | Error output and uncaught exceptions |

NSSM rotates logs at 10 MB.

### Log Format

```json
{
  "timestamp": "2026-03-29T10:15:32.451Z",
  "level": "info",
  "event": "governor_APPROVE",
  "pair": "EURUSD",
  "final_size": 0.008750,
  "risk_state": "NORMAL"
}
```

### Key Log Events

| Event | Level | Meaning |
|---|---|---|
| `preflight_complete` | info | Pre-flight passed, starting pipeline |
| `MarketFeed started` | info | Feed connected and polling |
| `snapshot published` | info | Snapshot sent over ZMQ |
| `regime_classified` | info | Regime determined for a pair |
| `calibration_complete` | info | Position sized, Kelly params logged |
| `calibration_rejected` | warning | No signal — edge ≤ 0, no segment data, or DD ≥ 5% |
| `gate_N_REJECT` | info | Risk gate N rejected the trade |
| `governor_APPROVE` | info | Trade approved, routed to execution |
| `governor_REDUCE` | info | Trade size reduced by risk gates |
| `kill_switch_SOFT` | warning | SOFT kill switch activated |
| `kill_switch_HARD` | critical | HARD kill switch activated, flattening positions |
| `kill_switch_EMERGENCY` | critical | EMERGENCY — MT5 disconnected, state dumped |
| `reconciler_mismatch` | critical | State drift detected, HARD triggered |
| `kill_switch_RESET` | warning | Kill switch manually reset by operator |

### Searching Logs

```powershell
# Find all REJECT events
Select-String -Path C:\apex_v4\logs\apex_stdout.log -Pattern "REJECT"

# Find all kill switch events
Select-String -Path C:\apex_v4\logs\apex_stdout.log -Pattern "kill_switch"

# Find events for a specific pair
Select-String -Path C:\apex_v4\logs\apex_stdout.log -Pattern '"pair": "EURUSD"'

# Show last 50 lines live (like tail -f)
Get-Content C:\apex_v4\logs\apex_stdout.log -Tail 50 -Wait
```

---

## 3. Service Management

```powershell
# Start
nssm start APEX_V4

# Stop (graceful — 30 second timeout)
nssm stop APEX_V4

# Restart
nssm restart APEX_V4

# Status
nssm status APEX_V4

# View service configuration
nssm dump APEX_V4

# Edit service configuration
nssm edit APEX_V4
```

---

## 4. Kill Switch Operations

### Check Current State

```powershell
# Fast check via Redis
redis-cli GET kill_switch

# Full audit trail via PostgreSQL
psql -U apex -d apex_v4 -c "
SELECT id, timestamp_ms, level, previous_state, new_state, reason, created_at
FROM kill_switch_events
ORDER BY timestamp_ms DESC
LIMIT 20;"
```

### Understanding the State

| Redis Value | Meaning | Trading |
|---|---|---|
| (nil) | NONE — no kill switch | Normal |
| SOFT | No new signals | Existing positions managed |
| HARD | Positions flattened | No trading |
| EMERGENCY | MT5 disconnected | No trading, state dump on disk |

### Manual Trigger (emergency)

```python
# Connect to the running pipeline via Python:
import asyncio, redis, yaml
from db.models import make_session_factory, make_engine
from src.risk.kill_switch import KillSwitch

async def trigger():
    cfg = yaml.safe_load(open("config/settings.yaml"))
    r = redis.Redis(host="localhost", port=6379, db=0)
    sf = make_session_factory(make_engine())
    ks = KillSwitch(r, sf)
    await ks.recover_from_db()
    await ks.trigger("HARD", "manual trigger by operator")

asyncio.run(trigger())
```

### Kill Switch Reset Procedure

**Do not reset until you have investigated and resolved the root cause.**

1. Identify why the kill switch triggered:
   ```powershell
   psql -U apex -d apex_v4 -c "
   SELECT reason, new_state, created_at
   FROM kill_switch_events
   ORDER BY timestamp_ms DESC LIMIT 5;"
   ```

2. Check open positions in MT5 terminal — verify no unintended positions

3. If EMERGENCY: read the state dump:
   ```powershell
   Get-ChildItem C:\apex_v4\data\emergency\ | Sort-Object LastWriteTime -Descending | Select-Object -First 1 | Get-Content
   ```

4. Fix the root cause (MT5 connection, excessive drawdown, etc.)

5. Reset:
   ```python
   async def reset():
       # same setup as above...
       await ks.recover_from_db()
       await ks.manual_reset("I CONFIRM SYSTEM IS SAFE", operator="your_name")
   asyncio.run(reset())
   ```

6. Restart service:
   ```powershell
   nssm start APEX_V4
   ```

7. Monitor logs for 30 minutes before leaving unattended

---

## 5. Grafana Dashboard Guide

Dashboard: http://localhost:3000 (auto-provisioned from `ops/grafana_dashboard.json`)

### Panel Reference

| Panel | Metric | Normal | Alert Threshold |
|---|---|---|---|
| Pipeline Cycle Duration | `apex_pipeline_cycle_duration_ms` | < 200ms | 1000ms |
| Kill Switch State | `apex_kill_switch_total` | 0 increments | Any increment |
| Gate Rejections | `apex_gate_rejections_total` | Sporadic | Sustained high rate |
| Signals Generated | `apex_signals_generated_total` | Regime-dependent | Flat (no signals in trending market) |
| Portfolio VaR % | `apex_portfolio_var_pct` | < 2% | 3% (SOFT), 5% (HARD) |
| Current Drawdown | `apex_current_drawdown_pct` | < 3% | 5% (REDUCE), 8% (HARD) |
| Covariance Condition | `apex_covariance_condition` | < 10 | 15 (shrinkage), 25 (alert), 30 (crisis) |
| Open Positions | `apex_open_positions_count` | 0–4 | > 4 (unexpected) |

### Alert Rules

12 alert rules in `ops/alert_rules.yml`. Configured in Prometheus.

| Alert | Condition | Severity |
|---|---|---|
| PipelineDown | No metrics for 5 minutes (dead-man) | Critical |
| KillSwitchSOFT | SOFT kill triggered | Warning |
| KillSwitchHARD | HARD kill triggered | Critical |
| KillSwitchEMERGENCY | EMERGENCY kill triggered | Critical |
| HighPortfolioVaR | VaR > 4% | Warning |
| CriticalPortfolioVaR | VaR > 4.5% | Critical |
| HighDrawdown | Drawdown > 6% | Warning |
| CriticalDrawdown | Drawdown > 7% | Critical |
| HighCycleLatency | Cycle > 1000ms for 2+ minutes | Warning |
| CorrelationCrisis | Condition number > 25 | Warning |
| StateDriftDetected | Reconciliation mismatch | Critical |
| GateRejectionSpike | Sustained high rejection rate | Info |

---

## 6. Backup Procedures

### Manual Backup

```powershell
C:\apex_v4\ops\backup_db.ps1
```

Default backup location: `C:\apex_v4\backups\`
Filename format: `apex_v4_backup_YYYY-MM-DD_HHMMSS.sql`

### Automated Daily Backup

Set up via Windows Task Scheduler:

```powershell
# Create scheduled task (run as Administrator)
$action = New-ScheduledTaskAction -Execute "PowerShell.exe" `
    -Argument "-NonInteractive -File C:\apex_v4\ops\backup_db.ps1"
$trigger = New-ScheduledTaskTrigger -Daily -At "01:00AM"
$settings = New-ScheduledTaskSettingsSet -RunOnlyIfNetworkAvailable
Register-ScheduledTask -TaskName "APEX_V4_Backup" `
    -Action $action -Trigger $trigger -Settings $settings `
    -RunLevel Highest
```

### Restore from Backup

```powershell
# Stop APEX first
nssm stop APEX_V4

# Restore
psql -U apex -d apex_v4 -f C:\apex_v4\backups\apex_v4_backup_2026-03-29_010000.sql

# Restart
nssm start APEX_V4
```

---

## 7. Common Errors and Fixes

### "RuntimeError: Proactor event loop does not implement add_reader"

**Cause:** Python 3.10+ on Windows uses ProactorEventLoop by default. pyzmq requires SelectorEventLoop.

**Fix:** This is already handled in `src/pipeline.py`. If you see this error it means you're running the pipeline directly without going through `src/pipeline.py`. Always use `python -m src.pipeline`.

### "connection refused" to PostgreSQL

**Cause:** PostgreSQL service not running, wrong credentials, or DATABASE_URL not set.

**Fix:**
```powershell
# Check service
Get-Service postgresql-x64-16

# Start if needed
Start-Service postgresql-x64-16

# Verify environment variable
echo $env:APEX_DATABASE_URL
```

### "NoneType is not callable" on startup

**Cause:** `init_context()` was called with `None` dependencies (DI bug from L2 lesson).

**Fix:** Ensure `config/secrets.env` is loaded and PostgreSQL + Redis are reachable before starting APEX.

### Kill switch in HARD state on startup

**Cause:** Previous run triggered HARD kill switch (drawdown, correlation crisis, or reconciliation mismatch). State persists in PostgreSQL.

**Fix:** See Section 4 of this document — investigate root cause, then perform manual reset.

### "no segment data" warnings in calibration

**Cause:** A segment (strategy × regime × session combination) has fewer than 30 historical trade outcomes.

**Fix:** Continue paper trading to accumulate outcomes. This warning is expected during early paper trading phases.

### Grafana shows "No data"

**Cause:** Prometheus not scraping metrics. Either APEX is not running or the metrics port (8000) is not reachable.

**Fix:**
```powershell
# Check APEX is running and metrics are exposed
Invoke-WebRequest -Uri http://localhost:8000 -UseBasicParsing | Select-Object Content

# Check Prometheus is running
docker-compose ps
```

---

## 8. Configuration Changes

When changing `config/settings.yaml`:

1. Stop APEX: `nssm stop APEX_V4`
2. Edit the file
3. Validate changes make sense (check `tasks/lessons.md` for relevant rules)
4. Run pre-flight: `python -m src.pipeline --preflight-only`
5. Start APEX: `nssm start APEX_V4`
6. Monitor logs for 5 minutes

**Never change risk thresholds during a trading session.** Make configuration changes outside market hours.
