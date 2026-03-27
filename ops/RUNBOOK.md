# APEX V4 — Operator Runbook

> **Audience:** System operators and on-call engineers
> **Last Updated:** 2026-03-28
> **Classification:** INTERNAL

---

## 1. System Architecture

```
 INTERNET
     |
     v
 MT5 BROKER (tick stream)
     |
     v
+--------------------------------------------+
|           WINDOWS VPS                       |
|                                             |
|  +----------+   +---------+   +---------+   |
|  | PostgreSQL|   | Memurai |   |  MT5    |   |
|  | (port 5432)   | (Redis) |   | Terminal|   |
|  |          |   | (6379)  |   |         |   |
|  +----+-----+   +----+----+   +----+----+   |
|       |              |              |        |
|       +--------------+--------------+        |
|                      |                       |
|              +-------+--------+              |
|              | APEX V4 NSSM   |              |
|              | (pipeline.py)  |              |
|              |  - Feed        |              |
|              |  - Regime      |              |
|              |  - Alpha       |              |
|              |  - Risk        |              |
|              |  - Execution   |              |
|              |  - Learning    |              |
|              |  - Reconciler  |              |
|              +-------+--------+              |
|                      |                       |
|              +-------+--------+              |
|              | Prometheus:8000 |              |
|              | (metrics scrape)|              |
|              +-------+--------+              |
|                      |                       |
|         +------------+------------+          |
|         |                         |          |
|  +------+------+   +------+------+          |
|  | Prometheus  |   | Grafana     |          |
|  | (9090)      |   | (3000)      |          |
|  +-------------+   +-------------+          |
+--------------------------------------------+
```

**Startup dependency order:**
```
1. PostgreSQL  (must be running before APEX)
2. Memurai     (must be running before APEX)
3. MT5 Terminal (must be logged in and connected)
4. APEX V4     (NSSM service — auto-starts after dependencies)
5. Prometheus  (optional — monitoring)
6. Grafana     (optional — dashboards)
```

---

## 2. Service Management

### Start APEX V4
```powershell
nssm start APEX_V4
```

### Stop APEX V4 (graceful — closes positions, flushes state)
```powershell
nssm stop APEX_V4
```

### Check Status
```powershell
nssm status APEX_V4
```

### View Live Logs
```powershell
Get-Content C:\apex_v4\logs\apex_stdout.log -Tail 50 -Wait
```

### Reinstall Service (after config changes)
```powershell
cd C:\apex_v4\ops
.\nssm_install.ps1
```

---

## 3. Health Check Procedures

### 3.1 Quick Health Check (30 seconds)

```powershell
# 1. Service running?
nssm status APEX_V4

# 2. Metrics endpoint responding?
Invoke-WebRequest -Uri http://localhost:8000/metrics -UseBasicParsing | Select-Object StatusCode

# 3. PostgreSQL reachable?
psql -U apex -d apex_v4 -c "SELECT 1;"

# 4. Memurai responding?
redis-cli PING

# 5. MT5 terminal logged in?
# Check MT5 terminal GUI — "Connection" status in bottom-left
```

### 3.2 Deep Health Check (5 minutes)

```powershell
# 1. Kill switch state
psql -U apex -d apex_v4 -c "SELECT new_state, reason, timestamp_ms FROM kill_switch_events ORDER BY timestamp_ms DESC LIMIT 5;"

# 2. Recent state drift events
psql -U apex -d apex_v4 -c "SELECT COUNT(*) FROM reconciliation_log WHERE mismatch_detected = true;"

# 3. Recent trade outcomes
psql -U apex -d apex_v4 -c "SELECT strategy, regime, session, won, r_multiple, closed_at FROM trade_outcomes ORDER BY closed_at DESC LIMIT 10;"

# 4. Check Prometheus metrics
curl http://localhost:8000/metrics | Select-String "apex_kill_switch_total"
curl http://localhost:8000/metrics | Select-String "apex_state_drift_total"
curl http://localhost:8000/metrics | Select-String "apex_current_drawdown_pct"

# 5. Check log for errors
Select-String -Path C:\apex_v4\logs\apex_stdout.log -Pattern "CRITICAL|ERROR" | Select-Object -Last 20
```

---

## 4. Kill Switch Procedures

### 4.1 Understanding Kill Switch Levels

| Level | Trigger | Effect | Auto-Reset |
|-------|---------|--------|------------|
| SOFT | VaR > 3%, manual | No new signals, existing positions stay | NO |
| HARD | State drift, drawdown > 8%, condition crisis | Flatten ALL positions | NO |
| EMERGENCY | MT5 disconnect, reconciler failure, unhandled exception | Disconnect MT5, dump state to disk | NO |

### 4.2 Checking Current Kill Switch State

```sql
SELECT new_state, reason, timestamp_ms
FROM kill_switch_events
ORDER BY timestamp_ms DESC
LIMIT 1;
```

```powershell
redis-cli GET kill_switch
```

### 4.3 Resetting SOFT Kill Switch

**Prerequisites:**
- Identify and resolve the root cause
- VaR is now below 3%
- No outstanding state drift events

```python
# Run from project root
python -c "
import asyncio
from db.models import make_session_factory
from src.risk.kill_switch import KillSwitch
import redis

r = redis.Redis(decode_responses=True)
sf = make_session_factory()
ks = KillSwitch(redis_client=r, session_factory=sf)

async def reset():
    await ks.recover_from_db()
    await ks.manual_reset('I CONFIRM SYSTEM IS SAFE', operator='<YOUR_NAME>')
    print('Kill switch reset to NONE')

asyncio.run(reset())
"
```

Then restart the service:
```powershell
nssm start APEX_V4
```

### 4.4 Resetting HARD Kill Switch

**Prerequisites:**
- All positions are flat (verify with MT5 terminal)
- Root cause identified (state drift, drawdown, correlation crisis)
- Redis reconciled with broker state

Follow the same reset procedure as SOFT (Section 4.3).

### 4.5 Resetting EMERGENCY Kill Switch

**Prerequisites:**
- Read the emergency state dump: `C:\apex_v4\data\emergency\emergency_*.json`
- Verify MT5 terminal is connected and logged in
- Verify all positions are in expected state
- Root cause identified and resolved

**Procedure:**
1. Review emergency dump file for the full state at time of EMERGENCY
2. Manually verify all positions in MT5 terminal
3. Follow the reset procedure in Section 4.3
4. Start the service: `nssm start APEX_V4`
5. Monitor logs for 15 minutes after restart

---

## 5. Common Failure Modes

### 5.1 "PostgreSQL unreachable"

**Symptoms:** Preflight check 2 fails; pipeline exits.
**Fix:**
```powershell
# Check PostgreSQL service
Get-Service postgresql*
# Start if stopped
Start-Service postgresql-x64-16
# Verify
psql -U apex -d apex_v4 -c "SELECT 1;"
# Restart APEX
nssm restart APEX_V4
```

### 5.2 "Redis unreachable"

**Symptoms:** Preflight check 1 fails; pipeline exits.
**Fix:**
```powershell
# Check Memurai service
Get-Service Memurai
# Start if stopped
Start-Service Memurai
# Verify
redis-cli PING
# Restart APEX
nssm restart APEX_V4
```

### 5.3 "MT5 account_info returned None"

**Symptoms:** Preflight check 3 fails; pipeline exits.
**Fix:**
1. Open MT5 terminal
2. Verify you are logged in (check bottom-left status bar)
3. If disconnected: File > Login to Trade Account
4. Wait for connection confirmation
5. Restart APEX: `nssm restart APEX_V4`

### 5.4 "State drift detected"

**Symptoms:** HARD kill switch triggered; positions flattened.
**Root causes:**
- Network glitch between MT5 and pipeline
- Fill ACK lost during order execution
- Redis key expired while position was open

**Fix:**
1. Review reconciliation_log: `SELECT * FROM reconciliation_log WHERE mismatch_detected = true ORDER BY timestamp_ms DESC LIMIT 5;`
2. Verify all positions in MT5 terminal are flat
3. Reset kill switch (Section 4.4)
4. Restart service

### 5.5 "Pipeline stopped emitting metrics"

**Symptoms:** PipelineDown alert fires in Prometheus.
**Fix:**
```powershell
# Check service status
nssm status APEX_V4
# Check exit code
Get-EventLog -LogName Application -Source APEX_V4 -Newest 5
# Check logs
Get-Content C:\apex_v4\logs\apex_stderr.log -Tail 50
# Restart
nssm restart APEX_V4
```

### 5.6 "NSSM keeps restarting the service"

**Symptoms:** Service restarts every 10 seconds (crash loop).
**Fix:**
1. Stop the service: `nssm stop APEX_V4`
2. Check stderr log: `Get-Content C:\apex_v4\logs\apex_stderr.log -Tail 100`
3. Fix the root cause (usually a config or dependency issue)
4. Start the service: `nssm start APEX_V4`

---

## 6. Database Maintenance

### Backup
```powershell
C:\apex_v4\ops\backup_db.ps1
```
Backups are stored in `C:\apex_v4\backups\` with 30-day retention.

### Restore from Backup
```powershell
pg_restore -h localhost -U apex -d apex_v4 -c C:\apex_v4\backups\apex_v4_YYYYMMDD_HHMMSS.dump
```

### Run Migrations
```powershell
cd C:\apex_v4
venv\Scripts\python -m alembic upgrade head
```

---

## 7. Configuration Files

| File | Purpose | Secrets? |
|------|---------|----------|
| `config/settings.yaml` | Runtime parameters (mode, thresholds, pairs) | No |
| `config/secrets.env` | MT5 credentials, DB password | **YES** |
| `ops/apex_v4.env` | Service environment variables | No |
| `ops/prometheus.yml` | Prometheus scrape config | No |
| `ops/alert_rules.yml` | Prometheus alerting rules | No |

### Changing Trading Mode
Edit `config/settings.yaml`:
```yaml
system:
  mode: live    # paper | live
```
Then restart: `nssm restart APEX_V4`

### Changing Capital Allocation
Edit `config/settings.yaml`:
```yaml
risk:
  capital_allocation_pct: 0.10   # 10% of equity
```
Then restart (operator confirmation required on startup).

---

## 8. Emergency Contacts

| Role | Name | Contact |
|------|------|---------|
| System Operator | ___________ | ___________ |
| Trading Lead | ___________ | ___________ |
| Infrastructure | ___________ | ___________ |

---

*This runbook is version-controlled. Update after every incident or config change.*
