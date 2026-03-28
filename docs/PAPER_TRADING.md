# APEX V4 — Paper Trading Guide

> **Purpose:** Validate strategy performance before risking real capital
> **Last Updated:** 2026-03-29

---

## 1. How Paper Trading Works

In paper mode (`system.mode: paper`), APEX V4 runs the full pipeline but simulates order execution:

- MT5 data is **live** (real market prices, spreads, sessions)
- Feature computation is **real** (TA-Lib indicators on live candles)
- Regime classification is **real** (ADX rules on live data)
- Alpha hypotheses are **real** (momentum and mean reversion signals)
- Risk governor is **real** (7 gates, Kelly sizing)
- Order execution is **simulated** (no actual orders sent to MT5)
- Fill prices are **estimated** (mid-price at signal time + spread)
- Trade outcomes are **recorded** to PostgreSQL (real learning loop)

The system tracks a `paper_positions` dictionary in memory. When a simulated position closes, the outcome is written to `trade_outcomes` exactly as a real trade would be.

---

## 2. Pre-flight in Paper Mode

Paper mode has 9 pre-flight checks. Checks 8 and 9 are warnings (not blocking) in paper mode:

| Check | Paper Mode | Live Mode |
|---|---|---|
| 1. PostgreSQL connection | BLOCK if fail | BLOCK if fail |
| 2. Redis connection | BLOCK if fail | BLOCK if fail |
| 3. MT5 connection | BLOCK if fail | BLOCK if fail |
| 4. Kill switch state = NONE | BLOCK if fail | BLOCK if fail |
| 5. Schema integrity | BLOCK if fail | BLOCK if fail |
| 6. VaR computation | BLOCK if fail | BLOCK if fail |
| 7. Config validation | BLOCK if fail | BLOCK if fail |
| 8. V3 data imported | **WARN** (yellow) | BLOCK if fail |
| 9. Segment counts ≥ 30 | **WARN** (yellow) | BLOCK if fail |

If you see yellow warnings for checks 8 or 9, paper trading will still start. Segments without 30+ trades will produce `calibration_rejected: no segment data` warnings and no signals for those combinations.

---

## 3. Starting Paper Trading

```powershell
# Ensure mode is paper in config/settings.yaml
nssm start APEX_V4

# Watch logs
Get-Content C:\apex_v4\logs\apex_stdout.log -Tail 50 -Wait
```

Expected startup sequence:
```
preflight_check_1: postgres ... pass
preflight_check_2: redis ... pass
...
preflight_complete: score=97.0
MarketFeed started: pairs=['EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD']
pipeline_started: mode=paper
```

---

## 4. What to Monitor

### Daily Checks

```powershell
# Signals generated today
psql -U apex -d apex_v4 -c "
SELECT strategy, regime, session, COUNT(*) as signals,
       AVG(r_multiple) as avg_r,
       SUM(CASE WHEN won THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*), 0) as win_rate
FROM trade_outcomes
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY strategy, regime, session;"

# Kill switch activity
psql -U apex -d apex_v4 -c "
SELECT level, reason, created_at
FROM kill_switch_events
WHERE created_at > NOW() - INTERVAL '24 hours'
ORDER BY created_at DESC;"

# Rejection breakdown
# (from Prometheus via Grafana Gate Rejections panel)
```

### Weekly Checks

```powershell
# Cumulative segment performance
psql -U apex -d apex_v4 -c "
SELECT strategy, regime, session,
       COUNT(*) as trade_count,
       AVG(r_multiple) as avg_r,
       SUM(CASE WHEN won THEN 1 ELSE 0 END)::float / COUNT(*) as win_rate,
       AVG(r_multiple) * SUM(CASE WHEN won THEN 1 ELSE 0 END)::float / COUNT(*) -
       (1 - SUM(CASE WHEN won THEN 1 ELSE 0 END)::float / COUNT(*)) as edge
FROM trade_outcomes
GROUP BY strategy, regime, session
HAVING COUNT(*) >= 10
ORDER BY edge DESC;"
```

---

## 5. Performance Metrics Explained

### R-Multiple

The R-multiple is the key performance metric:

```
r_multiple = (exit_price - entry_price) / |entry_price - stop_loss_price|

Positive: winner (how many R did we earn?)
Negative: loser (how many R did we lose?)
```

A trade stopped out at the stop loss returns r_multiple = -1.0 (lost exactly 1R).
A trade hitting take profit at 4× ATR returns r_multiple ≈ +2.67 (the ATR ratios).

### Edge

```
edge = p_win × avg_R − (1 − p_win)

Positive: profitable segment
Negative: losing segment
Zero: breakeven
```

Edge is what CalibrationEngine uses to compute Kelly fraction. A segment with edge ≤ 0 produces no signals.

### Expectancy per Trade

```
expectancy = edge × avg_R

This is the average expected R-multiple per trade.
```

### Win Rate vs Average R

Neither metric alone is sufficient:
- High win rate + low avg_R = breakeven at best
- Low win rate + high avg_R = can be profitable (trend-following characteristic)

Focus on **edge** (the product) rather than win rate in isolation.

---

## 6. Go/No-Go Criteria for Live Trading

All of the following must be satisfied before switching from paper to live:

### Quantitative Criteria

| Criterion | Threshold | Why |
|---|---|---|
| Minimum segment trades | ≥ 30 per active segment | Statistical validity floor (ADR-002) |
| Portfolio edge (weighted avg) | > 0.05 | Minimum positive expectancy |
| Maximum drawdown (paper) | < 15% | Stress test headroom |
| Win rate (any segment) | > 30% | Not pathologically low |
| Average R (per trade) | > 1.0 | Reward justifies risk |
| Kill switch triggers (HARD/EMERGENCY) | 0 (or explained) | No structural risk failures |

### Operational Criteria

| Criterion | Check |
|---|---|
| Full deployment checklist complete | ops/DEPLOYMENT_CHECKLIST.md — all 50 items |
| Pre-flight passes all 9 (no warnings) | `--preflight-only` |
| 701 tests passing | `pytest tests/ -v` |
| `/risk-verify` passes | 5/5 formulas verified |
| `/audit` passes | 100/100 architecture compliance |
| Backup procedure tested | Restore from backup confirmed |
| Kill switch reset tested | Manual reset procedure confirmed working |
| Monitoring alerts tested | PipelineDown dead-man alert fires and clears |

### Duration Criteria

- Minimum paper trading duration: **4 weeks** of market-hours operation
- Must cover at least: 2 trending weeks, 1 ranging week, 1 volatile/news week
- Minimum: 200 total trades across all segments

---

## 7. Bootstrapping from V3 Data

APEX V4 requires segment history (trade outcomes per strategy × regime × session combination) before it can size trades. Without history, every `calibrate()` call returns None.

### Migration Script

```powershell
python scripts/migrate_v3_data.py
```

This script reads V3 historical trade outcomes and imports them into the V4 `trade_outcomes` table, correctly mapping V3 regime/strategy labels to V4 enums.

### Verifying Migration

```powershell
psql -U apex -d apex_v4 -c "
SELECT strategy, regime, session, COUNT(*) as count
FROM trade_outcomes
GROUP BY strategy, regime, session
ORDER BY count ASC;"
```

Any segment with count < 30 will show `calibration_rejected: no segment data` warnings until paper trading accumulates enough outcomes.

### Active Segments

APEX V4 has 24 active segments:
- 2 strategies × 3 regimes × 4 sessions
  - Strategies: MOMENTUM, MEAN_REVERSION
  - Regimes: TRENDING_UP, TRENDING_DOWN, RANGING
  - Sessions: LONDON, NY, OVERLAP, ASIA

TRENDING_UP and TRENDING_DOWN only apply to MOMENTUM. RANGING only applies to MEAN_REVERSION. In practice, segments are:
- MOMENTUM: TRENDING_UP × 4 sessions = 4 segments
- MOMENTUM: TRENDING_DOWN × 4 sessions = 4 segments
- MEAN_REVERSION: RANGING × 4 sessions = 4 segments
- Total: 12 active segments (24 was the theoretical maximum)

---

## 8. Common Paper Trading Observations

### "No signals for days"

**Not a bug.** APEX only trades when:
1. Regime is TRENDING or RANGING (not UNDEFINED)
2. Signal passes all alpha engine gates (multi-TF EMA, ADF, conviction)
3. CalibrationEngine has segment data (30+ trades)
4. All 7 risk gates pass

Markets spend significant time in the ADX dead zone (22–31). No signals in dead zones is correct behavior.

### "calibration_rejected: edge <= 0"

**Expected during early paper trading.** Until the segment accumulates real performance data, p_win comes from V3 historical data which may show edge ≤ 0 for some segments. This is a valid rejection — the system correctly declines to trade negative-expectancy setups.

### Consistent gate 2 rejections (stale data)

**Indicates MT5 connectivity issues.** The feed is producing snapshots with timestamps more than 5 seconds old. Check:
1. MT5 terminal is connected to broker (no "No connection" banner)
2. VPS internet connection is stable
3. ZMQ socket is functioning: `redis-cli GET kill_switch` should work

### SOFT kill switch triggered repeatedly

**Normal if VaR is genuinely elevated.** Consecutive triggers suggest correlated open positions or high market volatility. Review open position pairs for USD concentration.
