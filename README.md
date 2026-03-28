# APEX V4

Production-grade hybrid regime-based algorithmic Forex trading system built on MetaTrader 5.

**Current status:** Phase 6 complete — paper trading ready. 701 tests passing. Deployment score 97/100.

---

## What Is APEX V4

APEX V4 is a disciplined rebuild of APEX V3, designed to fix the V3 failures not of architecture but of execution discipline: hardcoded values, untested risk math, and missing state reconciliation.

The system trades four Forex pairs across two regime-gated strategies:

- **Momentum** — activated on TRENDING regimes. Multi-timeframe EMA confirmation, ATR-based sizing.
- **Mean Reversion** — activated on RANGING regime. ADF stationarity gate, Kalman smoothing, Ornstein–Uhlenbeck calibration.

Position sizing uses quarter-Kelly criterion, capped at 2% of allocated capital. A sequential 7-gate risk governor evaluates every trade before execution. Three-level kill switch (SOFT/HARD/EMERGENCY) provides circuit-breaker protection.

---

## Architecture

```
 ┌─────────────────────────────────────────────────────────┐
 │                   WINDOWS VPS                           │
 │                                                         │
 │  MT5 Terminal ──► MarketFeed ──► ZMQ PUSH               │
 │  (broker feed)    (M5/M15/H1       tcp://127.0.0.1:5559 │
 │                   candle close)         │               │
 │                                         ▼               │
 │                              ┌─────────────────┐        │
 │                              │  Pipeline Loop  │        │
 │                              │                 │        │
 │  MarketSnapshot               │  FeatureFabric  │        │
 │       │                      │  (TA-Lib)       │        │
 │       ▼                      │       │         │        │
 │  FeatureVector               │  RegimeClassifier        │
 │       │                      │       │         │        │
 │       ▼                      │  Alpha Engines  │        │
 │  Regime                      │  (Momentum /    │        │
 │       │                      │   MeanReversion)│        │
 │       ▼                      │       │         │        │
 │  AlphaHypothesis             │  CalibrationEngine       │
 │       │                      │  (Kelly sizing) │        │
 │       ▼                      │       │         │        │
 │  CalibratedTradeIntent       │  RiskGovernor   │        │
 │       │                      │  (7 gates)      │        │
 │       ▼                      │       │         │        │
 │  RiskDecision                │  ExecutionGateway        │
 │       │                      │  (MT5 orders)   │        │
 │       ▼                      │       │         │        │
 │  FillRecord                  │  LearningLoop   │        │
 │       │                      │  (Kelly update) │        │
 │       ▼                      └─────────────────┘        │
 │  TradeOutcome                                           │
 │                                                         │
 │  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐  │
 │  │PostgreSQL│  │ Memurai  │  │ Prometheus + Grafana  │  │
 │  │ (5432)   │  │ (6379)   │  │ (9090)       (3000)   │  │
 │  └──────────┘  └──────────┘  └──────────────────────┘  │
 └─────────────────────────────────────────────────────────┘
```

**Data flow:** MT5 broker → MarketFeed → ZMQ → Pipeline → FeatureVector → Regime → AlphaHypothesis → CalibratedTradeIntent → RiskDecision → Execution → FillRecord → TradeOutcome → SegmentUpdate (learning loop)

---

## Tech Stack

| Layer | Technology | Notes |
|---|---|---|
| Language | Python 3.11 | Full type hints, PEP 8 |
| Broker API | MetaTrader5 | Windows-only, real mode |
| Indicators | TA-Lib 0.6.8 | C library, Python bindings |
| Schemas | Pydantic v2 | Frozen models, strict validation |
| Transport | ZeroMQ (pyzmq) | PUSH/PULL, tcp://127.0.0.1:5559 |
| Database | PostgreSQL 16+ | Source of truth, 7 tables |
| Cache | Redis / Memurai | Feature vectors, positions, kill state |
| ORM | SQLAlchemy 2.0 | Session factory, connection pool |
| Migrations | Alembic | Version-controlled schema |
| Logging | structlog | JSON output, file rotation |
| Metrics | Prometheus client | Port 8000, 12 alert rules |
| Dashboards | Grafana | 9 panels, auto-provisioned |
| Service | NSSM | Windows service, auto-restart |
| Testing | pytest + pytest-asyncio | 701 tests, all mocked |
| Stats | scipy, statsmodels | ADF test, OU fitting |
| Numerics | numpy, TA-Lib | Covariance, indicators |

---

## Quick Start (Windows VPS)

### Prerequisites

- Windows Server 2019/2022 or Windows 10/11
- Python 3.11 installed and on PATH
- MetaTrader5 terminal installed and logged in
- PostgreSQL 16+ running as Windows service
- Memurai (Redis for Windows) running as Windows service
- NSSM downloaded and on PATH
- TA-Lib C library installed (Windows binary)

### 1. Clone and Install

```powershell
cd C:\
git clone <repo> apex_v4
cd C:\apex_v4
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
pip install MetaTrader5
```

### 2. Configure Secrets

```powershell
copy config\secrets.env.example config\secrets.env
notepad config\secrets.env
```

Required values:
```env
MT5_LOGIN=12345678
MT5_PASSWORD=your_password
MT5_SERVER=YourBroker-Server
POSTGRES_USER=apex
POSTGRES_PASSWORD=strong_password
```

### 3. Configure Settings

Edit `config/settings.yaml`:
- Set `system.mode: paper` (keep this for initial deployment)
- Set `mt5.mode: real` (required on Windows with MT5 terminal)
- Verify pairs, thresholds, and risk parameters

### 4. Run Database Migrations

```powershell
python -m alembic upgrade head
```

### 5. Migrate V3 Data (paper mode requires segment history)

```powershell
python scripts/migrate_v3_data.py
```

### 6. Run Pre-flight Check

```powershell
python -m src.pipeline --preflight-only
```

### 7. Install as Windows Service

```powershell
# Run as Administrator
.\ops\nssm_install.ps1
nssm start APEX_V4
```

### 8. Start Monitoring

```powershell
cd C:\apex_v4
docker-compose up -d
```

Grafana: http://localhost:3000 (admin / set GF_ADMIN_PASSWORD)

---

## Configuration Reference

### config/settings.yaml

```yaml
system:
  mode: paper          # paper | live

mt5:
  mode: stub           # stub | real (real = Windows with MT5 terminal)
  pairs:
    - EURUSD
    - GBPUSD
    - USDJPY
    - AUDUSD
  timeframes: [M5, M15, H1, H4]
  candle_limits:
    M5: 50
    M15: 50
    H1: 200
    H4: 50

redis:
  host: localhost
  port: 6379
  db: 0
  ttl:
    feature_vector: 300      # 5 minutes
    open_positions: 60       # 1 minute
    segment_stats: 3600      # 1 hour
    last_reconcile: 30       # 30 seconds

postgres:
  host: localhost
  port: 5432
  dbname: apex_v4

regime:
  adx_trend_threshold: 31   # ADX > 31 → trending (calibrated P2.8)
  adx_range_threshold: 22   # ADX < 22 → ranging  (calibrated P2.8)

risk:
  capital_allocation_pct: 0.10   # 10% of equity allocated
  max_position_size: 0.02        # 2% hard cap per trade
  kelly_fraction: 0.25           # quarter-Kelly
  min_trade_sample: 30           # minimum segment history before live
  var_hard_limit: 0.05           # 5% portfolio VaR → REJECT
  var_soft_limit: 0.03           # 3% portfolio VaR → SOFT kill switch
  dd_scalar_thresholds:
    low: 0.02                    # < 2% DD → full size
    high: 0.05                   # 2–5% DD → half size; ≥ 5% → no trade
  ewma_lambda: 0.999
  condition_number_warn: 15.0
  condition_number_max: 30.0

spread:
  max_points: 0.00030            # 3 pips max spread

alpha:
  min_rr_ratio: 1.8              # minimum R:R ratio
  adf_pvalue_threshold: 0.05     # ADF stationarity gate
  ou_max_halflife_h1: 48         # max OU half-life in H1 candles
  conviction_threshold: 0.65     # minimum conviction for mean reversion
  zscore_guard: 3.0              # reject signals beyond 3σ

reconciler:
  heartbeat_seconds: 5

zmq:
  address: "tcp://127.0.0.1:5559"

prometheus:
  port: 8000
```

### config/secrets.env

```env
# MT5 credentials
MT5_LOGIN=12345678
MT5_PASSWORD=your_mt5_password
MT5_SERVER=YourBroker-Server

# PostgreSQL — use either full URL or individual parts
APEX_DATABASE_URL=postgresql://apex:password@localhost:5432/apex_v4

# OR individual parts:
POSTGRES_USER=apex
POSTGRES_PASSWORD=your_db_password
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=apex_v4
```

**Never commit `secrets.env` to git.** It is in `.gitignore`.

---

## How to Run

### Paper Mode (recommended start)

```powershell
# Via NSSM service (preferred — auto-restart, logging)
nssm start APEX_V4

# Or direct (for debugging)
.\venv\Scripts\python.exe -m src.pipeline
```

Paper mode simulates execution without real orders. MT5 data is still live.

### Live Mode

**Before switching to live:**

1. Complete paper trading with all go/no-go criteria met (see `docs/PAPER_TRADING.md`)
2. Edit `config/settings.yaml`: set `system.mode: live`
3. Run full pre-flight check — all 9 checks must pass
4. Review `ops/DEPLOYMENT_CHECKLIST.md` — all 50 items checked
5. Run `/audit` and `/risk-verify` — both must pass

```powershell
# Restart service after config change
nssm stop APEX_V4
nssm start APEX_V4
```

---

## Monitoring

### Prometheus Metrics (port 8000)

Key metrics scraped by Prometheus:

| Metric | Description |
|---|---|
| `apex_pipeline_cycle_duration_ms` | End-to-end cycle latency |
| `apex_kill_switch_total` | Kill switch activations by level |
| `apex_gate_rejections_total` | Risk gate rejections by gate + reason |
| `apex_signals_generated_total` | Approved signals by strategy/regime/pair |
| `apex_portfolio_var_pct` | Current portfolio VaR % |
| `apex_current_drawdown_pct` | Current drawdown fraction |
| `apex_covariance_condition` | Covariance matrix condition number |
| `apex_open_positions_count` | Number of open positions |

### Grafana Dashboard (port 3000)

Auto-provisioned from `ops/grafana_dashboard.json`. Contains 9 panels:

1. Pipeline Cycle Duration (ms)
2. Kill Switch State
3. Gate Rejections by Gate
4. Signals Generated by Strategy
5. Portfolio VaR %
6. Current Drawdown %
7. Covariance Condition Number
8. Open Positions Count
9. P&L Curve

### Alert Rules

12 alert rules in `ops/alert_rules.yml`:

- `PipelineDown` — dead-man (no metrics for 5m)
- `KillSwitchSOFT/HARD/EMERGENCY`
- `HighPortfolioVaR` — VaR > 4%
- `HighDrawdown` — DD > 6%
- `HighCycleLatency` — cycle > 1000ms
- `CorrelationCrisis` — condition number > 25
- `StateDriftDetected` — reconciliation mismatch

---

## Kill Switch Operations

The kill switch has three levels. Higher levels are irreversible without manual confirmation.

### Check Current State

```powershell
# Via Redis
redis-cli GET kill_switch

# Via PostgreSQL (full audit trail)
psql -U apex -d apex_v4 -c "SELECT * FROM kill_switch_events ORDER BY timestamp_ms DESC LIMIT 10;"
```

### Manual Reset (after investigating root cause)

```python
import asyncio
from src.risk.kill_switch import KillSwitch

async def reset():
    ks = KillSwitch(redis_client, session_factory)
    await ks.manual_reset("I CONFIRM SYSTEM IS SAFE", operator="your_name")

asyncio.run(reset())
```

The confirmation string must be exactly: `I CONFIRM SYSTEM IS SAFE`

### Kill Switch Behavior

| Level | Trigger | Effect | Recovery |
|---|---|---|---|
| SOFT | VaR > 3%, manual | No new signals | Manual reset |
| HARD | DD > 8%, correlation crisis, manual | Flatten all positions + no new signals | Manual reset |
| EMERGENCY | MT5 disconnect, unhandled exception | Disconnect MT5, dump state to disk, fire alert | Manual reset + restart |

---

## Phase History

APEX V4 was built across 7 phases:

| Phase | Description | Status |
|---|---|---|
| 0 | V3 critical bug fixes (4 bugs: Kelly cap, conviction fallback, phantom fills, hardcoded portfolio) | Complete |
| 1 | Foundation: PostgreSQL schema, Pydantic schemas, MarketFeed, FeatureFabric, Redis state | Complete |
| 2 | Alpha engines: RegimeClassifier, MomentumEngine, MeanReversionEngine, backtest validation | Complete |
| 3 | Risk engine: CalibrationEngine, EWMACovarianceMatrix, RiskGovernor, KillSwitch | Complete |
| 4 | Execution + learning: ExecutionGateway, FillTracker, TradeOutcomeRecorder, KellyInputUpdater | Complete |
| 5 | Observability: structlog, Prometheus metrics, Grafana dashboard, StateReconciler | Complete |
| 6 | Production hardening: pre-flight gate, capital allocation, deployment scripts, Windows service | Complete |

---

## License and Disclaimer

**RISK WARNING:** Algorithmic trading involves substantial risk of financial loss. Past performance is not indicative of future results. This software is provided for educational and research purposes. You are solely responsible for any trading decisions made using this system.

Use of this software in live trading requires:
1. Deep understanding of the codebase and strategy
2. Completion of paper trading validation (minimum 30 trades per segment)
3. Risk capital you can afford to lose
4. Compliance with all applicable laws and broker terms of service

This code is proprietary and confidential.
