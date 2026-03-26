# APEX V4 — Claude CLI Execution Playbook

> Copy. Paste. Execute. In this exact order.
> Every prompt is self-contained and production-ready.

---

## Before You Start

```bash
# Every session — run this first, always
cd ~/Desktop/apex_v4 && source venv/bin/activate && claude
```

---

## Chapter 1: One-Time Setup

### 1-A: Mac Environment

Paste this into Terminal (before Claude Code is installed):

```
I need to set up a professional Python trading system development
environment on macOS from scratch.

Requirements:
- Homebrew package manager
- Python 3.11 via pyenv (not system Python)
- Node.js 20 LTS via nvm
- TA-Lib C library via Homebrew (required before pip install)
- Git configured with my identity

Assess what is already installed, install what is missing,
and verify everything works. Operate autonomously.
Do not ask for confirmation between steps.
```

---

### 1-B: Claude Code CLI

```
Install Claude Code CLI globally via npm and authenticate it.
Verify with a simple test prompt.
Show the installed version and confirm authentication succeeded.
```

---

### 1-C: Project Scaffold

```
Create the APEX V4 project at ~/Desktop/apex_v4.

Read APEX_V4_STRATEGY.md Appendix for the exact folder structure.
Create every folder and placeholder file listed there including
the tasks/ folder with tasks/todo.md and tasks/lessons.md.

Then:
- Create Python 3.11 virtual environment at venv/
- Install all libraries:
  MetaTrader5, numpy, scipy, pandas, TA-Lib, statsmodels,
  filterpy, redis, psycopg2-binary, pyzmq, msgpack, pydantic,
  pytest, pytest-asyncio, prometheus-client, structlog,
  python-dotenv, alembic, backtrader, pyfolio-reloaded, empyrical
- Create requirements.txt from pip freeze
- Initialize git with branch main
- Make first commit: "chore: apex v4 scaffold"
- Test that import talib works — this fails on macOS if
  brew install ta-lib was skipped

Report any installation failures clearly.
```

---

### 1-D: Install Custom Skills

```
Create the Claude Code slash command system for APEX V4.
All 6 files go in ~/Desktop/apex_v4/.claude/commands/

implement.md — Purpose: Given a phase and component,
read APEX_V4_STRATEGY.md, implement to production standard,
write tests, verify passing. Operate fully autonomously.

audit.md — Purpose: Full architecture compliance check.
Compare every module against APEX_V4_STRATEGY.md.
Report ADR compliance, missing implementations, formula
deviations. Produce a scored report with exact fixes.

fix.md — Purpose: Given an error or failing test, diagnose
root cause, implement fix, re-run tests, confirm resolution.
Never ask for guidance. Reason through it autonomously.

phase-gate.md — Purpose: Run the quality gate checklist for
the specified phase from APEX_V4_STRATEGY.md Section 9.
Execute all tests. Produce PASS/FAIL report with evidence.

risk-verify.md — Purpose: Extract every formula from the
risk engine and calibration engine. Verify each against
Section 7 of APEX_V4_STRATEGY.md exactly. Flag every
deviation with the correct formula and required fix.

hardening.md — Purpose: Review codebase for production
readiness. Check error handling, logging, no hardcoded values,
secrets from environment, thread safety of concurrent
components. Produce a prioritized fix list.

Confirm all 6 files were created.
```

---

## Chapter 2: Phase 0 — V3 Critical Fixes

### 2-A: Execute Fixes

```
Read CLAUDE.md. Follow the session start protocol.
V3 is at ~/Desktop/apex_v3.
Architecture reference: APEX_V4_STRATEGY.md.

Fix the 4 critical bugs listed in Section 1.2.

For each bug:
1. Find the broken code — do not ask, find it yourself
2. Understand why it is production-dangerous
3. Implement the correct version per the strategy document
4. Write a unit test that would have caught this bug
5. Verify the test passes

After all 4 are fixed:
- Run full test suite
- Tag git as v3.1-fixed
- Give me a confidence score 0-100 per fix with reasoning

Operate fully autonomously.
```

---

### 2-B: Phase Gate Check

```
Run /phase-gate for Phase 0.
Show me evidence for each exit criterion from
APEX_V4_STRATEGY.md Section 9.
Do not mark complete until all criteria are met.
```

---

## Chapter 3: Phase 1 — Data & Feature Foundation

### 3-A: Database Schema

```
Read CLAUDE.md. Session start protocol.
V4 build target: ~/Desktop/apex_v4.
Architecture: APEX_V4_STRATEGY.md.

Implement the PostgreSQL database schema for APEX V4.
Use Alembic for migrations.

Tables required (from Section 6 module contracts):
- market_snapshots
- candles
- feature_vectors
- trade_outcomes
- kill_switch_events
- fills
- reconciliation_log

After creating migrations: run alembic upgrade head,
verify all tables exist, make commit "feat: database schema".
```

---

### 3-B: Data Contracts

```
Implement src/market/schemas.py using Pydantic v2.

Create these models with full validation exactly as
defined in APEX_V4_STRATEGY.md Section 6:
- OHLCV
- MarketSnapshot (with is_stale computed field)
- FeatureVector
- AlphaHypothesis
- CalibratedTradeIntent
- RiskDecision

Every field must have the type, validation, and constraints
from the spec. No additional fields. No missing fields.
Write unit tests that prove invalid data is rejected.
```

---

### 3-C: MT5 Market Feed

```
Implement src/market/feed.py — async MT5 data ingestion.

Requirements:
- Connect to MT5 using credentials from config/secrets.env
- Support pairs: EURUSD, GBPUSD, USDJPY, AUDUSD
- On H1, M15, M5 candle close: build and validate MarketSnapshot
- Session classifier: LONDON 07-16 UTC, NY 12-21 UTC,
  ASIA 22-07 UTC, OVERLAP 12-16 UTC
- Mark is_stale=True if last tick > 5000ms old
- Publish valid snapshots to ZMQ PUSH ipc:///tmp/apex_market.ipc
- On validation failure: log error, skip — never propagate bad data

Use asyncio. Use structlog for all logging.
Unit tests must mock MT5 entirely.
```

---

### 3-D: Feature Fabric

```
Implement src/features/fabric.py.

Use TA-Lib exclusively for all indicator calculations.
Do not write custom numpy implementations.

Calculate from H1 candles in MarketSnapshot:
- talib.ATR(high, low, close, timeperiod=14) → atr_14
- talib.ADX(high, low, close, timeperiod=14) → adx_14
- talib.EMA(close, timeperiod=200) → ema_200
- talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2) → bb_upper, bb_mid, bb_lower

Additional fields:
- spread_ok: snapshot.spread_points < threshold from settings.yaml
- news_blackout: read from Redis key "news_blackout_{pair}"

Raise ValueError with message if fewer than 200 H1 candles.
Unit tests: verify each indicator against known input/output pairs.
```

---

### 3-E: State Manager

```
Implement src/features/state.py.

RedisStateManager class:
- store_feature_vector(fv): key "fv:{pair}", TTL 300s
- get_feature_vector(pair) → FeatureVector | None
- store_open_positions(positions): key "open_positions", TTL 60s
- get_open_positions() → list
- set_kill_switch(level): key "kill_switch", no TTL
- get_kill_switch() → str | None
- set_news_blackout(pair, active, duration_minutes)

PostgresWriter class:
- write_feature_vector(fv): insert into feature_vectors
- write_trade_outcome(outcome): insert into trade_outcomes
- write_kill_switch_event(level, reason): insert into kill_switch_events
- All writes async, non-blocking
- On error: log critical, do NOT crash the pipeline

All connection details from environment variables only.
```

---

### 3-F: Phase 1 Gate

```
Run /phase-gate for Phase 1.
Then run: pytest tests/unit/ -v
Show me the full output.
Tag git as v4.0-phase1 only if all criteria pass.
```

---

## Chapter 4: Phase 2 — Regime Classifier & Alpha Engines

### 4-A: Regime Classifier

```
Implement src/regime/classifier.py.

Use hard ADX rules only. No ML. No probabilities.

Logic in exact order:
1. news_blackout is True → UNDEFINED
2. spread_ok is False → UNDEFINED
3. adx_14 > 25 AND close > ema_200 → TRENDING_UP
4. adx_14 > 25 AND close < ema_200 → TRENDING_DOWN
5. adx_14 < 20 → RANGING
6. Everything else (ADX 20-25) → UNDEFINED

Log every classification: pair, adx value, close vs ema200, result.
Unit tests: verify all 6 branches with synthetic FeatureVectors.
```

---

### 4-B: Momentum Engine

```
Implement src/alpha/momentum.py.

Fires only on TRENDING_UP or TRENDING_DOWN regime.

Signal logic:
- Direction from regime state
- Multi-TF confirmation: H4 EMA20 and H1 EMA20 must agree with direction
- Entry zone: M15 EMA20 ± (0.2 × atr_14)
- Stop loss: entry ± (1.5 × atr_14) against direction
- Take profit: entry ± (2.0 × atr_14 × 2.0) in direction
- Setup score 0-30: +10 H4 confirms, +10 ADX>30, +5 LONDON/OVERLAP, +5 spread<1pt
- Reject if expected_R < 1.8

Return None with a logged reason for every rejection.
Unit tests for each scoring component and rejection condition.
```

---

### 4-C: Mean Reversion Pipeline

```
Implement the mean reversion alpha engine in 3 files:

FILE 1 — src/alpha/mean_reversion.py (orchestrator):
Fires only on RANGING regime.
Requires minimum 200 H1 candles.
Pipeline: ADF gate → Kalman filter → OU fit → conviction → signal.
Return None with reason at every gate failure.

FILE 2 — Integration of filterpy Kalman:
Use filterpy.kalman.KalmanFilter(dim_x=1, dim_z=1).
Set R from rolling variance of last 100 candles — not static.
Update on each new H1 candle close.

FILE 3 — OU MLE calibration (exact Section 7.2 formulas):
ρ = lag-1 autocorrelation
θ = -ln(ρ) / Δt
μ = mean(X)
σ² = exact formula from Section 7.2
half_life = ln(2) / θ
Reject if ρ ≤ 0 or half_life > 48.

Conviction score (exact Section 7.3):
σ_eq = sqrt(σ² / 2θ)
z = (x_current - μ) / σ_eq
if |z| > 3.0: return None — log "regime_break_suspected"
C = erf(|z| / sqrt(2))
if C < 0.65: return None

Run /risk-verify after implementation to confirm formula match.
```

---

### 4-D: Backtest Validation

```
Run a backtrader backtest on 6 months of EURUSD H1 data.

Use backtrader with a custom MT5 data feed adapter.
Run both momentum and mean reversion strategies through
the regime classifier.

Report:
- Total candles processed
- Regime distribution: % trending, % ranging, % undefined
- Signals per strategy
- Average conviction for MR signals
- Average expected R for momentum signals

Expected: 25-35% trending, 35-45% ranging.
If outside these ranges, adjust ADX threshold by ±2,
explain the adjustment, and re-run until within range.

Tag git as v4.0-phase2 after backtest validates.
```

---

## Chapter 5: Phase 3 — Risk Engine

### 5-A: Performance Database

```
Implement src/calibration/history.py.

PerformanceDatabase class:
- get_segment_stats(strategy, regime, session) → dict | None
  Query trade_outcomes for last 90 days.
  Return None if count < 30 (insufficient sample).
  Return: {win_rate, avg_R, trade_count, last_updated}

- update_segment(outcome): insert into trade_outcomes

- bootstrap_from_v3(v3_data): import V3 historical trades,
  map to trade_outcomes schema, set mode="v3_historical"

Write tests: verify None returned when count < 30,
verify correct win_rate calculation with synthetic data.
```

---

### 5-B: Calibration Engine

```
Implement src/calibration/engine.py.

Exact Section 7.1 formulas — no deviation:

  f_star    = (p_win * avg_R - (1 - p_win)) / avg_R
  f_quarter = f_star * 0.25
  f_final   = min(f_quarter, 0.02)

  dd_scalar:
    current_dd < 0.02  → 1.0
    current_dd < 0.05  → 0.5
    current_dd ≥ 0.05  → return None (no trade)

  correlation_scalar:
    same_currency_count ≥ 2 → 0.5
    otherwise              → 1.0

  final_size = f_final × dd_scalar × correlation_scalar

Return None if: no segment data, edge ≤ 0, dd ≥ 5%.
Log reason for every None return.
Run /risk-verify after implementation.
```

---

### 5-C: EWMA Covariance

```
Implement src/risk/covariance.py.

Exact Section 7.4 formulas:

  Σ_t = 0.999 × Σ_{t-1} + 0.001 × (r_t × r_t^T)
  Update on H1 candle close only — never on tick.

  Eigenvalue shrinkage:
    κ = max_eigenvalue / max(min_eigenvalue, 1e-8)
    if κ > 15.0:
      floor = max_eigenvalue / 15.0
      clip all eigenvalues below floor to floor
      reconstruct Σ_reg = U × diag(clipped) × U^T

  Decay multiplier Φ(κ):
    κ ≤ 15.0  → 1.0
    κ ≥ 30.0  → 0.0
    otherwise → exp(-0.5 × (κ - 15.0))

  VaR_99 = 2.326 × sqrt(W^T × Σ_reg × W) × portfolio_value

Run /risk-verify after implementation.
```

---

### 5-D: Kill Switch

```
Implement src/risk/kill_switch.py.

Three levels only: SOFT, HARD, EMERGENCY.

Rules:
- State managed with asyncio.Lock — not plain boolean
- Only escalate — never auto-de-escalate (HARD → SOFT is manual only)
- Persist every state change to Redis AND PostgreSQL immediately
- On startup: read state from PostgreSQL (survives process restart)

SOFT action:  log warning — no new signals allowed
HARD action:  log critical — flatten all open positions via MT5
EMERGENCY:    log critical — disconnect MT5, write full state
              to disk as JSON, fire alert

Manual reset requires operator to pass string:
"I CONFIRM SYSTEM IS SAFE"
Any other string raises PermissionError.

Chaos test requirement: trigger HARD, kill process,
restart, verify system reads HARD from PostgreSQL and
refuses to trade without manual reset.
```

---

### 5-E: State Reconciler

```
Implement src/risk/reconciler.py.

Run on 5-second asyncio heartbeat.

Each cycle:
1. Call mt5.positions_get() — this is ground truth
2. Read open_positions from Redis
3. Diff broker_tickets vs redis_tickets
4. If any mismatch (phantom or ghost positions):
   - log critical with full diff
   - write to reconciliation_log table
   - trigger kill_switch HARD with reason "state_drift"
   - reconcile Redis to match broker (broker always wins)
5. Update Redis open_positions and last_reconcile_ts

On mt5.positions_get() returning None:
- treat as broker disconnect
- trigger kill_switch EMERGENCY

If reconciler loop itself throws exception:
- trigger kill_switch EMERGENCY with reason "reconciler_failure"
- never let the reconciler die silently
```

---

### 5-F: Risk Governor (7 Gates)

```
Implement src/risk/governor.py.

7 sequential gates. Fail-fast — the first failure returns immediately.
Log every gate evaluation with gate number and outcome.

Gate 1 — Kill switch active:
  if not kill_switch.allows_new_trades: REJECT "kill_switch_active"

Gate 2 — Data freshness:
  if snapshot.is_stale: REJECT "stale_data"

Gate 3 — Signal sanity:
  SL must be > 0
  TP must be > 0
  LONG: SL < entry_zone[0] (SL below entry)
  SHORT: SL > entry_zone[1] (SL above entry)
  Violation: REJECT "invalid_signal_geometry"

Gate 4 — Net directional exposure:
  Net USD exposure > 40% of portfolio: REDUCE size by 50%

Gate 5 — Portfolio VaR:
  VaR_99 > 5% of portfolio: REJECT "var_limit_breached"
  VaR_99 > 3% of portfolio: trigger SOFT kill switch, continue

Gate 6 — Covariance condition number:
  Φ(κ) == 0.0: trigger HARD kill switch, REJECT "correlation_crisis"
  Otherwise: multiply final_size by Φ(κ)

Gate 7 — Drawdown state:
  current_dd > 8%: trigger HARD kill switch, REJECT "max_drawdown"
  current_dd > 5%: REDUCE size by 50%

Return RiskDecision with decision, final_size, reason, gate_failed.
```

---

### 5-G: Chaos Tests

```
Write and run all 5 chaos tests in tests/chaos/test_risk_engine.py.

Test 1 — Kill switch survives restart:
  Trigger HARD kill switch.
  Simulate process restart (re-instantiate KillSwitch from DB).
  Verify it reads HARD from PostgreSQL on init.
  Verify evaluate() returns REJECT immediately.

Test 2 — State drift halts trading:
  Put 3 positions in Redis.
  Mock mt5.positions_get() returning only 2 positions.
  Run reconciler cycle.
  Verify HARD kill switch triggered within the cycle.
  Verify Redis updated to match broker (2 positions).

Test 3 — Correlation crisis zeros position:
  Feed EWMA covariance returns that drive κ > 30.
  Verify Φ(κ) returns 0.0.
  Verify risk governor returns REJECT "correlation_crisis".
  Verify HARD kill switch was triggered.

Test 4 — Fail-fast on Gate 1:
  Set kill switch to SOFT (active).
  Call evaluate() with stale snapshot (would fail Gate 2).
  Verify rejection reason is "kill_switch_active" not "stale_data".
  Confirms Gate 1 fires before Gate 2.

Test 5 — Drawdown hard stop:
  Set current_dd = 0.09 in Redis.
  Call evaluate() with valid signal.
  Verify REJECT with reason "max_drawdown".
  Verify HARD kill switch was triggered.
  Verify a second evaluate() call also returns REJECT
  (kill switch blocks even after drawdown condition passes).

All 5 must pass. Tag git as v4.0-phase3 only after all pass.
```

---

## Chapter 6: Phase 4 — Execution & Learning

### 6-A: Execution Gateway

```
Implement src/execution/gateway.py.

Pre-flight checks before every mt5.order_send():
- kill_switch.allows_new_trades must be True
- decision.decision must be "APPROVE"
- decision.final_size > 0
- entry_price != 0, stop_loss != 0, take_profit != 0
- RiskDecision timestamp < 2000ms ago (reject stale approvals)

Order construction:
- volume = round(final_size × portfolio_equity / 100000, 2)
- volume = max(0.01, min(volume, 100.0))
- Use mt5.symbol_info_tick(pair).ask for LONG, .bid for SHORT

On mt5.order_send() response:
- retcode == TRADE_RETCODE_DONE: record fill
- any other retcode: log error with retcode, return None

Paper trading mode (settings.yaml trading_mode: "paper"):
- Skip mt5.order_send() entirely
- Simulate fill at current ask/bid with slippage = 0
- Log: "PAPER TRADE: [pair] [direction] [volume] lots"
- Record identically to live fill
```

---

### 6-B: Fill Tracker & Learning Loop

```
Implement src/execution/fill_tracker.py and src/learning/:

fill_tracker.py:
- record_fill(fill): write to PostgreSQL fills table and Redis
  Include: ticket, requested_price, actual_price,
  slippage_points, volume, strategy, regime, session, timestamp
- record_close(ticket, close_price, close_time):
  Calculate R-multiple = (close - entry) / |entry - stop|
  Negate for SHORT positions
  Return outcome dict for recorder

recorder.py:
- record(outcome): insert into trade_outcomes

updater.py:
- update_segment(strategy, regime, session):
  Recalculate win_rate and avg_R from last 90 days
  Cache result in Redis key "segment:{s}:{r}:{s}", TTL 3600s
  Log if segment count drops below 30

Integration test requirement:
Prove the full feedback cycle works end-to-end:
fill → fill_tracker → recorder → updater → calibration
engine reads updated segment stats on next call.
Show this with a single integration test.

Tag git as v4.0-phase4 after integration test passes.
```

---

## Chapter 7: Phase 5 — Observability

### 7-A: Prometheus Metrics

```
Implement src/observability/metrics.py.

Instrument these exact metrics:

Counters:
  apex_signals_generated_total{strategy, regime, pair}
  apex_trades_executed_total{strategy, regime, direction}
  apex_trades_won_total{strategy, regime}
  apex_gate_rejections_total{gate_number, reason}
  apex_kill_switch_total{level}
  apex_state_drift_total

Gauges:
  apex_portfolio_var_pct
  apex_current_drawdown_pct
  apex_covariance_condition
  apex_open_positions_count
  apex_win_rate_7d

Histograms:
  apex_signal_latency_ms (buckets: 50,100,200,500,1000)
  apex_slippage_points (buckets: 0.1,0.5,1,2,5)
  apex_r_multiple (buckets: -3,-2,-1,0,1,2,3,5)

Expose on port 8000 via prometheus_client HTTP server.
Instrument the pipeline.py critical path for latency.
```

---

### 7-B: systemd Service

```
Create ops/apex_v4.service for production deployment.

Requirements:
- User: apex (create if needed)
- WorkingDirectory: /home/apex/Desktop/apex_v4
- ExecStart: /home/apex/Desktop/apex_v4/venv/bin/python -m src.pipeline
- EnvironmentFile: /home/apex/Desktop/apex_v4/config/secrets.env
- Restart: always
- RestartSec: 10
- After: network.target postgresql.service redis.service
- StandardOutput: journal
- StandardError: journal

Also provide:
- install command
- start command
- status check command
- live log tail command
- graceful stop command
```

---

### 7-C: Paper Trading Validation

```
Run the 7-day paper trading simulation.

Set config/settings.yaml: trading_mode: "paper"
Run pipeline against 7 days of historical EURUSD data.

The simulation must:
- Generate signals through full regime classifier
- Apply the complete risk engine (all 7 gates)
- Simulate fills (paper mode)
- Record every outcome to PostgreSQL
- Generate a pyfolio report at the end

Success criteria (from APEX_V4_STRATEGY.md Section 9):
- 0 crashes
- 0 state drift events
- win rate ≥ 48% over ≥ 50 trades

If win rate < 48%: do NOT proceed to Phase 6.
Report which segments underperformed and your diagnosis.

Tag git as v4.0-phase5 only if all 3 criteria are met.
```

---

## Chapter 8: Phase 6 — Live Migration

### 8-A: V3 Data Migration

```
Implement scripts/migrate_v3_data.py.

Read V3 trade history from ~/Desktop/apex_v3.
Locate the trade log files — do not ask where they are, find them.

Map each V3 trade to trade_outcomes schema:
- strategy: MOMENTUM or MEAN_REVERSION (infer from V3 signal type)
- regime: map from V3 regime labels
- session: calculate from entry timestamp UTC hour
- r_multiple: (exit_price - entry_price) / |entry_price - stop_loss|
- won: r_multiple > 0
- mode: "v3_historical"

After import:
- Print segment breakdown: strategy × regime × session
- Flag every segment with < 30 trades in red
- Print total imported trade count

These flagged segments cannot trade in V4 until the live
sample reaches 30.
```

---

### 8-B: Go-Live Checklist

```
Implement a startup pre-flight validation in src/pipeline.py.

Before the main trading loop starts, check all 10 items:

1. V3 data imported — at least 1 row in trade_outcomes with mode="v3_historical"
2. All 4 trading segments have ≥ 30 outcomes
3. Kill switch state is INACTIVE
4. Redis is reachable and responding
5. PostgreSQL is reachable and all tables exist
6. MT5 connection succeeds — mt5.account_info() returns non-None
7. Paper trading ran for ≥ 7 days (check trade_outcomes timestamps)
8. Zero unresolved state drift events in reconciliation_log
9. settings.yaml has capital_allocation_pct configured
10. secrets.env exists and has MT5 credentials set

For any failed check:
- Print the check name in red
- Print why it failed
- Print exact steps to fix it
- REFUSE to start the pipeline

On all checks passing:
- Print all 10 as green
- Require operator to type: "CONFIRMED [capital_allocation_pct]"
  Example: "CONFIRMED 0.10"
- Any other input aborts startup
```

---

### 8-C: Capital Allocation & Dashboard

```
Two tasks:

TASK 1 — Capital allocation:
Add to config/settings.yaml: capital_allocation_pct: 0.10
Modify CalibrationEngine.calibrate() to multiply final_size
by settings.capital_allocation_pct before returning.
Add a startup log: "Capital allocation: X% of portfolio"

TASK 2 — Grafana dashboard:
Create ops/grafana_dashboard.json with these panels:
- Live PnL (time series)
- Current drawdown % (gauge, red at 5%, critical at 8%)
- Portfolio VaR % (gauge)
- Open positions count (stat)
- Signal rate per hour (time series)
- Win rate 7-day rolling (stat)
- Kill switch status (stat, green=INACTIVE, red=any active)
- Regime distribution today (pie chart)
- Slippage distribution (histogram)

Tag git as v4.0-phase6-ready.
Do NOT change trading_mode to "live" — I do that manually.
```

---

## Chapter 9: Maintenance Prompts

### Weekly Audit

```
Read CLAUDE.md. Session start protocol.

Run /audit on the full APEX V4 codebase.

I need:
1. ADR compliance score per ADR (1-9)
2. Mathematical formula compliance score (Section 7)
3. Test coverage score for: risk engine, execution, calibration
4. Any code contradicting a documented decision

Format: scored table. For anything < 90: exact fix with code.
No vague recommendations.
```

---

### Win Rate Drop Investigation

```
APEX V4 win rate has dropped from [X]% to [Y]%.
Time window: last 14 days.

Investigate without changing any code:
1. Query trade_outcomes: last 14 days vs prior 14 days
2. Break down degradation by strategy, regime, session
3. Check if regime distribution shifted
4. Check if average slippage increased
5. Check if any risk gate rejection rate increased
6. Check if any specific pair is driving the drop

Produce:
- Root cause hypothesis ranked by likelihood
- Supporting data for each hypothesis
- Recommended action for each

Wait for my approval before changing anything.
```

---

### Production Incident Response

```
Kill switch triggered at HARD level.
Triggered at: [timestamp]
Reason logged: [paste reason from kill_switch_events]

Produce an incident report:
1. Full state at time of trigger (positions, VaR, drawdown)
2. Position diff: broker vs Redis at that moment
3. Last successful reconciliation timestamp
4. Process logs in the 60 seconds before trigger
5. Was this a broker disconnect, process crash, or logic trigger?
6. Open positions currently in MT5 requiring manual attention
7. Safe sequence to investigate and restart

Do not restart. Do not close positions. Give me facts to decide.
```

---

### Formula Verification

```
Run /risk-verify on the current codebase.

For every formula in src/risk/ and src/calibration/:
1. Show the formula as implemented (code)
2. Show the formula from APEX_V4_STRATEGY.md Section 7 (spec)
3. MATCH or DEVIATION
4. If DEVIATION: show the correct implementation

I need 100% match before any risk component goes to production.
Confidence score required at the end.
```

---

## Chapter 10: Emergency Prompts

### Context Recovery

```
/compact
```

Then:

```
Re-orient yourself.
Read CLAUDE.md for session protocol.
Run: git log --oneline -10
Run: cat tasks/todo.md
Run: cat tasks/lessons.md

Tell me:
- What phase are we on?
- What was the last completed task?
- What is the next task?
- Are there any incomplete items in todo.md?
```

---

### Hard Reset After Incident

```
The system is in HARD kill switch state.
All positions have been manually closed in MT5.
I have reviewed the incident and confirmed it is safe to resume.

Steps to reset:
1. Verify no open positions in MT5
2. Reset kill switch with confirmation
3. Clear state drift from reconciliation_log
4. Verify Redis open_positions matches MT5 (should be empty)
5. Run a reconciler test cycle
6. Confirm system is ready to trade

Walk me through each step and get my explicit confirmation
before executing the kill switch reset.
```

---

### Segment Insufficient Sample

```
Segment [strategy/regime/session] has only [N] trades.
The minimum is 30. We need to trade this session live.

Options:
1. How many more paper trades do we need in this segment?
2. Can we borrow win rate from a related segment temporarily?
   (e.g., same strategy, different but similar regime)
3. What is the risk of trading at minimum 0.5% size
   while building the sample to 30?

Give me the risk analysis for each option.
I will decide which path to take.
```
