# APEX V4 — Active Task Plan

## Session: 2026-03-21 — Scaffold & Environment Setup

### Goal
Create complete project scaffold from APEX_V4_STRATEGY.md Appendix,
install all dependencies, and make first commit.

### Checklist

- [x] Read CLAUDE.md and APEX_V4_STRATEGY.md
- [x] Write plan to tasks/todo.md
- [x] Create all folders from Appendix folder structure
- [x] Create all placeholder source files (src/, tests/, db/, ops/, scripts/)
- [x] Create .claude/commands/ custom skill files
- [x] Create config/, README.md, .gitignore
- [x] Install all required Python packages
- [x] Create requirements.txt from pip freeze
- [x] Test `import talib` works
- [x] Stage and commit: "chore: apex v4 scaffold"

### Notes
- ta-lib brew installed at /opt/homebrew/Cellar/ta-lib/0.6.4 ✓
- Python 3.11 venv exists at venv/ ✓
- Git initialized with no commits yet
- APEX_V4_STRATEGY.md Appendix: recorder.md appears to be a typo → using recorder.py

---

## Review — 2026-03-21

**Status: COMPLETE**

### Installation Results
- All 74 packages installed in venv/
- `MetaTrader5`: SKIPPED — Windows-only, not on macOS PyPI. Must be installed on the Windows
  machine running MT5. Noted in requirements.txt comments.
- `TA-Lib` brew (0.6.4): INSTALLED ✓ — Python bindings verified (ATR, ADX, BB all pass)
- All other packages: INSTALLED ✓

### Scaffold Results
- 55 files committed in first commit `73167f5`
- All folders from Appendix created
- Strategy note: `src/learning/recorder.md` in Appendix is a typo → created as `recorder.py`
- config/secrets.env created but excluded from git via .gitignore

### Next Step
Phase 0 — V3 bug fixes (Section 8, P0.1–P0.7)

---

## Session: 2026-03-21 — MT5 Abstraction Layer

### Goal
Create an MT5 abstraction layer so the codebase works on macOS (stub) and
Windows (real MT5). Solves the MetaTrader5 Windows-only package problem.

### Checklist
- [x] Read current state (settings.yaml, requirements.txt, src/market/)
- [x] Create `src/market/mt5_types.py` — data classes for AccountInfo, Tick, OrderResult
- [x] Create `src/market/mt5_client.py` — abstract base class
- [x] Create `src/market/mt5_real.py` — RealMT5Client wrapping actual MetaTrader5
- [x] Create `src/market/mt5_stub.py` — StubMT5Client with realistic fake data
- [x] Create `src/market/mt5_factory.py` — factory function reading mt5.mode from settings
- [x] Update `config/settings.yaml` — add mt5.mode: stub
- [x] Update `requirements.txt` — comment about MetaTrader5 Windows-only
- [x] Write unit tests for StubMT5Client (28 tests)
- [x] Confirm all existing tests still pass (28/28)

### Review — 2026-03-21

**Status: COMPLETE** — 28/28 tests pass.

### Design Decisions
- Added `mt5_types.py` to hold frozen dataclasses mirroring MT5 return types.
  This avoids the rest of the codebase depending on MetaTrader5 types at all.
- Factory reads `APEX_MT5_MODE` env var (fallback: "stub"). This is simpler
  than parsing settings.yaml at factory level — config layer can set the env var.
- `mt5_real.py` lazy-imports MetaTrader5 inside each method so the module
  can be imported on macOS without error.
- Stub returns realistic March 2026 prices for EURUSD, GBPUSD, USDJPY, AUDUSD.

---

## Session: 2026-03-21 — Phase 0: V3 Critical Bug Fixes

### Goal
Fix the 4 critical bugs in V3 (Section 1.2), write unit tests, verify all pass.
Each fix gets its own isolated commit: `fix(v3): <description>`.

### Bugs
1. **Hardcoded portfolio_value** — `main_v3.py:206` defaults to $100K
2. **Conviction fallback 0.5** — `main_v3.py:272` trades on brain failure
3. **Phantom fill tracking** — `paper_tracker.py:107` records before confirmation
4. **Full Kelly, no cap** — `risk_governor.py:327-331` no quarter-Kelly/2% cap/dd scalar

### Checklist
- [x] Fix 1: portfolio_value default None, refuse trade without real equity
- [x] Fix 2: conviction fallback 0.5 → 0.0
- [x] Fix 3: gate record_signal on TRADE_RETCODE_DONE (10009)
- [x] Fix 4: quarter-Kelly ×0.25, min(f,0.02), dd_scalar per Section 7.1
- [x] Write unit tests for all 4 fixes (17 new tests)
- [x] Run full V3 test suite — 813/813 pass
- [x] Tag v3.1-fixed
- [x] Confidence report

### Review — 2026-03-21

**Status: COMPLETE** — 813/813 tests pass, tagged v3.1-fixed.

### Commits (v3.0-pre-fix → v3.1-fixed)
1. `0b12bdb` fix(v3): remove hardcoded portfolio_value $100K default
2. `080d565` fix(v3): conviction fallback 0.5 → 0.0 on brain failure
3. `ea22218` fix(v3): gate fill recording on TRADE_RETCODE_DONE
4. `ef2f69b` fix(v3): quarter-Kelly + 2% cap + drawdown scalar (Section 7.1)
5. `af3fd81` test: unit tests for all 4 critical V3 bug fixes

---

## Session: 2026-03-21 — PostgreSQL Database Schema (P1.1)

### Goal
Implement the PostgreSQL database schema for APEX V4 using SQLAlchemy ORM
and Alembic migrations. 7 tables from Section 6 module contracts.

### Checklist
- [x] Create `db/models.py` — SQLAlchemy models for all 7 tables
- [x] Initialize Alembic in `db/` directory
- [x] Configure `alembic.ini` and `env.py` for apex_v4 database
- [x] Generate autogenerated migration
- [x] Run `alembic upgrade head`
- [x] Verify all 7 tables exist in PostgreSQL
- [ ] Commit: `feat: database schema`

### Tables
1. `market_snapshots` — raw tick/snapshot data (pair, timestamp, candles JSONB, spread, session)
2. `candles` — OHLCV time-series (pair, timeframe, timestamp, O/H/L/C/V)
3. `feature_vectors` — computed indicators (pair, timestamp, atr_14, adx_14, ema_200, bb_*, session)
4. `trade_outcomes` — trade results (pair, strategy, regime, session, direction, entry/exit, r_multiple)
5. `kill_switch_events` — kill switch audit trail (level, reason, previous/new state)
6. `fills` — fill tracking (order_id, pair, direction, prices, slippage)
7. `reconciliation_log` — state reconciliation audit (redis vs MT5 snapshots, mismatch, action)
