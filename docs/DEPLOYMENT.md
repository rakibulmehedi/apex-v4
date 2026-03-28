# APEX V4 — Deployment Guide

> **Target:** Windows VPS with MetaTrader5 terminal
> **Last Updated:** 2026-03-29
> **Companion:** ops/DEPLOYMENT_CHECKLIST.md (50-item gate checklist)

---

## 1. Windows VPS Requirements

### Hardware

| Resource | Minimum | Recommended |
|---|---|---|
| CPU cores | 2 | 4+ |
| RAM | 4 GB | 8 GB |
| Storage | 40 GB | 100 GB SSD |
| Network | 10 Mbps | 100 Mbps, low latency to broker |

### Software

| Component | Version | Notes |
|---|---|---|
| Windows | Server 2019/2022 or Win 10/11 | Server recommended for 24/7 uptime |
| Python | 3.11.x | Must be 3.11 — not 3.12+ |
| PostgreSQL | 16+ | Windows installer from postgresql.org |
| Memurai | Latest | Redis-compatible Windows service |
| MetaTrader5 | Latest from broker | Must be logged in before APEX starts |
| NSSM | 2.24+ | Non-Sucking Service Manager |
| Docker Desktop | Optional | Only needed for Prometheus/Grafana |
| TA-Lib C library | 0.6.x | Windows binary installer required |

---

## 2. Step-by-Step Deployment Guide

### Step 1: Install Python 3.11

```powershell
# Download from python.org/downloads/release/python-3119/
# During install: check "Add Python to PATH"

# Verify
python --version   # must show 3.11.x
```

### Step 2: Install TA-Lib C Library

TA-Lib requires the C library before the Python bindings can be installed:

1. Download the Windows binary from the unofficial binaries repository
2. Run the installer (`ta-lib-0.4.0-msvc.zip` or similar)
3. Add the TA-Lib directory to your PATH if needed

```powershell
# Verify after pip install
python -c "import talib; print(talib.__version__)"
```

### Step 3: Install PostgreSQL 16

1. Download from postgresql.org/download/windows/
2. Run installer — note the password you set for `postgres` superuser
3. Select "PostgreSQL Server" and "pgAdmin 4" components
4. Accept default port 5432
5. Verify the service is running:

```powershell
Get-Service postgresql-x64-16
```

### Step 4: Create the APEX Database and User

```powershell
# Connect as superuser
psql -U postgres
```

```sql
-- Create database
CREATE DATABASE apex_v4;

-- Create limited-privilege user (NOT superuser)
CREATE USER apex WITH PASSWORD 'your_strong_password_here';
GRANT CONNECT ON DATABASE apex_v4 TO apex;
\c apex_v4
GRANT USAGE ON SCHEMA public TO apex;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO apex;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO apex;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO apex;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO apex;
\q
```

### Step 5: Install Memurai (Redis for Windows)

1. Download from memurai.com
2. Run the installer — it installs as a Windows service automatically
3. Verify:

```powershell
Get-Service Memurai
redis-cli PING   # should return PONG
```

### Step 6: Install MetaTrader5

1. Download the MT5 installer from your broker
2. Install and log in to your trading account
3. Leave MT5 open and connected — APEX requires it to be running

### Step 7: Download and Install NSSM

1. Download NSSM from nssm.cc/download
2. Extract `nssm.exe` to `C:\Windows\System32\` (or another PATH directory)
3. Verify:

```powershell
nssm version
```

### Step 8: Clone Repository and Install Dependencies

```powershell
cd C:\
git clone <your-repo-url> apex_v4
cd C:\apex_v4

# Create virtual environment
python -m venv venv
.\venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install MetaTrader5 Python package (Windows-only)
pip install MetaTrader5
```

### Step 9: Configure Secrets

```powershell
# Create secrets file (never commit this to git)
copy NUL config\secrets.env
notepad config\secrets.env
```

Add these values (use your actual credentials):

```env
# MT5 credentials
MT5_LOGIN=12345678
MT5_PASSWORD=your_mt5_password
MT5_SERVER=YourBroker-Server

# PostgreSQL — recommended: full URL
APEX_DATABASE_URL=postgresql://apex:your_strong_password_here@localhost:5432/apex_v4
```

Verify it is in `.gitignore`:

```powershell
git check-ignore -v config\secrets.env   # should output the .gitignore rule
```

### Step 10: Configure Settings

Review and edit `config/settings.yaml`:

```yaml
system:
  mode: paper      # Keep as paper for initial deployment

mt5:
  mode: real       # Must be real on Windows VPS with MT5 terminal
  pairs:
    - EURUSD
    - GBPUSD
    - USDJPY
    - AUDUSD
```

Verify regime thresholds, risk parameters, and pair list match your trading plan.

### Step 11: Run Database Migrations

```powershell
cd C:\apex_v4
.\venv\Scripts\activate
python -m alembic upgrade head
```

Verify tables exist:

```powershell
psql -U apex -d apex_v4 -c "\dt"
```

Expected tables: `market_snapshots`, `candles`, `feature_vectors`, `trade_outcomes`, `kill_switch_events`, `fills`, `reconciliation_log`

### Step 12: Migrate V3 Trade Data

Paper trading requires segment history (minimum 30 trades per segment). Import from V3:

```powershell
python scripts/migrate_v3_data.py
```

Verify segment counts:

```powershell
psql -U apex -d apex_v4 -c "
SELECT strategy, regime, session, COUNT(*) as trade_count
FROM trade_outcomes
GROUP BY strategy, regime, session
ORDER BY strategy, regime, session;"
```

All 24 active segments must have ≥ 30 trades.

### Step 13: Run Pre-flight Check

```powershell
python -m src.pipeline --preflight-only
```

All 9 checks must pass:

```
[1] PostgreSQL connection      ✓
[2] Redis connection           ✓
[3] MT5 connection             ✓
[4] Kill switch state: NONE    ✓
[5] Schema integrity           ✓
[6] VaR computation            ✓
[7] Config validation          ✓
[8] V3 data imported           ✓  (warn in paper mode, block in live)
[9] Segment counts ≥ 30        ✓  (warn in paper mode, block in live)
```

Checks 8 and 9 produce yellow warnings in paper mode (not blocking). They block in live mode.

### Step 14: Install APEX as a Windows Service

```powershell
# Run PowerShell as Administrator
.\ops\nssm_install.ps1
```

The script:
- Installs the `APEX_V4` Windows service via NSSM
- Configures auto-restart on crash (10s delay)
- Sets clean shutdown exit codes (0, 42, 43 → stay down)
- Configures graceful shutdown (30s total: Ctrl+C → WM_CLOSE → terminate)
- Sets up log rotation (10 MB files) to `C:\apex_v4\logs\`
- Loads secrets from `config/secrets.env` as environment variables
- Depends on `postgresql-x64-16` and `Redis` services

```powershell
# Start the service
nssm start APEX_V4

# Verify it started
nssm status APEX_V4   # should show: SERVICE_RUNNING

# Check logs
Get-Content C:\apex_v4\logs\apex_stdout.log -Tail 50 -Wait
```

### Step 15: Install Monitoring (Optional but Recommended)

```powershell
# Install Docker Desktop first, then:
cd C:\apex_v4
docker-compose up -d
```

- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (default login: admin / change this!)

Change Grafana admin password:

```powershell
# Set in docker-compose.yml environment or:
# Grafana UI → Profile → Change Password
```

---

## 3. Environment Variables Reference

All secrets are loaded from `config/secrets.env` by NSSM at service start.

| Variable | Required | Description |
|---|---|---|
| `MT5_LOGIN` | Yes (live) | MetaTrader5 account number |
| `MT5_PASSWORD` | Yes (live) | MetaTrader5 account password |
| `MT5_SERVER` | Yes (live) | Broker server name (e.g. `ICMarkets-Live01`) |
| `APEX_DATABASE_URL` | Yes | Full PostgreSQL URL (preferred) |
| `POSTGRES_USER` | Alt | If not using APEX_DATABASE_URL |
| `POSTGRES_PASSWORD` | Alt | If not using APEX_DATABASE_URL |
| `POSTGRES_HOST` | Alt | Default: `localhost` |
| `POSTGRES_PORT` | Alt | Default: `5432` |
| `POSTGRES_DB` | Alt | Default: `apex_v4` |

NSSM also injects:

| Variable | Value | Description |
|---|---|---|
| `PYTHONPATH` | `C:\apex_v4` | Project root on sys.path |
| `PYTHONUNBUFFERED` | `1` | Real-time log output |
| `PYTHONDONTWRITEBYTECODE` | `1` | No .pyc files |
| `APEX_HOME` | `C:\apex_v4` | Installation root |

---

## 4. Pre-flight Checklist Summary

The full 50-item checklist is in `ops/DEPLOYMENT_CHECKLIST.md`. Key gate items:

**Infrastructure (I-1 to I-12):**
- Windows VPS provisioned, Python 3.11, venv, dependencies
- PostgreSQL 16+, Memurai, MT5 terminal, NSSM

**Security (S-1 to S-9):**
- secrets.env created, not in git
- PostgreSQL user `apex` with limited privileges (not superuser)
- Prometheus and Grafana bound to 127.0.0.1
- Grafana admin password changed
- Windows Firewall configured (external: RDP + MT5 broker only)

**Application (A-1 to A-15):**
- settings.yaml reviewed, trading_mode = paper initially
- Alembic migrations complete, 7 tables present
- V3 data migrated, 24 segments with ≥ 30 trades
- Kill switch state = NONE
- Pre-flight passes all 9 checks
- 701 tests pass (`pytest tests/ -v`)
- `/risk-verify` passes — 5/5 formulas
- `/audit` passes — 100/100

**Monitoring (M-1 to M-8):**
- Prometheus scraping port 8000
- Alert rules loaded (12 rules)
- Grafana dashboard imported (9 panels)
- Dead-man alert tested

**Recovery (R-1 to R-5):**
- Backup script installed and tested
- Daily backup scheduled via Windows Task Scheduler
- Kill switch reset procedure tested

---

## 5. Firewall Configuration

Only these ports should be accessible externally:

| Port | Protocol | Purpose |
|---|---|---|
| 3389 | TCP | RDP (Remote Desktop) |
| Broker ports | TCP | MT5 connection to broker (varies by broker) |

These ports must be bound to localhost only (not exposed externally):

| Port | Service |
|---|---|
| 5432 | PostgreSQL |
| 6379 | Memurai (Redis) |
| 5559 | ZMQ (feed → pipeline) |
| 8000 | Prometheus metrics |
| 9090 | Prometheus UI |
| 3000 | Grafana |

Windows Firewall PowerShell commands:

```powershell
# Allow RDP from specific IP only
New-NetFirewallRule -Name "RDP-Restricted" -DisplayName "RDP Restricted" `
    -Protocol TCP -LocalPort 3389 -RemoteAddress "YOUR.IP.ADDRESS" `
    -Action Allow

# Block RDP from all other IPs
New-NetFirewallRule -Name "RDP-Block-All" -DisplayName "Block RDP All" `
    -Protocol TCP -LocalPort 3389 -Action Block -Priority 200
```

---

## 6. Switching to Live Mode

**Do not switch to live mode until paper trading validation is complete.**

See `docs/PAPER_TRADING.md` for go/no-go criteria.

When ready:

```powershell
# 1. Stop service
nssm stop APEX_V4

# 2. Edit settings
notepad C:\apex_v4\config\settings.yaml
# Change: system.mode: live

# 3. Run pre-flight (all 9 checks must pass, no warnings)
.\venv\Scripts\activate
python -m src.pipeline --preflight-only

# 4. Start service
nssm start APEX_V4

# 5. Monitor closely for first 30 minutes
Get-Content C:\apex_v4\logs\apex_stdout.log -Tail 100 -Wait
```

During the first live session:
- Have MT5 terminal open and visible
- Watch Grafana for any kill switch alerts
- Be ready to manually close positions via MT5 if needed
