# APEX V4 — Agentic Workflow Orchestration Guide

> You do not write code. You define goals and enforce quality.
> Claude executes. This is how serious engineering teams operate.

---

## Mental Model

```
WRONG: "Open src/risk/governor.py and add a check on line 47"
RIGHT: "The VaR gate is missing from the risk engine.
        Implement it per Section 7.5 of APEX_V4_STRATEGY.md."
```

You specify WHAT. Claude decides HOW.

---

## Part 1: One-Time Mac Setup

### 1.1 Environment Bootstrap

```
I need to set up a professional Python trading system development
environment on macOS from scratch.

Requirements:
- Python 3.11 via pyenv (not system Python)
- Node.js 20 LTS via nvm
- Homebrew package manager
- TA-Lib C library (required before pip install TA-Lib)
- Git configured with my identity

Assess what is already installed, install what is missing,
and verify everything works. Operate autonomously — do not
ask for confirmation between steps.
```

### 1.2 Claude Code CLI

```
Install Claude Code CLI globally via npm and authenticate it.
Verify the installation with a simple test prompt.
Show me the installed version and confirm authentication succeeded.
```

### 1.3 Project Scaffold

```
Create the APEX V4 project at ~/Desktop/apex_v4.

Read APEX_V4_STRATEGY.md Appendix for the exact folder structure.
Create every folder and placeholder file listed there.

Then:
- Create a Python 3.11 virtual environment
- Install all libraries from requirements.txt
  (MetaTrader5, numpy, scipy, pandas, TA-Lib, statsmodels,
   filterpy, redis, psycopg2-binary, pyzmq, msgpack, pydantic,
   pytest, pytest-asyncio, prometheus-client, structlog,
   python-dotenv, alembic, backtrader, pyfolio-reloaded, empyrical)
- Initialize git and make the first commit: "chore: apex v4 scaffold"
- Verify TA-Lib imports correctly (common failure point on macOS)

Confirm everything is working before finishing.
- Verify tasks/todo.md and tasks/lessons.md exist — Claude uses
  these for planning and self-improvement per CLAUDE.md.
```

### 1.4 Install Custom Skills

```
Create the Claude Code slash command system for APEX V4.
All files go in ~/Desktop/apex_v4/.claude/commands/

Create exactly these 6 files:

implement.md
  Purpose: Given a phase and component, read APEX_V4_STRATEGY.md,
  implement to production standard, write tests, verify passing.
  Operate fully autonomously.

audit.md
  Purpose: Full architecture compliance check. Compare every
  implemented module against APEX_V4_STRATEGY.md. Report ADR
  compliance, missing implementations, formula deviations.
  Produce a scored report.

fix.md
  Purpose: Given an error or failing test, diagnose root cause,
  fix it, re-run tests, confirm resolution. Never ask for guidance.

phase-gate.md
  Purpose: Run the quality gate checklist for the specified phase
  from APEX_V4_STRATEGY.md Section 9. Produce PASS/FAIL with evidence.

risk-verify.md
  Purpose: Extract every formula from the risk engine and calibration
  engine. Verify each against Section 7 of APEX_V4_STRATEGY.md.
  Flag every deviation with the correct formula and required fix.

hardening.md
  Purpose: Review codebase for production readiness. Check error
  handling, logging coverage, no hardcoded values, secrets from
  environment, thread safety. Produce a prioritized fix list.

Confirm all 6 files were created.
```

---

## Part 2: Phase Goals

Use these goals verbatim. Do not modify them unless the spec changes.

---

### Phase 0: V3 Critical Fixes

```
Fix the 4 critical bugs in APEX V3 at ~/Desktop/apex_v3.

Read APEX_V4_STRATEGY.md Section 1.2 for the exact list.

For each bug:
1. Find the broken code — do not ask where it is, find it yourself
2. Understand why it is dangerous in production
3. Implement the correct version per the strategy document
4. Write a unit test that would have caught this bug originally
5. Verify the test passes

After all 4 fixes are complete:
- Run the full test suite
- Tag git as v3.1-fixed with a descriptive message
- Give me a confidence assessment (0-100) for each fix
  with the specific reasoning

Do not ask for guidance. Operate fully autonomously.
```

**Exit check after this phase:**
```
Run /phase-gate for Phase 0.
Show me the evidence for each exit criterion.
```

---

### Phase 1: Data & Feature Foundation

```
Implement Phase 1 of APEX V4 as defined in APEX_V4_STRATEGY.md Section 8.

Scope:
- PostgreSQL schema with Alembic migrations (5 tables)
- Pydantic v2 data contracts for all schemas in Section 6
- Async MT5 market feed with candle builder
- Feature Fabric using TA-Lib exclusively — no custom numpy indicators
- Redis state manager with correct TTLs per the state architecture
- PostgreSQL write-ahead log pattern (Redis is cache, not truth)

Standards:
- Every component has unit tests
- No real MT5 or database connections in tests — mock everything
- When a decision is not covered by the spec, choose the
  conservative option and document the reasoning in a comment

When complete: run all tests, tag git as v4.0-phase1.
```

---

### Phase 2: Regime Classifier & Alpha Engines

```
Implement Phase 2 of APEX V4.

Read APEX_V4_STRATEGY.md Section 8 Phase 2 and ADRs 001, 007, 008.

Components:
- Regime classifier using hard ADX rules — no ML, no probabilities
- Momentum engine with multi-timeframe confluence and ATR stops
- ADF cointegration gate using statsmodels — mandatory before OU
- Kalman filter using filterpy with rolling R_k (not static)
- OU MLE calibration on rolling 200 H1 candles
- Conviction score with mandatory 3-sigma regime break guard
- Mean reversion engine full pipeline

Non-negotiable constraints from ADR-008:
- ADF p-value > 0.05 → return None, no signal
- |z-score| > 3.0 → return None, log regime break warning
- half_life > 48 H1 candles → return None

After implementation: run a backtrader backtest on 6 months
of historical EURUSD H1 data. Expected regime distribution:
25-35% trending, 35-45% ranging. If outside these ranges,
adjust the ADX thresholds, document why, and re-run.

Tag git as v4.0-phase2 when complete.
```

---

### Phase 3: Risk Engine

```
Implement Phase 3 of APEX V4: the risk and safety infrastructure.

Read APEX_V4_STRATEGY.md Sections 7, 8 Phase 3, and ADRs 003, 004, 005.

This is the most critical phase. The standard is higher here.

Components:
- Performance database: 90-day rolling segment stats in PostgreSQL
- Calibration engine: exact Section 7.1 formulas, quarter-Kelly,
  all three scalars (drawdown, correlation, regime decay)
- EWMA covariance: eigenvalue shrinkage, update on H1 candle close
  (not tick), λ = 0.999
- Kill switch: 3 levels, asyncio.Lock, persisted to Redis AND
  PostgreSQL, survives process restart
- State reconciler: 5-second heartbeat, broker is always truth,
  any drift triggers HARD kill switch immediately
- Risk governor: 7 gates, sequential, fail-fast, every gate logged

Mathematical correctness:
After implementation, run /risk-verify.
Every formula must match Section 7 exactly.
Fix all deviations before proceeding.

Chaos tests — all 5 must pass before this phase is complete:
1. Kill switch state survives process kill and restart
2. State drift detection halts trading within 5 seconds
3. Correlation crisis (κ ≥ 30) drives position size to zero
4. Cascade: Gate 1 fires before Gate 2 (fail-fast confirmed)
5. Drawdown ≥ 8% triggers HARD kill switch, blocks new trades

Do not mark this phase complete until all 5 chaos tests pass.

Tag git as v4.0-phase3.
```

---

### Phase 4: Execution & Learning Loop

```
Implement Phase 4 of APEX V4.

Read APEX_V4_STRATEGY.md Section 8 Phase 4.

Components:
- Execution gateway with pre-flight validation on every order
- Fill tracker: slippage = |actual_price - requested_price| / point_size
- Trade outcome recorder: R-multiple, strategy, regime, session
- Kelly input updater: segment-level only, rolling 90 days
- pyfolio performance reporting integration
- Paper trading mode: settings.yaml flag, simulates fills,
  records identically to live trades

Critical requirement — the feedback loop must close:
Write an integration test that proves this full cycle works:
trade fill → outcome recorder → segment stats → calibration
engine reads updated stats on next signal.

Tag git as v4.0-phase4.
```

---

### Phase 5: Observability & Paper Trading

```
Implement Phase 5 of APEX V4.

Read APEX_V4_STRATEGY.md Section 8 Phase 5.

Prometheus metrics — instrument every critical path:
- apex_signals_generated_total (labels: strategy, regime, pair)
- apex_trades_executed_total (labels: strategy, regime, direction)
- apex_gate_rejections_total (labels: gate_number, reason)
- apex_kill_switch_total (labels: level)
- apex_portfolio_var_pct (gauge)
- apex_current_drawdown_pct (gauge)
- apex_covariance_condition (gauge)
- apex_signal_latency_ms (histogram)
- apex_slippage_points (histogram)
- apex_r_multiple (histogram)

systemd service at ops/apex_v4.service:
- dedicated user, auto-restart with 10-second delay
- loads secrets from config/secrets.env
- starts after postgresql.service and redis.service

Paper trading validation:
Run a 7-day simulation using historical data.
Requirements: 0 crashes, 0 state drift events, win rate ≥ 48%.
If win rate < 48%, do not proceed — report which segments
are underperforming and why.

Tag git as v4.0-phase5.
```

---

### Phase 6: Live Migration

```
Prepare APEX V4 for live trading. Do not enable live trading —
only prepare the infrastructure. I will flip the switch manually.

Read APEX_V4_STRATEGY.md Section 8 Phase 6.

Step 1 — Seed performance database:
Import V3 historical trade data from ~/Desktop/apex_v3.
Map to trade_outcomes schema. Print segment summary.
Flag every segment with fewer than 30 trades — these
segments cannot trade until the sample is sufficient.

Step 2 — Go-live startup validation:
Implement a pre-flight check script that validates all 10
go-live criteria before the pipeline is allowed to start.
If any check fails: refuse to start, print what failed,
print how to fix it.

Step 3 — Capital allocation control:
Add capital_allocation_pct: 0.10 to settings.yaml.
All final position sizes multiply by this factor.
Add a startup confirmation prompt: operator must type
the exact allocation percentage before the system starts.

Step 4 — Grafana dashboard:
Create ops/grafana_dashboard.json with panels for:
live PnL, drawdown, VaR, open positions, signal rate,
win rate (7d rolling), kill switch status.

Tag git as v4.0-phase6-ready.
```

---

## Part 3: Ongoing Operations

### Weekly Architecture Audit

```
Run a full architectural audit of APEX V4.

Use /audit. Cross-reference every module against APEX_V4_STRATEGY.md.

Report:
1. Which ADRs are fully implemented?
2. Which ADRs are partially implemented?
3. Any code that contradicts a documented decision?
4. Any mathematical formulas deviating from Section 7?
5. Test coverage gaps in the critical path?

Score each area 0-100. For anything below 90, provide
the exact fix with code. Do not provide vague suggestions.
```

### Performance Degradation

```
Win rate has dropped from [X]% to [Y]% over the last 14 days.

Investigate:
1. Query trade_outcomes: last 14 days vs prior 14 days
2. Break down by strategy, regime, and session
3. Identify which segments degraded most
4. Check if regime distribution shifted
5. Check if slippage increased
6. Check if any risk gates are firing more frequently

Produce a root cause hypothesis with supporting data.
Do not change any code until I approve the diagnosis.
```

### Production Incident

```
Kill switch triggered at HARD level. Reason: [paste reason]

Investigate and produce an incident report:
1. State at time of trigger
2. Which positions were in broker but not Redis, or vice versa?
3. Last successful reconciliation timestamp
4. Broker disconnect or process crash?
5. Open positions requiring manual attention?
6. Safe restart sequence

Do not restart the system. Give me the facts to decide.
```

---

## Part 4: Rules That Never Break

### Rule 1 — Goals, Not Instructions

```
WRONG: "Open src/risk/governor.py line 47 and add this check"
RIGHT: "Gate 5 VaR check is missing. Implement per Section 7.5."
```

### Rule 2 — Context Once Per Session

```
Read CLAUDE.md. Follow the session start protocol.
V3: ~/Desktop/apex_v3 — reference only.
V4: ~/Desktop/apex_v4 — build target.
Today's goal: [goal here]
```

### Rule 3 — Autonomy With Hard Boundaries

```
Implement [component]. Operate autonomously.
Make the safer choice on any ambiguity and document it.

Exception: stop and ask before any database schema change.
```

### Rule 4 — Verification Before Trust

```
[Component] was implemented. Before I use it in production:
1. Read the implementation
2. Read the relevant Section of APEX_V4_STRATEGY.md
3. Verify every formula matches exactly
4. Run existing tests
5. Write 3 edge case tests
6. Give me a confidence score 0-100 with justification

I do not proceed below 95.
```

### Rule 5 — Failure Is Information

```
This test is failing:
[paste full error output]

Diagnose from what you have. Read files if you need to.
Run commands if you need to. Tell me root cause and fix it.
```

---

## Quick Reference

```
DAILY START
  cd ~/Desktop/apex_v4 && source venv/bin/activate
  claude
  > Read CLAUDE.md. Follow session start protocol.
  > Today's goal: [goal]

CUSTOM SKILLS
  /implement    autonomous module build with tests
  /audit        architecture compliance check
  /fix          autonomous bug repair
  /phase-gate   phase completion verification
  /risk-verify  mathematical formula check
  /hardening    production readiness review

GOAL QUALITY CHECK
  Does it specify WHAT, not HOW?
  Does it reference the strategy spec?
  Does it include quality standards?
  Does it have clear completion criteria?
  Does it allow autonomous operation?

CONTEXT RECOVERY
  /compact → then:
  Re-orient from CLAUDE.md and git log.
  What phase are we on? What is the next task?
```
