# APEX V4 — Trading Strategy

> **Reference:** APEX_V4_STRATEGY.md Sections 2–4
> **Last Updated:** 2026-03-29

---

## 1. Trading Philosophy

APEX V4 is a **regime-gated hybrid strategy**. The core insight is that no single trading strategy works in all market conditions. The system first classifies the current market regime, then activates the appropriate alpha engine for that regime.

**Principles:**

1. **No ML for position sizing.** Kelly inputs come from historical trade outcomes only. LLM softmax is not a posterior probability.
2. **No ML for regime classification.** Hard ADX rules are deterministic, auditable, and cannot drift.
3. **Minimum sample gate.** A strategy does not trade a regime+session segment until it has at least 30 historical outcomes. There is no edge without evidence.
4. **AI = research only.** Never allowed to influence position sizing or regime classification.
5. **Quarter-Kelly, always.** Full Kelly is mathematically correct but practically ruinous. Sizing is capped at 2% of allocated capital regardless.

---

## 2. Instruments and Sessions

**Pairs traded:** EURUSD, GBPUSD, USDJPY, AUDUSD

**Trading sessions** (UTC):

| Session | Hours (UTC) | Characteristics |
|---|---|---|
| LONDON | 07:00–12:00 | High liquidity, EUR/GBP pairs active |
| OVERLAP | 12:00–16:00 | London + NY both open, highest volume |
| NY | 16:00–21:00 | USD pairs active, news risk |
| ASIA | 21:00–07:00 | Lower liquidity, JPY pairs active |

Session is recorded with every trade outcome. The segment key is `(strategy, regime, session)` — each combination has its own Kelly parameters.

---

## 3. Regime Classification

Regime is determined by two ADX rules applied sequentially. Rules are evaluated in strict order — the first matching rule wins.

**ADX thresholds** (calibrated via Phase 2 backtest on synthetic H1 data):
- Trend threshold: **31** (originally 25, adjusted P2.8)
- Range threshold: **22** (originally 20, adjusted P2.8)

Note: These thresholds are scheduled for re-verification against real H1 data in Phase 5 paper trading.

### Rule Evaluation Order

```
Rule 1: news_blackout is True           → UNDEFINED  (no trade)
Rule 2: spread_ok is False              → UNDEFINED  (spread too wide)
Rule 3: ADX-14 > 31 AND close > EMA-200 → TRENDING_UP
Rule 4: ADX-14 > 31 AND close < EMA-200 → TRENDING_DOWN
Rule 5: ADX-14 < 22                     → RANGING
Rule 6: ADX-14 in 22–31 (dead zone)    → UNDEFINED  (no trade)
```

**Rationale:** The dead zone (ADX 22–31) produces ambiguous signals. Neither momentum nor mean reversion edge is reliable here. Skipping it reduces false signals.

### Regime → Strategy Routing

| Regime | Strategy Activated |
|---|---|
| TRENDING_UP | MomentumEngine |
| TRENDING_DOWN | MomentumEngine |
| RANGING | MeanReversionEngine |
| UNDEFINED | Neither (no trade) |

---

## 4. Momentum Engine

**File:** `src/alpha/momentum.py`
**Activated on:** TRENDING_UP or TRENDING_DOWN

### Signal Logic

1. **Direction** from regime: TRENDING_UP → LONG, TRENDING_DOWN → SHORT
2. **Multi-TF confirmation:**
   - H4 EMA-20 must agree with direction
   - H1 EMA-20 must agree with direction
   - If either disagrees → no signal
3. **Entry zone:** M15 EMA-20 ± (0.2 × ATR-14)
4. **Stop loss:** entry ± (1.5 × ATR-14) against direction
5. **Take profit:** entry ± (4.0 × ATR-14) in direction
6. **Minimum R:R:** 1.8 — hypothesis rejected if expected_R < 1.8

### Setup Score (0–30)

Scoring adjusts conviction, not position size (size comes from Kelly):

| Condition | Points |
|---|---|
| H4 EMA-20 confirms direction | +10 |
| ADX-14 > 30 (strong trend) | +10 |
| OVERLAP or LONDON session | +5 |
| Spread ≤ 1 pip (0.00010) | +5 |

### ATR Multipliers

```
Entry zone:    M15 EMA-20 ± 0.2 × ATR-14   (tight entry window)
Stop loss:     1.5 × ATR-14                 (tight stop)
Take profit:   4.0 × ATR-14                 (4:1 reward at TP entry)
Expected R:R:  4.0 / 1.5 = 2.67            (well above 1.8 threshold)
```

---

## 5. Mean Reversion Engine

**File:** `src/alpha/mean_reversion.py`
**Activated on:** RANGING

This engine runs a statistical pipeline before generating any hypothesis. All gates must pass.

### Pipeline

```
Step 1: ADF gate — Augmented Dickey-Fuller test (p < 0.05)
        If series is non-stationary → no signal

Step 2: Kalman filter — smooth H1 price series
        Reduces noise before OU calibration

Step 3: OU MLE — fit Ornstein-Uhlenbeck parameters (θ, μ, σ)
        Calibrate mean-reversion speed and equilibrium

Step 4: Half-life check — half_life ≤ 48 H1 candles
        Fast enough mean reversion to be tradeable

Step 5: Z-score computation — z = (price - μ) / σ
        |z| < 3.0 guard (3σ guard prevents chasing extremes)

Step 6: Conviction — C = f(half_life, z-score, p-value)
        Must be ≥ 0.65 to emit hypothesis

Step 7: Entry, SL, TP computation
        Stop loss: 1.5 × ATR-14
        Take profit: based on expected return to mean
        Minimum R:R: 1.8
```

### Key Thresholds

| Parameter | Value | Source |
|---|---|---|
| ADF p-value | < 0.05 | config/settings.yaml: `alpha.adf_pvalue_threshold` |
| OU half-life | ≤ 48 H1 candles | `alpha.ou_max_halflife_h1` |
| Conviction | ≥ 0.65 | `alpha.conviction_threshold` |
| Z-score guard | < 3.0 | `alpha.zscore_guard` |
| Minimum R:R | ≥ 1.8 | `alpha.min_rr_ratio` |
| Min H1 candles | 200 | Strategy spec |

### Ornstein–Uhlenbeck Model

The OU process models mean-reverting price dynamics:

```
dX_t = θ(μ - X_t)dt + σ dW_t

where:
  θ = mean-reversion speed (fitted via MLE)
  μ = long-run mean (fitted via MLE)
  σ = volatility (fitted via MLE)

Half-life = ln(2) / θ   (time to revert halfway to mean)
```

The Kalman smoother is applied before OU fitting to remove tick noise from the H1 series.

---

## 6. Signal Flow Summary

```
MarketSnapshot
      │
      ▼
FeatureVector
  atr_14, adx_14, ema_200
  bb_upper, bb_lower, bb_mid
  spread_ok, news_blackout
      │
      ▼
RegimeClassifier
  → TRENDING_UP / TRENDING_DOWN / RANGING / UNDEFINED
      │
      ├─ TRENDING ──► MomentumEngine
      │                  multi-TF EMA check
      │                  entry/SL/TP via ATR
      │                  → AlphaHypothesis
      │
      ├─ RANGING ───► MeanReversionEngine
      │                  ADF gate
      │                  Kalman + OU fit
      │                  conviction gate
      │                  → AlphaHypothesis
      │
      └─ UNDEFINED → no signal
            │
            ▼
      AlphaHypothesis (or None)
        strategy, pair, direction
        entry_zone, stop_loss, take_profit
        setup_score, expected_R, regime
        conviction (MR only)
            │
            ▼
      CalibrationEngine
        segment lookup (strategy × regime × session)
        Kelly sizing
        → CalibratedTradeIntent (or None)
            │
            ▼
      RiskGovernor (7 gates)
        → RiskDecision: APPROVE | REJECT | REDUCE
            │
            ▼
      ExecutionGateway
        paper: simulate fill
        live: MT5 order_send()
```

---

## 7. Risk Management Overview

See `docs/RISK_ENGINE.md` for the full mathematical specification.

Key constraints:

| Parameter | Value | Effect |
|---|---|---|
| Capital allocated | 10% | Only 10% of account equity is at risk |
| Max position size | 2% | Kelly output is capped at 2% of allocated capital |
| Kelly fraction | 0.25 | Quarter-Kelly — standard conservative sizing |
| VaR hard limit | 5% | Portfolio VaR > 5% → REJECT signal |
| VaR soft limit | 3% | Portfolio VaR > 3% → SOFT kill switch |
| Max drawdown hard | 8% | DD > 8% → HARD kill switch + REJECT |
| Max drawdown reduce | 5% | DD > 5% → reduce position by 50% |
| Drawdown no-trade | 5% | CalibrationEngine refuses to size at DD ≥ 5% |
| Min segment trades | 30 | No live signals without 30 historical outcomes |

---

## 8. What This System Does Not Do

Per `APEX_V4_STRATEGY.md` Section 10:

- **No sentiment analysis** — news event data is limited to blackout windows, not prediction
- **No ML regime detection** — ADX rules only
- **No AI position sizing** — Kelly inputs from trade outcomes, never from models
- **No high-frequency trading** — M5 is the shortest trigger timeframe
- **No options, futures, or crypto** — Forex (MT5) only
- **No multi-account management** — single account, single pipeline
- **No intraday scalping** — minimum R:R of 1.8 requires meaningful price moves
