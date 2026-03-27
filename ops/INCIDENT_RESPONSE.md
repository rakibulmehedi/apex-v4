# APEX V4 — Incident Response Playbook

> **Audience:** System operators and on-call engineers
> **Last Updated:** 2026-03-28
> **Companion:** ops/RUNBOOK.md (operational procedures)

---

## 1. Severity Levels

| Level | Description | Response Time | Examples |
|-------|-------------|---------------|----------|
| **P0** | System down, positions at risk, money actively being lost | **Immediate** (< 5 min) | EMERGENCY kill switch, MT5 disconnect, uncontrolled position |
| **P1** | Trading halted, no active risk but system non-functional | **< 30 min** | HARD kill switch, PostgreSQL down, pipeline crash loop |
| **P2** | Degraded performance, trading continues with reduced capacity | **< 4 hours** | SOFT kill switch, high latency, high slippage |
| **P3** | Minor issue, no trading impact | **Next business day** | Grafana dashboard error, log rotation issue, metric gap |

---

## 2. P0 — Critical: System Down / Positions at Risk

### 2.1 EMERGENCY Kill Switch Triggered

**Alert:** `KillSwitchEMERGENCY` in Prometheus/Grafana
**Impact:** MT5 disconnected, all trading halted, state dumped to disk

**Immediate Actions (< 5 minutes):**
1. **DO NOT PANIC.** The system has protected itself by disconnecting.
2. Read the emergency dump:
   ```powershell
   Get-ChildItem C:\apex_v4\data\emergency\ | Sort-Object LastWriteTime -Descending | Select-Object -First 1 | Get-Content
   ```
3. Open MT5 terminal — verify positions manually:
   - Are there open positions? If yes, decide: hold or close manually.
   - Check account equity — any unexpected losses?
4. Check the reason in the dump file:
   - `broker_disconnect` → MT5 lost connection to broker
   - `reconciler_failure` → internal error in state reconciliation
   - `unhandled exception in pipeline` → code bug

**Resolution:**
1. Fix the root cause (MT5 connection, bug fix, etc.)
2. Reset kill switch (see RUNBOOK.md Section 4.5)
3. Restart service: `nssm start APEX_V4`
4. Monitor for 30 minutes

### 2.2 Uncontrolled Open Position

**Scenario:** Pipeline crashes with open positions, no kill switch triggered
**Impact:** Positions running without SL/TP management

**Immediate Actions:**
1. Open MT5 terminal immediately
2. Verify all open positions have SL and TP set
3. If any position lacks SL/TP: set them manually NOW
4. Decision: close all positions or let them run with SL/TP
5. Check pipeline logs for crash reason
6. Restart pipeline after fixing root cause

### 2.3 MT5 Broker Disconnect Mid-Trade

**Scenario:** MT5 loses connection while an order is in flight
**Impact:** Order may or may not have been filled; state is uncertain

**Immediate Actions:**
1. Open MT5 terminal — check Trade tab for the position
2. If position exists: the order was filled — verify SL/TP are set
3. If position does not exist: the order was rejected — no action needed
4. The reconciler should have detected the disconnect within 5 seconds
5. EMERGENCY kill switch should have triggered automatically
6. Follow Section 2.1 for EMERGENCY recovery

---

## 3. P1 — High: Trading Halted

### 3.1 HARD Kill Switch Triggered

**Alert:** `KillSwitchHARD` in Prometheus/Grafana
**Impact:** All positions flattened, no new trades

**Diagnosis:**
```sql
SELECT new_state, reason, timestamp_ms
FROM kill_switch_events
ORDER BY timestamp_ms DESC LIMIT 5;
```

**Common causes and fixes:**
| Cause | Fix |
|-------|-----|
| `state_drift` | Check reconciliation_log, verify Redis/MT5 sync |
| `max_drawdown` | Review trade history, ensure drawdown < 8% |
| `correlation_crisis` | Covariance matrix degenerate — unusual market conditions |

**Resolution:** See RUNBOOK.md Section 4.4

### 3.2 PostgreSQL Down

**Alert:** Pipeline exits with preflight check 2 failure
**Impact:** Pipeline cannot start; no trading

**Immediate Actions:**
```powershell
Get-Service postgresql*
Start-Service postgresql-x64-16
# Verify
psql -U apex -d apex_v4 -c "SELECT 1;"
nssm restart APEX_V4
```

### 3.3 Pipeline Crash Loop

**Alert:** `PipelineDown` dead-man alert
**Impact:** Service keeps restarting (NSSM auto-restart)

**Diagnosis:**
```powershell
nssm stop APEX_V4
Get-Content C:\apex_v4\logs\apex_stderr.log -Tail 100
```

**Common causes:**
- Import error (missing dependency)
- Configuration error (bad YAML)
- MT5 terminal not running
- Database migration needed

---

## 4. P2 — Medium: Degraded Performance

### 4.1 SOFT Kill Switch

**Alert:** `KillSwitchSOFT` in Prometheus
**Impact:** No new trades, existing positions maintained

**Diagnosis:** Usually VaR > 3% soft limit
```sql
SELECT new_state, reason FROM kill_switch_events ORDER BY timestamp_ms DESC LIMIT 1;
```

**Resolution:** Wait for VaR to decrease (positions close), then reset.

### 4.2 High Signal Latency

**Alert:** `HighSignalLatency` (p95 > 500ms)
**Impact:** Stale approvals may be rejected

**Diagnosis:** Check system resources (CPU, memory, disk I/O)
```powershell
Get-Process python | Select-Object CPU, WorkingSet64
```

### 4.3 High Slippage

**Alert:** `HighSlippage` (p95 > 2 points)
**Impact:** Execution quality degraded

**Diagnosis:** Check broker conditions, spread, market volatility
```sql
SELECT pair, AVG(slippage_points), MAX(slippage_points)
FROM fills
WHERE filled_at > NOW() - INTERVAL '1 hour'
GROUP BY pair;
```

---

## 5. P3 — Low: Minor Issues

### 5.1 Grafana Dashboard Not Loading
- Check Grafana service: `docker-compose -f docker-compose.yml ps`
- Restart: `docker-compose -f docker-compose.yml restart grafana`

### 5.2 Prometheus Not Scraping
- Check Prometheus targets: http://localhost:9090/targets
- Verify APEX metrics endpoint: http://localhost:8000/metrics
- Check `ops/prometheus.yml` configuration

### 5.3 Log File Growing Large
- NSSM auto-rotates at 10 MB
- Manual rotation: `nssm rotate APEX_V4`
- Application logs also rotate at 10 MB (5 backups)

---

## 6. Escalation Path

| Escalation Level | Who | When |
|-------------------|-----|------|
| L1 — Operator | On-call operator | P0-P3: first response |
| L2 — Trading Lead | Trading decision maker | P0-P1: position decisions |
| L3 — Engineering | System developer | P0-P1: code bug or infrastructure |
| L4 — Management | Portfolio manager | P0: significant financial impact |

---

## 7. Post-Incident Review Template

Complete this after every P0 or P1 incident:

```markdown
## Incident Report: [DATE] — [TITLE]

### Timeline
- HH:MM — Alert fired / issue detected
- HH:MM — First responder engaged
- HH:MM — Root cause identified
- HH:MM — Fix applied
- HH:MM — Service restored
- HH:MM — Monitoring confirmed stable

### Root Cause
[Description of what went wrong and why]

### Impact
- Duration: X minutes
- Trades affected: N
- Financial impact: $X / X pips
- Kill switch level triggered: SOFT / HARD / EMERGENCY

### Resolution
[What was done to fix it]

### Prevention
[What changes will prevent recurrence]
- [ ] Code fix committed: [commit hash]
- [ ] Lesson added to tasks/lessons.md
- [ ] Runbook updated
- [ ] Alert rule added/modified

### Review Participants
- [Name, Role]
```

---

*This playbook is version-controlled. Update after every incident.*
