# APEX V4 — System Architecture

> **Reference:** APEX_V4_STRATEGY.md Section 5
> **Last Updated:** 2026-03-29

---

## 1. Overview

APEX V4 is a single-process, event-driven trading pipeline that runs on a Windows VPS. It polls MetaTrader 5 for candle closes, computes features, classifies market regime, generates trade hypotheses, sizes and risk-checks them, executes via MT5, and feeds results back into the calibration system.

The pipeline is a single Python 3.11 asyncio process managed by NSSM as a Windows service.

---

## 2. System Diagram

```
 INTERNET
     │
     ▼
 MT5 BROKER (live tick stream)
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│                     WINDOWS VPS                             │
│                                                             │
│  ┌──────────────┐     ┌─────────┐     ┌───────────────┐    │
│  │ PostgreSQL   │     │ Memurai │     │ MT5 Terminal  │    │
│  │ (5432)       │     │ (6379)  │     │               │    │
│  └──────┬───────┘     └────┬────┘     └───────┬───────┘    │
│         │                  │                  │            │
│         └──────────────────┴──────────────────┘            │
│                            │                               │
│                   ┌────────▼────────┐                      │
│                   │  MarketFeed     │ ◄── ZMQ PUSH          │
│                   │  (candle poller)│     :5559             │
│                   └────────┬────────┘                      │
│                            │ ZMQ PULL                      │
│                   ┌────────▼────────┐                      │
│                   │                 │                      │
│                   │  PIPELINE LOOP  │                      │
│                   │  (src/pipeline) │                      │
│                   │                 │                      │
│  ┌────────────────┼─────────────────┼────────────────┐    │
│  │ FeatureFabric  │ RegimeClassifier│ Alpha Engines   │    │
│  │ (TA-Lib)       │ (ADX rules)     │ (Momentum / MR) │    │
│  └────────────────┼─────────────────┼────────────────┘    │
│                   │                 │                      │
│  ┌────────────────┼─────────────────┼────────────────┐    │
│  │ CalibrationEng │ RiskGovernor    │ ExecutionGateway│    │
│  │ (Kelly sizing) │ (7 gates)       │ (MT5 orders)    │    │
│  └────────────────┼─────────────────┼────────────────┘    │
│                   │                 │                      │
│  ┌────────────────┼─────────────────┼────────────────┐    │
│  │ LearningLoop   │ KillSwitch      │ StateReconciler │    │
│  │ (Kelly update) │ (SOFT/HARD/EMG) │ (5s heartbeat)  │    │
│  └────────────────┼─────────────────┼────────────────┘    │
│                   │                                        │
│              ┌────▼───────┐                               │
│              │ Prometheus  │  :8000 ─────► Grafana :3000   │
│              │ (metrics)   │                               │
│              └─────────────┘                              │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Component Diagram

```
src/
├── pipeline.py              — Main loop, PipelineContext, init_context()
├── market/
│   ├── feed.py              — MarketFeed (ZMQ publisher)
│   ├── mt5_client.py        — MT5Client interface (ABC)
│   ├── mt5_real.py          — RealMT5Client (Windows only)
│   ├── mt5_stub.py          — StubMT5Client (test/paper)
│   ├── mt5_factory.py       — get_mt5_client() factory
│   ├── mt5_types.py         — TIMEFRAME_MAP, RateBar type
│   ├── schemas.py           — Pydantic v2 data contracts
│   └── validator.py         — Snapshot validation helpers
├── features/
│   ├── fabric.py            — FeatureFabric (TA-Lib indicators)
│   └── state.py             — RedisStateManager, PostgresWriter
├── regime/
│   └── classifier.py        — RegimeClassifier (ADX rules)
├── alpha/
│   ├── momentum.py          — MomentumEngine
│   ├── mean_reversion.py    — MeanReversionEngine
│   ├── kalman.py            — Kalman filter smoother
│   └── ou_calibration.py   — Ornstein–Uhlenbeck MLE + conviction
├── calibration/
│   ├── engine.py            — CalibrationEngine (Kelly sizing)
│   └── history.py           — PerformanceDatabase (segment stats)
├── risk/
│   ├── governor.py          — RiskGovernor (7-gate)
│   ├── kill_switch.py       — KillSwitch (SOFT/HARD/EMERGENCY)
│   ├── covariance.py        — EWMACovarianceMatrix
│   └── reconciler.py        — StateReconciler (heartbeat)
├── execution/
│   ├── gateway.py           — ExecutionGateway (MT5 orders)
│   └── fill_tracker.py      — FillTracker (confirmation)
├── learning/
│   ├── recorder.py          — TradeOutcomeRecorder
│   └── updater.py           — KellyInputUpdater
├── observability/
│   ├── logging.py           — structlog configuration
│   └── metrics.py           — Prometheus counters/gauges
├── reporting/
│   └── performance.py       — Performance metrics
└── backtest/
    ├── bt_feed.py           — Backtrader data feed
    ├── data_gen.py          — Synthetic data generation
    └── phase2_backtest.py   — Phase 2 validation backtest

db/
├── models.py                — SQLAlchemy ORM (7 tables)
└── __init__.py

scripts/
├── migrate_v3_data.py       — V3 → V4 trade outcome migration
├── paper_sim.py             — Paper trading simulation runner
└── backtrader_backtest.py   — Full backtest runner
```

---

## 4. Data Flow

Every trading cycle follows this exact pipeline:

```
1. MarketFeed polls MT5 every 5 seconds per pair
   └─ Detects M5/M15/H1 candle close
   └─ Fetches M5(50), M15(50), H1(200), H4(50) candles
   └─ Builds and validates MarketSnapshot
   └─ Publishes JSON over ZMQ PUSH to tcp://127.0.0.1:5559

2. Pipeline pulls from ZMQ (1-second poll timeout)
   └─ Deserializes MarketSnapshot

3. FeatureFabric computes FeatureVector
   └─ ATR-14 (H1), ADX-14 (H1), EMA-200 (H1)
   └─ Bollinger Bands (H1), spread_ok, news_blackout
   └─ Caches in Redis (TTL 300s)
   └─ Persists to feature_vectors table

4. RegimeClassifier classifies regime
   └─ TRENDING_UP / TRENDING_DOWN / RANGING / UNDEFINED
   └─ Uses ADX-14 vs thresholds (31/22) and close vs EMA-200

5. Alpha engine generates AlphaHypothesis (or None)
   └─ TRENDING → MomentumEngine
   └─ RANGING → MeanReversionEngine
   └─ UNDEFINED → skip

6. CalibrationEngine sizes the trade
   └─ Looks up segment stats (strategy × regime × session)
   └─ Applies Kelly formula → quarter-Kelly → 2% cap
   └─ Returns CalibratedTradeIntent | None

7. RiskGovernor evaluates 7 gates
   └─ Returns RiskDecision (APPROVE | REJECT | REDUCE)

8. ExecutionGateway sends order to MT5
   └─ Paper mode: simulate fill
   └─ Live mode: MT5 order_send()
   └─ Records Fill only on TRADE_RETCODE_DONE

9. TradeOutcomeRecorder records outcome on close
   └─ Writes to trade_outcomes table
   └─ KellyInputUpdater refreshes segment stats in PostgreSQL

10. StateReconciler (parallel, 5s heartbeat)
    └─ Compares Redis positions vs MT5 broker positions
    └─ Any mismatch → HARD kill switch
```

---

## 5. ZMQ Wire Format

**Transport:** ZeroMQ PUSH/PULL pattern
**Address:** `tcp://127.0.0.1:5559`
**Format:** JSON string (Pydantic `model_dump_json()`)

MarketSnapshot JSON structure:
```json
{
  "type": "MarketSnapshot",
  "pair": "EURUSD",
  "timestamp": 1711699200000,
  "candles": {
    "M5":  [{"open": 1.0841, "high": 1.0845, "low": 1.0839, "close": 1.0843, "volume": 1234.0}, ...],
    "M15": [...],
    "H1":  [...],
    "H4":  [...]
  },
  "spread_points": 0.00012,
  "session": "LONDON"
}
```

---

## 6. Database Schema

7 PostgreSQL tables. PostgreSQL is the authoritative source of truth. Redis is a derived cache that is always re-populated from PostgreSQL on restart.

### market_snapshots
```
id              BIGINT PK
pair            VARCHAR(6)      — e.g. "EURUSD"
timestamp_ms    BIGINT          — Unix ms UTC
candles         JSONB           — Timeframe-keyed OHLCV arrays
spread_points   FLOAT
session         ENUM            — LONDON|NY|ASIA|OVERLAP
is_stale        BOOLEAN
created_at      TIMESTAMPTZ
```
*Index: (pair, timestamp_ms)*

### candles
```
id              BIGINT PK
pair            VARCHAR(6)
timeframe       ENUM            — M5|M15|H1|H4
timestamp_ms    BIGINT          — Bar open time
open, high, low, close, volume  FLOAT
created_at      TIMESTAMPTZ
```
*Index: (pair, timeframe, timestamp_ms) UNIQUE*

### feature_vectors
```
id              BIGINT PK
pair            VARCHAR(6)
timestamp_ms    BIGINT
atr_14          FLOAT
adx_14          FLOAT
ema_200         FLOAT
bb_upper, bb_lower, bb_mid  FLOAT
session         ENUM
spread_ok       BOOLEAN
news_blackout   BOOLEAN
created_at      TIMESTAMPTZ
```
*Index: (pair, timestamp_ms)*

### trade_outcomes
```
id              BIGINT PK
pair            VARCHAR(6)
strategy        ENUM            — MOMENTUM|MEAN_REVERSION
regime          ENUM            — TRENDING_UP|TRENDING_DOWN|RANGING|UNDEFINED
session         ENUM
direction       ENUM            — LONG|SHORT
entry_price     FLOAT
exit_price      FLOAT
r_multiple      FLOAT           — actual_return / risk
won             BOOLEAN
fill_id         BIGINT NULLABLE — FK to fills.id
opened_at       TIMESTAMPTZ
closed_at       TIMESTAMPTZ
created_at      TIMESTAMPTZ
```
*Index: (strategy, regime, session) — segment lookup key*
*Index: (pair, closed_at)*

### kill_switch_events
```
id              BIGINT PK
timestamp_ms    BIGINT
level           ENUM            — SOFT|HARD|EMERGENCY
previous_state  VARCHAR(20)
new_state       VARCHAR(20)
reason          TEXT
broker_state_mismatch  BOOLEAN
created_at      TIMESTAMPTZ
```
*Index: (timestamp_ms)*

### fills
```
id              BIGINT PK
order_id        BIGINT UNIQUE   — MT5 order ticket
pair            VARCHAR(6)
direction       ENUM
strategy        ENUM
regime          ENUM
requested_size  FLOAT
actual_size     FLOAT
requested_price FLOAT
actual_fill_price  FLOAT
slippage_points FLOAT
filled_at       TIMESTAMPTZ
created_at      TIMESTAMPTZ
```
*Index: (order_id) UNIQUE, (pair, filled_at)*

### reconciliation_log
```
id                  BIGINT PK
timestamp_ms        BIGINT
redis_positions     JSONB       — Snapshot of Redis open_positions
mt5_positions       JSONB       — Snapshot of MT5 broker positions
mismatch_detected   BOOLEAN
positions_diverged  JSONB NULLABLE
action_taken        VARCHAR(20) — SOFT|HARD or NULL
created_at          TIMESTAMPTZ
```
*Index: (timestamp_ms), (mismatch_detected)*

---

## 7. Module Responsibilities

| Module | Responsibility | Key Class |
|---|---|---|
| `market/feed.py` | Poll MT5, detect candle closes, publish snapshots over ZMQ | `MarketFeed` |
| `market/schemas.py` | All Pydantic v2 data contracts | Various |
| `features/fabric.py` | Compute TA-Lib indicators from MarketSnapshot | `FeatureFabric` |
| `features/state.py` | Cache FeatureVectors in Redis, write to PostgreSQL | `RedisStateManager`, `PostgresWriter` |
| `regime/classifier.py` | Classify market regime from ADX and EMA rules | `RegimeClassifier` |
| `alpha/momentum.py` | Generate momentum hypotheses on trending regimes | `MomentumEngine` |
| `alpha/mean_reversion.py` | Generate mean reversion hypotheses on ranging regime | `MeanReversionEngine` |
| `calibration/engine.py` | Compute edge and size from Kelly criterion | `CalibrationEngine` |
| `calibration/history.py` | Look up historical segment statistics | `PerformanceDatabase` |
| `risk/governor.py` | 7-gate sequential risk check | `RiskGovernor` |
| `risk/kill_switch.py` | Three-level circuit breaker | `KillSwitch` |
| `risk/covariance.py` | EWMA covariance matrix, VaR calculation | `EWMACovarianceMatrix` |
| `risk/reconciler.py` | Compare Redis positions to MT5 broker, trigger HARD on mismatch | `StateReconciler` |
| `execution/gateway.py` | Send orders to MT5, simulate in paper mode | `ExecutionGateway` |
| `execution/fill_tracker.py` | Track fill confirmation, persist to fills table | `FillTracker` |
| `learning/recorder.py` | Record trade outcomes to PostgreSQL | `TradeOutcomeRecorder` |
| `learning/updater.py` | Update Kelly inputs from trade outcomes | `KellyInputUpdater` |
| `observability/logging.py` | structlog JSON configuration with file rotation | — |
| `observability/metrics.py` | Prometheus metric definitions | — |

---

## 8. Startup Dependency Order

```
1. PostgreSQL  — must be running (kill switch state recovery depends on it)
2. Memurai     — must be running (Redis feature cache)
3. MT5 Terminal — must be logged in and connected to broker
4. APEX V4     — NSSM service starts after dependencies
5. Prometheus  — optional, scrapes :8000
6. Grafana     — optional, connects to Prometheus
```

On startup, the pipeline:
1. Loads `config/settings.yaml` and `config/secrets.env`
2. Runs 9-point pre-flight validation
3. Connects to PostgreSQL and Redis
4. Recovers kill switch state from PostgreSQL (survives restart)
5. Starts StateReconciler in background (5s heartbeat)
6. Starts Prometheus metrics server on port 8000
7. Enters the main ZMQ pull loop

---

## 9. Architecture Decisions (ADRs)

| ADR | Decision | Rationale |
|---|---|---|
| ADR-001 | Hard ADX rules, no ML for regime | ML regime classifiers drift; deterministic rules are auditable |
| ADR-002 | Minimum 30 trade outcomes per segment before live | Statistical validity floor |
| ADR-003 | Quarter-Kelly, 2% hard cap | Full Kelly is practically ruinous; proven quarter-Kelly protects capital |
| ADR-004 | PostgreSQL is source of truth; Redis is cache | Redis is volatile; WAL guarantees audit trail |
| ADR-005 | asyncio.Lock for kill switch state | Plain booleans have race conditions under concurrent signals |
| ADR-006 | ZMQ PUSH/PULL for feed→pipeline | Simple, high-throughput, no broker dependency |
| ADR-007 | EWMA updated on H1 close only, not tick | O(N²) matrix ops at tick frequency crashes Python pipeline |
| ADR-008 | AI (LLM) is never used for position sizing | LLM softmax ≠ posterior probability; Kelly inputs from trade outcomes only |
| ADR-009 | Single-process asyncio, no threads (except to_thread) | Simplicity; avoids GIL and shared-state bugs |
