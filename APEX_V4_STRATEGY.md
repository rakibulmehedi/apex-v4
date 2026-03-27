# APEX V4 — Architectural Strategy Document

> **Author:** Senior Software Architect
> **Context:** 20+ years in distributed systems, quantitative finance infrastructure, production trading systems
> **Status:** LOCKED — this document supersedes all prior AI-generated proposals
> **Last Updated:** 2026-03-20

---

## Executive Summary

APEX V3 is a 40K-line algorithmic trading system with a structurally sound 4-layer architecture
that failed not because of bad ideas, but because of **bad execution discipline** — hardcoded
values, untested risk math, and no state reconciliation. The system scored 74/100 in forensic
audit. That means 26 points of fixable, preventable failures.

APEX V4 is not a rewrite. It is a **disciplined rebuild** of the same architecture with:

1. Corrected mathematics (Kelly, conviction, covariance)
2. Proven open-source components where they exist
3. A regime-gated hybrid strategy (trend + mean reversion)
4. A kill switch hierarchy that actually works
5. State reconciliation that was never there before

**Target:** Forex only (MT5). **Strategy:** Hybrid regime-based. **Path:** Fix V3 → Build V4 → Gradual migration.

---

## Table of Contents

1. [Context & Problem Statement](#1-context--problem-statement)
2. [What We Learned from Prior Analysis](#2-what-we-learned-from-prior-analysis)
3. [Open Source Stack Decisions](#3-open-source-stack-decisions)
4. [Architecture Decisions (ADRs)](#4-architecture-decisions-adrs)
5. [System Architecture](#5-system-architecture)
6. [Module Contracts](#6-module-contracts)
7. [Mathematics Reference](#7-mathematics-reference)
8. [Build Sequence](#8-build-sequence)
9. [Quality Gates](#9-quality-gates)
10. [What Not To Build](#10-what-not-to-build)

---

## 1. Context & Problem Statement

### 1.1 Where We Are

| Asset | State | Action |
|---|---|---|
| APEX V3 codebase | Running, 4 critical bugs | Fix bugs, keep running during V4 build |
| V3 audit findings | 74/100, documented | Remediation plan exists |
| V4 architecture | Designed, not implemented | This document is the implementation contract |
| Gemini research docs | Reviewed, 4 PDFs | Selectively adopted — bugs identified and corrected |
| awesome-ai-in-finance | Reviewed | 5 libraries extracted, rest discarded |

### 1.2 The Real Problems in V3

Not architecture. **Discipline.**

```
CRITICAL (block live deployment):
  1. Hardcoded portfolio value → wrong risk sizing as account changes
  2. Conviction fallback = 1.0 → maximum trade on calculation failure
  3. Phantom fill tracking → state diverges from broker before confirmation
  4. Full Kelly, no cap → mathematically correct but practically ruinous

SUSTAINED-TRADING (fix within 2 weeks of live):
  5. EWMA covariance not updating from live data
  6. Kelly inputs never updated from trade outcomes
  7. Spread checks present but gating logic broken
  8. Kill switch has race condition under concurrent signals
  9. Redis is sole source of truth — no PostgreSQL WAL
  10. No state reconciliation between Redis and MT5 broker
```

### 1.3 Why V3 Scored 74 and Not 90+

The architecture was right. The gap was:
- **No feedback loop** — the system never learned from its own trades
- **No state reconciliation** — distributed state drifted silently
- **Mathematical shortcuts** — Kelly and conviction treated as implementation details, not core logic
- **No minimum sample gate** — signals fired with zero historical validation

---

## 2. What We Learned from Prior Analysis

### 2.1 GPT-o3 V4 Proposal — What to Take

✅ Quant Calibration Engine concept (correct diagnosis of V3's missing layer)
✅ Portfolio-level VaR with correlation matrix
✅ Post-trade feedback loop architecture
✅ AI = research only, never position sizing

### 2.2 GPT-o3 V4 Proposal — What to Discard

❌ "AI confidence = p_win" — statistically invalid. LLM softmax ≠ posterior probability
❌ "EWMA updated every tick" — O(N²) matrix ops at tick frequency crashes Python pipeline
❌ "Kafka OR ZeroMQ via config" — these are not interchangeable, fundamentally different systems
❌ Kill switch as plain Python boolean — no thread safety, race condition guaranteed

### 2.3 Gemini Research Docs — Adoption Map

| Document | Take | Discard |
|---|---|---|
| ML Feedback Loop | ZMQ PUSH/PULL topology, FlatBuffers schema concept | SGLD on OU params (wrong math), slippage → Kalman innovation (wrong fusion) |
| Risk Governor | EWMA covariance, eigenvalue shrinkage, NC dead-man switch | mu_vector = zeros bug, Python bool kill switch, TSRV unimplemented |
| Alpha Engine | Kalman math, OU MLE formulas, erf conviction mapping, einsum batching | CMA-ES real-time (infeasible), missing cointegration gate, 3σ → max position (dangerous) |
| C++ Infrastructure | Cache-line alignment, SPSC ring buffer, SoA layout, POD+ZMQ zero-copy | DPDK, FPGA, nanosecond LOB (MT5 broker is 10-50ms floor — these are theater) |

### 2.4 The One Insight No Document Had

**State Reconciliation.** Every document assumed state consistency. None addressed what happens when:
- The execution gateway crashes mid-order
- A fill ACK arrives but the ZMQ message drops
- Redis and MT5 broker positions diverge

This is not an edge case. This is **guaranteed to happen** in live trading. It is the single most dangerous gap in all prior analysis.

---

## 3. Open Source Stack Decisions

### 3.1 What We Use and Why

After reviewing `georgezouq/awesome-ai-in-finance` (4.9k stars, 530 forks), the following
libraries are adopted for APEX V4. All others are discarded.

#### TA-Lib — Feature Fabric

```bash
pip install TA-Lib==0.4.28
```

Battle-tested C library with Python bindings. 150+ indicators. Every ATR, ADX, EMA,
Bollinger band implementation is validated against industry standard. Writing these from
numpy scratch is unnecessary risk.

**Replaces:** All custom numpy indicator implementations in Phase 1 Feature Fabric.

```python
import talib
atr            = talib.ATR(high, low, close, timeperiod=14)
adx            = talib.ADX(high, low, close, timeperiod=14)
ema            = talib.EMA(close, timeperiod=200)
upper, mid, lower = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2)
```

#### backtrader — Backtesting Harness

```bash
pip install backtrader==1.9.78.123
```

18k stars, mature, supports MT5 data feeds via custom broker adapters.
**Replaces:** Custom `scripts/backtest.py`.

#### pyfolio-reloaded — Performance Analytics

```bash
pip install pyfolio-reloaded==0.9.5
```

Used by Quantopian, Zipline. Sharpe ratio, max drawdown, monthly returns — all pre-built.
Use `pyfolio-reloaded` not `pyfolio` — original is abandoned.

#### filterpy — Kalman Filter

```bash
pip install filterpy==1.4.5
```

Production-grade Kalman filter. Handles numerical stability correctly.
**Replaces:** Custom `src/alpha/kalman.py`.

```python
from filterpy.kalman import KalmanFilter
kf = KalmanFilter(dim_x=1, dim_z=1)
kf.x = np.array([[price]])
kf.F = np.array([[1.]])
kf.H = np.array([[1.]])
kf.R = rolling_variance   # from data, not static
kf.Q = process_noise
kf.predict()
kf.update(new_price)
filtered_state = kf.x[0, 0]
```

#### statsmodels — Cointegration Gate

```bash
pip install statsmodels==0.14.1
```

ADF test for stationarity is mandatory before OU fitting.

```python
from statsmodels.tsa.stattools import adfuller
result  = adfuller(price_series, maxlag=1, regression='c', autolag=None)
p_value = result[1]
if p_value > 0.05:
    return None  # not stationary — OU model invalid — no trade
```

### 3.2 What We Do Not Use

| Library | Reason |
|---|---|
| Gekko / zenbot | Abandoned 2020, crypto-only |
| FinRL / TensorTrade | Stock market RL — MT5 Forex context mismatch |
| FinGPT / PIXIU | LLM research — not production trading infrastructure |
| Zipline | Requires full Quantopian ecosystem, overkill for MT5 |
| Kafka | Single-operator system. PostgreSQL WAL achieves replay. Kafka adds operational overhead for no benefit at this scale |
| DPDK / FlatBuffers | Nanosecond optimization behind 10-50ms MT5 broker — irrelevant |

---

## 4. Architecture Decisions (ADRs)

### ADR-001: Hybrid Regime-Based Strategy

**Decision:** TRENDING regime → momentum. RANGING regime → mean reversion. UNDEFINED → no trade.

```
TRENDING_UP   → ADX > 25 AND close > EMA200 → Momentum engine
TRENDING_DOWN → ADX > 25 AND close < EMA200 → Momentum engine
RANGING       → ADX < 20                    → Mean Reversion engine
UNDEFINED     → ADX 20-25, news, spread     → NO TRADE
```

### ADR-002: p_win Source

**Decision:** p_win comes exclusively from the PostgreSQL trade history database,
segmented by (strategy × regime × session). Never from AI confidence scores.
Minimum 30-trade sample gate before any segment goes live.

### ADR-003: ZeroMQ + PostgreSQL WAL, Not Kafka

**Decision:** ZeroMQ for real-time IPC, PostgreSQL write-ahead log for durability.
Every signal, decision, and fill is written to PostgreSQL before execution.

### ADR-004: State Reconciliation is Mandatory Infrastructure

**Decision:** A dedicated StateReconciler runs on 5-second heartbeat, diffs Redis vs MT5
broker, triggers HARD kill switch on any mismatch. Broker state is always truth.

### ADR-005: Three-Level Kill Switch, Not Binary

**Decision:** SOFT (no new signals) → HARD (flatten all positions) → EMERGENCY
(disconnect broker, alert, write state to disk). Each level persisted to Redis AND
PostgreSQL. State managed via `asyncio.Lock()`, not plain boolean.

### ADR-006: TA-Lib for All Indicators

**Decision:** Zero custom numpy implementations for standard indicators.

### ADR-007: filterpy for Kalman Filter

**Decision:** R_k calibrated from rolling 100-candle variance, updated each candle close.
Not static. Not CMA-ES (infeasible at candle frequency).

### ADR-008: ADF Test is Mandatory Gate Before OU Fitting

**Decision:** ADF stationarity (p < 0.05) is a hard prerequisite.
Half-life gate: reject if > 48 H1 candles. 3-sigma events → regime break flag, not position entry.

### ADR-009: MT5 Python API Only, No Direct FIX

**Decision:** MT5 Python API via `MetaTrader5` library.
C++ execution engine and direct FIX deferred until migrating to prime broker.

---

## 5. System Architecture

```
MT5 BROKER FEED
      │
      ▼
┌─────────────────────────────────────────────────────────────┐
│  MARKET INPUT LAYER          src/market/                    │
│  feed.py — async MT5 tick stream + candle builder           │
│  validator.py — MarketSnapshot schema validation            │
│  Output → ZMQ PUSH → tcp://127.0.0.1:5559                  │
└─────────────────────────┬───────────────────────────────────┘
                          │ ZMQ PULL
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  FEATURE FABRIC              src/features/                  │
│  fabric.py — TA-Lib: ATR, ADX, EMA200, BB, Session         │
│  state.py — Redis TTL 300s + PostgreSQL WAL                 │
│  Output → FeatureVector                                     │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  REGIME CLASSIFIER           src/regime/                    │
│  classifier.py — hard ADX rules, no ML, no probabilities    │
└──────────────┬──────────────────────────┬───────────────────┘
               │ TRENDING                 │ RANGING
               ▼                          ▼
┌──────────────────────┐    ┌─────────────────────────────────┐
│  MOMENTUM ENGINE     │    │  MEAN REVERSION ENGINE          │
│  src/alpha/momentum  │    │  ADF gate → filterpy Kalman     │
│  Multi-TF confluence │    │  OU MLE → erf conviction score  │
│  ATR stops, R:R≥1.8  │    │  3σ guard → no trade            │
└──────────┬───────────┘    └────────────┬────────────────────┘
           └──────────────┬──────────────┘
                   AlphaHypothesis
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  QUANT CALIBRATION ENGINE    src/calibration/               │
│  history.py — PostgreSQL segment lookup (min 30 trades)     │
│  engine.py — edge calc, quarter-Kelly, drawdown scalar      │
│  Output → CalibratedTradeIntent (or None if no edge)        │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  PORTFOLIO RISK ENGINE       src/risk/                      │
│  governor.py — 7 sequential gates, fail-fast                │
│  covariance.py — EWMA + eigenvalue shrinkage (candle close) │
│  kill_switch.py — 3 levels, asyncio.Lock, persisted         │
│  reconciler.py — 5s heartbeat, Redis vs MT5 diff            │
│  Output → RiskDecision (APPROVE / REJECT / REDUCE)          │
└─────────────────────────┬───────────────────────────────────┘
                          │ APPROVE only
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  EXECUTION GATEWAY           src/execution/                 │
│  gateway.py — pre-flight checks, mt5.order_send()           │
│  fill_tracker.py — slippage measurement, PostgreSQL write   │
└─────────────────────────┬───────────────────────────────────┘
                          │ FillReport
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  POST-TRADE LEARNING LOOP    src/learning/                  │
│  recorder.py — outcome → PostgreSQL trade_outcomes          │
│  updater.py — segment stats recalc → Redis cache update     │
└─────────────────────────────────────────────────────────────┘

BACKGROUND SERVICES (always running):
  StateReconciler   — 5s heartbeat, Redis ↔ MT5 diff
  PrometheusServer  — metrics endpoint :8000
  systemd           — process supervision + auto-restart
```

### State Architecture

```
REDIS (cache, TTL on all keys)        POSTGRESQL (truth, append-only)
──────────────────────────────        ──────────────────────────────
fv:{pair}           TTL 300s          candles
open_positions      TTL 60s           feature_vectors
kill_switch         no TTL            market_snapshots
last_reconcile_ts   TTL 30s           signals
news_blackout:{pair} TTL varies       trade_outcomes
segment:{s}:{r}:{s} TTL 3600s        kill_switch_events
                                      fills
                                      reconciliation_log
```

**Rule:** Redis is always derived from PostgreSQL.
On restart, Redis is populated from PostgreSQL. Never the reverse.

---

## 6. Module Contracts

### MarketSnapshot

```python
{
    "type":          "MarketSnapshot",
    "pair":          str,           # 6 chars e.g. "EURUSD"
    "timestamp":     int,           # unix ms UTC
    "candles": {
        "M5":        List[OHLCV],   # min 50 candles
        "M15":       List[OHLCV],   # min 50 candles
        "H1":        List[OHLCV],   # min 200 candles
        "H4":        List[OHLCV],   # min 50 candles
    },
    "spread_points": float,         # > 0
    "session":       str,           # LONDON|NY|ASIA|OVERLAP
    "is_stale":      bool,          # True if > 5000ms old
}
```

### FeatureVector

```python
{
    "type":          "FeatureVector",
    "pair":          str,
    "timestamp":     int,
    "atr_14":        float,
    "adx_14":        float,
    "ema_200":       float,
    "bb_upper":      float,
    "bb_lower":      float,
    "bb_mid":        float,
    "session":       str,
    "spread_ok":     bool,
    "news_blackout": bool,
}
```

### AlphaHypothesis

```python
{
    "type":          "AlphaHypothesis",
    "strategy":      "MOMENTUM" | "MEAN_REVERSION",
    "pair":          str,
    "direction":     "LONG" | "SHORT",
    "entry_zone":    [float, float],
    "stop_loss":     float,
    "take_profit":   float,
    "setup_score":   int,             # 0-30
    "expected_R":    float,           # >= 1.8 required
    "regime":        str,
    "conviction":    float | None,    # MR only, 0.65-1.0
}
```

### CalibratedTradeIntent

```python
{
    "p_win":          float,   # from PostgreSQL — never from AI
    "expected_R":     float,
    "edge":           float,   # must be > 0
    "suggested_size": float,   # 0.0 to 0.02
    "segment_count":  int,     # historical trades in this segment
}
```

### RiskDecision

```python
{
    "decision":    "APPROVE" | "REJECT" | "REDUCE",
    "final_size":  float,
    "reason":      str,
    "risk_state":  "NORMAL" | "THROTTLE" | "HARD_STOP",
    "gate_failed": int | None,   # 1-7, None if APPROVE
}
```

---

## 7. Mathematics Reference

All formulas are validated and locked. These supersede anything in prior documents.

### 7.1 Kelly Criterion

```
f*        = (p × b - q) / b
            where p = p_win, q = 1 - p_win, b = avg_R

f_quarter = f* × 0.25
f_final   = min(f_quarter, 0.02)      # hard cap: never > 2% of portfolio

dd_scalar = 1.0   if current_dd < 0.02
          = 0.5   if 0.02 ≤ current_dd < 0.05
          = 0.0   if current_dd ≥ 0.05   # no new trades

size = f_final × dd_scalar × correlation_scalar × portfolio_equity
```

### 7.2 OU Process Parameters (MLE)

```
ρ         = lag-1 autocorrelation of filtered state X
θ         = -ln(ρ) / Δt
μ         = mean(X)
ε_i       = X[i+1] - X[i]·e^(-θΔt) - μ(1 - e^(-θΔt))
σ²        = (2θ / T(1-e^(-2θΔt))) × Σ(ε_i²)

half_life = ln(2) / θ
if half_life > 48:  return None   # reverts slower than 2 days
if ρ <= 0:          return None   # no mean reversion
```

### 7.3 Conviction Score

```
σ_eq = sqrt(σ² / 2θ)
z    = (x_current - μ) / σ_eq

if |z| > 3.0: return None         # regime break — do not trade

C = erf(|z| / sqrt(2))            # bounded [0, 1]

if C < 0.65:  return None         # insufficient edge
```

### 7.4 EWMA Covariance

```
Σ_t = λ × Σ_{t-1} + (1-λ) × (r_t × r_t^T)
λ   = 0.999    # H1 candle close update — not tick

Eigenvalue shrinkage if κ > κ_warn (15.0):
  floor        = max(eigenvalues) / κ_warn
  eigenvalues  = where(eigenvalues < floor, floor, eigenvalues)
  Σ_reg        = eigenvectors @ diag(eigenvalues) @ eigenvectors.T

Φ(κ) = 1.0                        if κ ≤ 15.0
      = exp(-γ × (κ - 15.0))      if 15.0 < κ < 30.0
      = 0.0                        if κ ≥ 30.0
```

### 7.5 Portfolio VaR (99%)

```
σ²_portfolio = W^T × Σ_reg × W
VaR_99       = 2.326 × sqrt(σ²_portfolio) × portfolio_value

if VaR_99 > 0.05 × portfolio_value: REJECT — gate_5_breach
if VaR_99 > 0.03 × portfolio_value: trigger SOFT kill switch
```

---

## 8. Build Sequence

### Phase 0 — V3 Bug Fixes (Week 1)

```
P0.1  git tag v3.0-pre-fix on current V3
P0.2  Fix portfolio_value: mt5.account_info().equity
P0.3  Fix conviction fallback: return 0.0 on failure (not 1.0)
P0.4  Fix fill recording: only after TRADE_RETCODE_DONE
P0.5  Fix Kelly: quarter-Kelly + 2% cap + drawdown scalar
P0.6  Write unit tests for all 4 fixes
P0.7  All tests pass → git tag v3.1-fixed → deploy to live
```

**Exit criteria:** 4 tests pass. V3 running live with fixes.

### Phase 1 — Foundation (Week 2-3)

```
P1.1  PostgreSQL schema (5 tables via Alembic)
P1.2  Pydantic v2 schemas for all data contracts
P1.3  Async MT5 feed + candle builder
P1.4  Feature Fabric using TA-Lib
P1.5  Redis state manager (TTL, staleness detection)
P1.6  Unit tests: each indicator vs known values
```

**Exit criteria:** MarketSnapshot flows end-to-end. FeatureVector in Redis + PostgreSQL. All unit tests pass.

### Phase 2 — Alpha Engines (Week 4-5)

```
P2.1  Regime classifier (hard ADX rules, no ML)
P2.2  Momentum engine (multi-TF, ATR stops, min R:R gate)
P2.3  ADF cointegration gate (statsmodels)
P2.4  filterpy Kalman filter (rolling R_k, not static)
P2.5  OU MLE calibration (rolling 200 H1 candles)
P2.6  Conviction score + 3-sigma guard
P2.7  Mean reversion engine (full pipeline)
P2.8  backtrader backtesting (6 months historical data)
```

**Exit criteria:** Regime distribution 25-35% trending / 35-45% ranging. Positive expected R.

### Phase 3 — Risk Engine (Week 6-7)

```
P3.1  Performance database (PostgreSQL segment stats, rolling 90d)
P3.2  Calibration engine (edge calc, quarter-Kelly, all scalars)
P3.3  EWMA covariance + eigenvalue shrinkage
P3.4  Kill switch (3 levels, asyncio.Lock, persisted)
P3.5  State reconciliation service (5s heartbeat)
P3.6  7-gate risk governor (sequential, fail-fast)
P3.7  Chaos tests (all 5 must pass)
```

**Exit criteria:** All chaos tests pass. Kill switch survives process restart. State drift triggers HARD stop within 5 seconds.

### Phase 4 — Execution + Learning (Week 8)

```
P4.1  Execution gateway (pre-flight, mt5.order_send)
P4.2  Fill tracker (slippage measurement, PostgreSQL write)
P4.3  Trade outcome recorder (R-multiple, segment attribution)
P4.4  Kelly input updater (segment-level, rolling 90d)
P4.5  pyfolio performance reporting
P4.6  Full integration test (snapshot → order → outcome)
```

**Exit criteria:** Full pipeline end-to-end in simulation. Outcome feeds back into calibration.

### Phase 5 — Observability + Paper Trading (Week 9-10)

```
P5.1  Prometheus metrics
P5.2  Paper trading mode flag
P5.3  systemd service
P5.4  7 days continuous paper trading
P5.5  Zero state drift events
P5.6  Win rate > 50% over 50+ paper trades
```

**Exit criteria:** 7 days paper trading. Zero crashes. Zero state drift.

### Phase 6 — Live Migration (Week 11-12)

```
P6.1  Import V3 historical trades → seed trade_outcomes
P6.2  Verify all segments ≥ 30 outcomes
P6.3  Complete 10-item go-live checklist
P6.4  Week 11: V4 live at 10% capital allocation
P6.5  Week 12: Evaluate vs V3 metrics
P6.6  Week 12+: Scale to 50% if V4 outperforms
P6.7  V3 standby for 30 days post migration
```

**Exit criteria:** First live trade executed and recorded correctly.

---

## 9. Quality Gates

### Per-Phase Gates

```
Phase 0:  pytest tests/unit/test_v3_fixes.py → 4 PASSED
Phase 1:  pytest tests/unit/test_phase1.py → ALL PASSED
Phase 2:  Backtest regime distribution within expected ranges
Phase 3:  pytest tests/chaos/ → ALL PASSED
Phase 4:  Full integration test passes in simulation mode
Phase 5:  7 days paper trading, 0 crashes, 0 state drift
Phase 6:  10-item checklist 100% complete
```

### Production Readiness Metrics

| Metric | Minimum | Target |
|---|---|---|
| Win rate (100+ trades) | > 50% | 54-58% |
| Average R (winning trades) | > 1.5R | 1.8-2.2R |
| Maximum drawdown | < 10% | < 6% |
| Kill switch triggers / month | < 3 SOFT, 0 HARD | 0 |
| State drift events | 0 unresolved | 0 |
| Signal latency | < 500ms | < 200ms |
| Average slippage | < 2 points | < 1 point |
| Min sample before live trade | 30 outcomes/segment | 50 |

---

## 10. What Not To Build

| Feature | Why Deferred |
|---|---|
| Direct FIX API | MT5 broker is current venue. FIX deferred until prime broker |
| FPGA / DPDK / nanosecond LOB | 10-50ms broker floor makes this irrelevant |
| Multi-asset (crypto, indices) | Prove Forex first |
| Reinforcement Learning alpha | Insufficient data. Deferred |
| LLM position sizing | Statistically invalid |
| Kafka | PostgreSQL WAL achieves replay at this scale |
| CMA-ES real-time calibration | Infeasible at candle frequency |
| Multi-tenant architecture | Single operator first |

---

## Appendix: Folder Structure

```
apex_v4/
├── CLAUDE.md                    ← agent instructions (auto-loaded)
├── WORKSPACE.md                 ← workspace layout and rules
├── APEX_V4_STRATEGY.md          ← this document
├── APEX_V4_WORKFLOW_ORCHESTRATION.md
├── README.md
├── requirements.txt
├── .gitignore
├── config/
│   ├── settings.yaml
│   └── secrets.env              # never in git
├── tasks/
│   ├── todo.md                  # active task plan (managed by Claude)
│   └── lessons.md               # accumulated lessons (managed by Claude)
├── .claude/
│   └── commands/                # custom slash commands (skills)
│       ├── implement.md
│       ├── audit.md
│       ├── fix.md
│       ├── phase-gate.md
│       ├── risk-verify.md
│       └── hardening.md
├── src/
│   ├── market/
│   │   ├── feed.py
│   │   ├── validator.py
│   │   └── schemas.py
│   ├── features/
│   │   ├── fabric.py
│   │   └── state.py
│   ├── regime/
│   │   └── classifier.py
│   ├── alpha/
│   │   ├── momentum.py
│   │   └── mean_reversion.py
│   ├── calibration/
│   │   ├── engine.py
│   │   └── history.py
│   ├── risk/
│   │   ├── governor.py
│   │   ├── covariance.py
│   │   ├── kill_switch.py
│   │   └── reconciler.py
│   ├── execution/
│   │   ├── gateway.py
│   │   └── fill_tracker.py
│   ├── learning/
│   │   ├── recorder.md
│   │   └── updater.py
│   └── pipeline.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── chaos/
├── db/
│   └── migrations/
├── ops/
│   ├── apex_v4.service
│   └── prometheus.yml
└── scripts/
    ├── migrate_v3_data.py
    └── backtrader_backtest.py
```

---

*This document is version-controlled. All changes require a commit message explaining
the architectural rationale. Do not edit without understanding downstream consequences.*
