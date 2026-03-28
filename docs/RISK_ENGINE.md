# APEX V4 — Risk Engine

> **Reference:** APEX_V4_STRATEGY.md Sections 4 and 7
> **Mathematical formulas are locked. No deviation without documented justification.**
> **Last Updated:** 2026-03-29

---

## 1. Overview

The risk engine is a sequential pipeline with three major components:

1. **CalibrationEngine** — computes position size via Kelly criterion
2. **RiskGovernor** — 7-gate sequential risk check on every trade intent
3. **KillSwitch** — three-level circuit breaker that can halt trading at any level

These components also interact with the **EWMACovarianceMatrix** for portfolio-level VaR computation.

---

## 2. CalibrationEngine — Position Sizing

**File:** `src/calibration/engine.py`
**Reference:** APEX_V4_STRATEGY.md Section 7.1

### Kelly Criterion (exact Section 7.1 formulas)

```
Step 1: Lookup segment statistics from PostgreSQL
        Segment key = (strategy, regime, session)
        Required: trade_count ≥ 30 (ADR-002)

Step 2: Compute edge
        edge = p_win × avg_R − (1 − p_win)
        If edge ≤ 0 → reject (no positive expectancy)

Step 3: Kelly fraction
        f*        = edge / avg_R
        f_quarter = f* × 0.25       ← quarter-Kelly
        f_final   = min(f_quarter, 0.02)  ← 2% hard cap

Step 4: Drawdown scalar
        current_dd < 0.02  → dd_scalar = 1.0   (full size)
        current_dd < 0.05  → dd_scalar = 0.5   (half size)
        current_dd ≥ 0.05  → dd_scalar = None  (no trade)

Step 5: Correlation scalar
        Count open positions sharing a currency with the new pair
        same_currency_count ≥ 2  → corr_scalar = 0.5
        otherwise                → corr_scalar = 1.0

Step 6: Final size
        final_size = f_final × dd_scalar × corr_scalar × capital_allocation_pct
```

### Parameters

| Parameter | Value | Source |
|---|---|---|
| Kelly fraction | 0.25 | `config/settings.yaml: risk.kelly_fraction` |
| Max position size | 0.02 | `config/settings.yaml: risk.max_position_size` |
| Capital allocation | 0.10 | `config/settings.yaml: risk.capital_allocation_pct` |
| Min segment trades | 30 | `config/settings.yaml: risk.min_trade_sample` |

### Why Quarter-Kelly

Full Kelly maximizes the geometric growth rate but produces extreme volatility in drawdown. Quarter-Kelly sacrifices ~6% of optimal growth for a 4× reduction in drawdown variance. This is the standard institutional choice for systematic strategies.

---

## 3. EWMA Covariance Matrix

**File:** `src/risk/covariance.py`
**Reference:** APEX_V4_STRATEGY.md Sections 7.4, 7.5

### EWMA Update Formula (exact Section 7.4)

```
Σ_t = λ × Σ_{t-1} + (1-λ) × (r_t × r_t^T)

where:
  Σ_t   = covariance matrix at time t
  λ     = 0.999 (decay factor — persistent memory)
  r_t   = H1 log return vector across all pairs
```

**Critical rule:** Updated on H1 candle close only, never on tick. Updating on tick causes O(N²) matrix operations at tick frequency, which crashes the Python pipeline.

### Eigenvalue Shrinkage (Section 7.4)

When the condition number κ exceeds the warning threshold:

```
κ = max(eigenvalues) / max(min(eigenvalues), 1e-8)

If κ > κ_warn (15.0):
    floor = max(eigenvalues) / κ_warn
    eigenvalues = where(eigenvalues < floor, floor, eigenvalues)
    Σ_reg = U @ diag(clipped_eigenvalues) @ U^T
```

Shrinkage prevents numerical instability from ill-conditioned matrices.

### Decay Multiplier Φ(κ) (Section 7.4)

Used by the RiskGovernor (Gate 6) to scale position size by correlation health:

```
κ ≤ 15.0          → Φ(κ) = 1.0          (well-conditioned, no penalty)
15.0 < κ < 30.0   → Φ(κ) = exp(-0.5 × (κ - 15.0))   (exponential decay)
κ ≥ 30.0          → Φ(κ) = 0.0          (correlation crisis — block trade)
```

If Φ(κ) = 0, Gate 6 triggers a HARD kill switch and rejects the signal.

### Portfolio VaR (Section 7.5)

```
σ²_portfolio = W^T × Σ_reg × W

where:
  W = position weight vector (pair → size fraction)

VaR_99 = 2.326 × sqrt(σ²_portfolio) × portfolio_value

2.326 = z-score for 99th percentile (one-tailed normal)
```

---

## 4. 7-Gate Risk Governor

**File:** `src/risk/governor.py`
**Reference:** APEX_V4_STRATEGY.md Section 4

Gates are evaluated sequentially. The first failing gate immediately returns REJECT or REDUCE — later gates are not evaluated. This fail-fast behavior is intentional.

### Gate 1: Kill Switch

```
Condition: kill_switch.allows_new_signals() is False
Action:    REJECT
Reason:    kill_switch_active
Risk state: HARD_STOP
```

### Gate 2: Data Freshness

```
Condition: snapshot.is_stale is True
           (timestamp > 5000ms behind wall clock)
Action:    REJECT
Reason:    stale_data
Risk state: NORMAL
```

### Gate 3: Signal Sanity

```
Condition (any of):
  SL ≤ 0 or TP ≤ 0
  LONG direction: SL ≥ entry_low
  SHORT direction: SL ≤ entry_high

Action:    REJECT
Reason:    invalid_signal_geometry
Risk state: NORMAL
```

Prevents trades with geometrically invalid SL/TP.

### Gate 4: Net Directional Exposure

```
Threshold: 40% of positions in same USD direction

Metric: count_based net USD exposure
  - USD as base: LONG = long USD, SHORT = short USD
  - USD as quote: LONG = short USD (buying EUR), SHORT = long USD

Condition: net_exposure > 0.40
Action:    REDUCE (size × 0.50)
Risk state: THROTTLE
```

Does not reject — reduces size by 50% to limit correlated exposure.

### Gate 5: Portfolio VaR

```
Condition 1: VaR_99 / portfolio_value > 0.05 (5%)
Action:      REJECT
Reason:      var_limit_breached
Risk state:  HARD_STOP

Condition 2: VaR_99 / portfolio_value > 0.03 (3%)
Action:      SOFT kill switch + continue (allow this trade)
Risk state:  THROTTLE
```

VaR uses the EWMA covariance matrix with the proposed new position included.

### Gate 6: Covariance Condition Number

```
Compute: Φ(κ) — decay multiplier

Condition: Φ(κ) = 0.0 (κ ≥ 30.0)
Action:    HARD kill switch + REJECT
Reason:    correlation_crisis

Otherwise:
Action:    size = size × Φ(κ)
           (reduces size as correlations become unstable)
```

### Gate 7: Drawdown State

```
Condition 1: current_dd > 0.08 (8%)
Action:      HARD kill switch + REJECT
Reason:      max_drawdown
Risk state:  HARD_STOP

Condition 2: current_dd > 0.05 (5%)
Action:      REDUCE (size × 0.50)
Risk state:  THROTTLE
```

Note: CalibrationEngine already blocks at DD ≥ 5% (returns None). Gate 7 is an independent double-check on the risk governor side.

### Gate Summary

| Gate | Condition | Action | Risk State |
|---|---|---|---|
| 1 | Kill switch active | REJECT | HARD_STOP |
| 2 | Stale snapshot (> 5s) | REJECT | NORMAL |
| 3 | Invalid SL/TP geometry | REJECT | NORMAL |
| 4 | Net USD exposure > 40% | REDUCE 50% | THROTTLE |
| 5a | Portfolio VaR > 5% | REJECT | HARD_STOP |
| 5b | Portfolio VaR > 3% | SOFT kill + continue | THROTTLE |
| 6a | Covariance Φ(κ) = 0 | HARD kill + REJECT | HARD_STOP |
| 6b | Covariance Φ(κ) < 1 | Scale size by Φ(κ) | THROTTLE |
| 7a | Drawdown > 8% | HARD kill + REJECT | HARD_STOP |
| 7b | Drawdown > 5% | REDUCE 50% | THROTTLE |

---

## 5. Kill Switch Hierarchy

**File:** `src/risk/kill_switch.py`
**Reference:** APEX_V4_STRATEGY.md Section 4, ADR-005

### Three Levels

```
NONE → SOFT → HARD → EMERGENCY
```

Levels only escalate — never auto-de-escalate. HARD → SOFT transition is forbidden. Only `manual_reset()` can return to NONE.

### Kill Level Behavior

| Level | Effect | Triggered By |
|---|---|---|
| SOFT | No new signals allowed | VaR > 3%, manual |
| HARD | SOFT + flatten all open positions | DD > 8%, correlation crisis, reconciliation mismatch, manual |
| EMERGENCY | HARD + disconnect MT5 + dump state to disk + fire alert callback | MT5 disconnect, unhandled exception, manual |

### State Persistence

Every state change is persisted to both Redis and PostgreSQL immediately (dual write). On process restart, kill switch state is recovered from PostgreSQL — Redis is not trusted for state recovery.

```
PostgreSQL: kill_switch_events table (full audit trail, timestamp, reason)
Redis:      kill_switch key (fast read for gates, no TTL)
```

### Thread Safety

State is protected by `asyncio.Lock`. Concurrent `trigger()` calls are serialized. The lock prevents race conditions that existed in V3 (V3 bug #8).

### Manual Reset

```python
# Confirmation string must be exactly this — no exceptions
await kill_switch.manual_reset(
    confirmation="I CONFIRM SYSTEM IS SAFE",
    operator="your_name"
)
```

The operator string is written to the audit trail.

### NSSM Exit Codes

| Exit Code | Meaning | NSSM Action |
|---|---|---|
| 0 | Clean shutdown | Stay DOWN |
| 42 | SOFT or HARD kill switch | Stay DOWN |
| 43 | EMERGENCY kill switch | Stay DOWN |
| Any other | Crash or error | Restart after 10s |

---

## 6. State Reconciler

**File:** `src/risk/reconciler.py`
**Reference:** APEX_V4_STRATEGY.md ADR-004

The reconciler runs in parallel with the main pipeline loop on a 5-second heartbeat.

**Operation:**
1. Read `open_positions` from Redis
2. Read open positions from MT5 broker via `positions_get()`
3. Compare: if any position exists in one but not the other → mismatch
4. On mismatch: trigger HARD kill switch, write to `reconciliation_log`
5. Write reconciliation record to PostgreSQL regardless (full audit trail)

**Design principle:** In any state conflict between Redis and MT5, MT5 wins. The broker is always the source of truth for actual positions.

---

## 7. Formula Verification

All mathematical formulas are locked in `APEX_V4_STRATEGY.md` Section 7. Before any release:

1. Run `/risk-verify` — extracts formulas from implementation and cross-references against Section 7
2. Achieve 100% match — no rounding, no approximation without documented justification in a code comment
3. Result from last session (2026-03-28): **5/5 formulas VERIFIED, 0 deviations**

### Verified Formulas

| Formula | Expected | Verified |
|---|---|---|
| Kelly: f* | `edge / avg_R` | PASS |
| Quarter-Kelly cap | `min(f* × 0.25, 0.02)` | PASS |
| EWMA update | `λΣ_{t-1} + (1-λ)r_t r_t^T, λ=0.999` | PASS |
| Decay multiplier Φ(κ) | `exp(-0.5(κ-15))` in (15,30), else 0/1 | PASS |
| Portfolio VaR | `2.326 × sqrt(W^T Σ_reg W) × V` | PASS |
