# APEX V4 — Pre-Deployment Gate Checklist

> **Purpose:** This checklist must be 100% complete before APEX V4 goes live.
> **Approver:** ___________________  **Date:** ___________________
> **Deployment Target:** Windows VPS (MT5 + NSSM)

---

## Infrastructure

- [ ] **I-1** Windows VPS provisioned with adequate resources (4+ CPU cores, 8+ GB RAM, SSD)
- [ ] **I-2** Python 3.11 installed and on PATH
- [ ] **I-3** Virtual environment created: `python -m venv C:\apex_v4\venv`
- [ ] **I-4** All dependencies installed: `pip install -r requirements.txt`
- [ ] **I-5** MetaTrader5 Python package installed: `pip install MetaTrader5`
- [ ] **I-6** TA-Lib C library installed (Windows binary from unofficial-binaries)
- [ ] **I-7** PostgreSQL 16+ installed and running as Windows service
- [ ] **I-8** Memurai (Redis for Windows) installed and running as Windows service
- [ ] **I-9** MT5 terminal installed, logged in, and connected to broker
- [ ] **I-10** NSSM downloaded and on PATH
- [ ] **I-11** Service dependencies configured: PostgreSQL -> Memurai -> APEX_V4
- [ ] **I-12** `C:\apex_v4` directory structure matches repository layout

## Security

- [ ] **S-1** `config/secrets.env` created with MT5_LOGIN, MT5_PASSWORD, MT5_SERVER
- [ ] **S-2** `config/secrets.env` has POSTGRES_USER and POSTGRES_PASSWORD (or APEX_DATABASE_URL)
- [ ] **S-3** PostgreSQL user `apex` created with limited privileges (NOT superuser)
  ```sql
  CREATE USER apex WITH PASSWORD '<password>';
  GRANT CONNECT ON DATABASE apex_v4 TO apex;
  GRANT USAGE ON SCHEMA public TO apex;
  GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO apex;
  GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO apex;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO apex;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO apex;
  ```
- [ ] **S-4** `config/secrets.env` is NOT in git (verified in `.gitignore`)
- [ ] **S-5** Windows Firewall configured: only RDP (3389) and MT5 broker ports open externally
- [ ] **S-6** Prometheus (9090) and Grafana (3000) bound to 127.0.0.1 (not 0.0.0.0)
- [ ] **S-7** Grafana admin password changed from default (set GF_ADMIN_PASSWORD in environment)
- [ ] **S-8** RDP access restricted to known IP addresses
- [ ] **S-9** Windows auto-updates configured for security patches (restart during market close)

## Application

- [ ] **A-1** `config/settings.yaml` reviewed — correct pairs, thresholds, mode
- [ ] **A-2** Trading mode set to `paper` for initial deployment
- [ ] **A-3** `capital_allocation_pct` set to `0.10` (10%) for initial live deployment
- [ ] **A-4** Database migrations run: `python -m alembic upgrade head`
- [ ] **A-5** All 7 required tables exist in PostgreSQL
- [ ] **A-6** V3 data migration complete: `python scripts/migrate_v3_data.py`
- [ ] **A-7** All 24 active segments have >= 30 trade outcomes (ADR-002)
- [ ] **A-8** Kill switch state is NONE (no active kill switch)
- [ ] **A-9** Zero unresolved state drift events in reconciliation_log
- [ ] **A-10** Pre-flight validation passes all 9 checks
- [ ] **A-11** NSSM service installed: `ops\nssm_install.ps1`
- [ ] **A-12** Service starts and stops cleanly (test both)
- [ ] **A-13** Full test suite passes: `pytest tests/ -v` (700+ tests)
- [ ] **A-14** `/risk-verify` passes — all formulas match Section 7 exactly
- [ ] **A-15** `/audit` passes — architecture compliance confirmed

## Monitoring

- [ ] **M-1** Prometheus running and scraping APEX metrics endpoint (:8000)
- [ ] **M-2** Alert rules loaded (`ops/alert_rules.yml`)
- [ ] **M-3** Grafana dashboard imported (`ops/grafana_dashboard.json`)
- [ ] **M-4** PipelineDown dead-man alert configured and tested
- [ ] **M-5** KillSwitch alerts configured (SOFT/HARD/EMERGENCY)
- [ ] **M-6** Grafana data source points to Prometheus
- [ ] **M-7** Dashboard displays real-time metrics (cycle duration, drawdown, VaR)
- [ ] **M-8** Alert notification channel configured (email, Slack, or webhook)

## Recovery

- [ ] **R-1** Database backup script installed: `ops/backup_db.ps1`
- [ ] **R-2** Daily backup scheduled via Windows Task Scheduler
- [ ] **R-3** Backup restore tested successfully
- [ ] **R-4** Kill switch reset procedure tested (Section 4.3-4.5 of RUNBOOK.md)
- [ ] **R-5** RUNBOOK.md reviewed and understood by operator
- [ ] **R-6** INCIDENT_RESPONSE.md reviewed and understood by operator
- [ ] **R-7** Emergency contacts filled in (RUNBOOK.md Section 8)
- [ ] **R-8** Emergency state dump directory exists: `data/emergency/`

## Paper Trading Validation (before switching to live)

- [ ] **P-1** 7+ days continuous paper trading completed
- [ ] **P-2** Zero crashes during paper trading period
- [ ] **P-3** Zero state drift events during paper trading
- [ ] **P-4** Win rate >= 50% over 50+ paper trades
- [ ] **P-5** Average R-multiple >= 1.5 on winning trades
- [ ] **P-6** Maximum drawdown < 10% during paper period
- [ ] **P-7** Signal latency p95 < 500ms
- [ ] **P-8** Kill switch triggers < 3 SOFT, 0 HARD during paper period

## Go-Live Transition

- [ ] **G-1** Change `system.mode` from `paper` to `live` in settings.yaml
- [ ] **G-2** Verify `capital_allocation_pct` is at intended level (start at 10%)
- [ ] **G-3** Start service and confirm operator prompt ("CONFIRMED 0.1")
- [ ] **G-4** Monitor first 4 hours of live trading continuously
- [ ] **G-5** Verify first live trade executes and records correctly
- [ ] **G-6** Verify fill slippage is within expected range (< 2 points)
- [ ] **G-7** V3 system kept on standby for 30 days post-migration

---

## Sign-Off

| Role | Name | Signature | Date |
|------|------|-----------|------|
| System Operator | | | |
| Trading Lead | | | |
| Risk Manager | | | |

**Deployment Readiness Score:** _____ / 100
**Decision:** [ ] APPROVED  [ ] BLOCKED — reason: ___________________

---

*This checklist is version-controlled. Every item must be checked before production deployment.*
