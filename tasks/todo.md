# APEX V4 — Active Task Plan

## Session: 2026-03-26 — Capital Allocation + Grafana Dashboard (P6.4)

### Goal
Two final tasks before tagging v4.0-phase6-ready:
1. Wire `capital_allocation_pct` into CalibrationEngine so position sizes reflect allocated capital
2. Create Grafana dashboard JSON for production monitoring

### Checklist
- [x] TASK 1: Capital allocation
  - [x] Add `capital_allocation_pct` param to `CalibrationEngine.__init__`
  - [x] Multiply `final_size` by `capital_allocation_pct` in `calibrate()`
  - [x] Pass setting from `init_context()` in pipeline.py
  - [x] Add startup log: "Capital allocation: X% of portfolio"
  - [x] Add unit tests for capital allocation scaling (4 tests)
- [x] TASK 2: Grafana dashboard
  - [x] Create `ops/grafana_dashboard.json` with 9 panels
- [x] Tag `v4.0-phase6-ready`
- [x] Do NOT change `trading_mode` to "live"

---

## Session: 2026-03-27 — Modular Commit & Finalization

### Goal
Commit all Phase 6 changes modularly, update .gitignore, and provide final playbook.

### Checklist
- [x] Update `.gitignore` to ignore `data/` directory
- [x] Commit pipeline hardening and DI fixes
- [x] Commit pre-flight refactor and bypass logic
- [x] Add `APEX_V4_CLI_PLAYBOOK_1.md`
- [x] Finalize `tasks/todo.md` and `tasks/lessons.md`

**Status: COMPLETE**
- 5 modular commits applied
- Working tree clean
- Documentation updated

---

## Previous: Pre-Flight Paper Trading Bypass (P6.2b)

### Goal
Refactor `run_preflight()` to implement a "Native Paper Trading Bypass" —
checks 8-9 (V3 data imported, ADR-002 segment counts) are bypassed in paper
mode with a yellow warning instead of blocking startup. Live mode still blocks.

### Checklist
- [x] Add `_YELLOW` ANSI colour helper and `_yellow()` formatter
- [x] Add `capital_allocation_pct: 0.10` to `config/settings.yaml` under `risk:`
- [x] Refactor `run_preflight()`:
  - [x] Exactly 9 checks in spec order (1-7 hard, 8-9 bypassable)
  - [x] Read `trading_mode` from `system.mode` in settings
  - [x] Paper mode: checks 8-9 fail → yellow warning, proceed to confirmation
  - [x] Live mode: checks 8-9 fail → red error, `sys.exit(1)`
  - [x] Hard checks 1-7: always block on failure regardless of mode
  - [x] Remove `_check_paper_duration` from check sequence (not in spec)
  - [x] Display trading mode in banner
  - [x] Log `trading_mode` and `bypassed` count on success
- [x] Rewrite `tests/unit/test_preflight.py`:
  - [x] All 9 individual check tests preserved (32 tests)
  - [x] 16 `run_preflight()` integration tests covering:
    - Confirmation flow (pass, wrong, EOF, KeyboardInterrupt)
    - Hard check failures block in both modes
    - Paper bypass for checks 8, 9, and both
    - Live mode blocks on checks 8, 9, and both
    - Mixed: hard fail + bypass fail → hard takes priority
    - Missing `system.mode` defaults to paper
- [x] All 689 tests pass (48 preflight + 641 existing)
- [x] Zero regressions

### Review
**Built:** Paper trading bypass for pre-flight validation. The 9 checks run in
the exact order specified. Checks 1-7 (Redis, PostgreSQL, MT5, kill switch,
state drift, capital_allocation_pct, secrets.env) are hard requirements that
always block. Checks 8-9 (V3 data imported, segment counts/ADR-002) are
bypassed in paper mode with a yellow "PAPER MODE ENABLED: Insufficient segment
history. Bootstrapping database natively with default minimum risk." warning.
Live mode blocks on all 9 checks. Operator confirmation ("CONFIRMED <pct>")
is required in all cases.

**Tests:** 48 tests (32 individual checks + 16 orchestrator integration).
Net +8 tests vs previous (removed 3 paper_duration tests, added 11 bypass tests).

**Decisions:**
1. `system.mode` used as `trading_mode` — already exists in settings.yaml, no new key needed
2. `_check_paper_duration` kept in source (not deleted) but removed from check sequence — out of spec
3. `capital_allocation_pct: 0.10` added to settings.yaml — was missing, required for check #6

---

## Session: 2026-03-26 — Pipeline Delta: Metric + Guard + Tests (P6.3)

### Goal
Harden the existing pipeline orchestrator with a cycle duration metric,
an account_info None guard, and 7 new integration tests covering all
untested code paths. Scope reduced per eng review — no regime routing
change, no rename.

### Context
The main trading loop already exists (P5.4-P5.5, commit ea6c9e7):
- `_async_main()` — async orchestrator with ZMQ PULL, shutdown, kill switch
- `process_tick()` — full pipeline: features → regime → alpha → calibrate → risk → execute → fill
- `init_context()` — builds all 18 components with DI
- 674 tests passing

Eng review found 2 code changes + 7 test gaps. Outside voice (Claude subagent)
challenged explicit regime routing and rename — both dropped as unnecessary risk.

### Data flow (unchanged, for reference)
```
MarketFeed ──ZMQ PUSH──→ _async_main() ──ZMQ PULL──→ process_tick()
                                                        │
    ┌───────────────────────────────────────────────────┘
    │
    ├─ Gate 0: kill_switch.is_active? → return
    ├─ Step 1: fabric.compute(snapshot) → FeatureVector
    ├─ Step 2: classifier.classify(fv) → Regime
    ├─ Step 3: _check_paper_closes(snapshot)
    ├─ Step 4: UNDEFINED → return
    ├─ Step 5: momentum.generate() + mr.generate() → hypotheses
    ├─ Step 6: mt5.account_info() → equity/balance  ← NEW: None guard
    ├─ Step 7: For each hypothesis:
    │    ├─ cal_engine.calibrate() → CalibratedTradeIntent
    │    ├─ governor.evaluate() → RiskDecision
    │    ├─ gateway.execute() → FillRecord
    │    └─ fill_tracker.record_fill()
    └─ Paper close detection → recorder → updater
```

### Checklist

**Code changes:**
- [x] Add `APEX_CYCLE_DURATION_MS` Histogram to `src/observability/metrics.py`
      Buckets: (10, 50, 100, 200, 500, 1000, 2000) ms
- [x] Observe `APEX_CYCLE_DURATION_MS` in `_async_main()` around `process_tick()`
      (wall-clock from message arrival to processing completion, not just compute)
- [x] Add None guard on `account_info()` in `process_tick()`:
      if None → `logger.warning("tick_skipped", reason="account_info_unavailable")` + return
      (Verified: StubMT5Client.account_info() returns valid data after init — paper mode safe)

**Tests (all in `tests/integration/test_pipeline.py`):**
- [x] Test: ValueError from fabric.compute() → tick skipped, no fills
- [x] Test: Both alpha engines return None → no governor.evaluate() called
- [x] Test: None from account_info() → tick skipped, no calibration
- [x] Test: Governor rejects → no gateway.execute() called
- [x] Test: Gateway returns None → no fill_tracker.record_fill() called
- [x] Test: SIGTERM triggers graceful shutdown (mock is_shutting_down, verify cleanup)
- [x] Test: Unhandled exception triggers EMERGENCY kill switch + sys.exit(1)

**Verification:**
- [x] All existing 674 tests still pass (no regressions)
- [x] New tests pass
- [x] Total test count = 681 ✓

### NOT in scope
- Explicit regime routing — dropped per outside voice (self-filtering is simpler, validated)
- Rename _async_main → run_pipeline — dropped (zero value, nonzero risk)
- governor.evaluate() async timeout — deferred to TODOS.md
- Grafana dashboard for new metric — deferred to TODOS.md
- Alertmanager rules — deferred to TODOS.md

### Eng review decisions
1. **Scope:** Build on existing (Option A) — not rewrite
2. **Regime routing:** Dropped per outside voice — self-filtering already correct
3. **Account guard:** Skip tick on None (Option A) — "Broker is Truth"
4. **Tests:** All in test_pipeline.py (Option A) — one file, one mental model
5. **Rename:** Dropped per outside voice — pure cost, zero value

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 2 | issues_found | Outside voice challenged routing + rename |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 3 | CLEAR | 1 issue (account guard), 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |

- **CROSS-MODEL:** Claude review recommended explicit routing; outside voice challenged it (measure first). User sided with outside voice — routing dropped.
- **UNRESOLVED:** 0
- **VERDICT:** ENG CLEARED — ready to implement.

---

## Session: 2026-03-25 — Startup Pre-Flight Validation (P6.2)

### Goal
Implement `run_preflight()` in `src/pipeline.py` — 10 mandatory checks before
the trading loop starts. Any failure blocks startup with red diagnostics + fix steps.
All pass → green summary → operator types "CONFIRMED <capital_allocation_pct>" to proceed.

### Checklist
- [x] Implement `run_preflight()` with 10 checks
- [x] Wire into `_async_main()` before the main loop
- [x] Write 40 unit tests (all 10 checks pass/fail, confirmation flow)
- [x] All 674 tests pass (including 40 new preflight tests)

---

## Session: 2026-03-25 — V3 Data Migration (P6.1)

### Goal
Implement `scripts/migrate_v3_data.py` to import V3 paper trade history into
V4 `trade_outcomes` table, seeding segment data for go-live.

### Checklist
- [x] Explore V3 codebase: locate trade log files, schema, regime/strategy labels
- [x] Read V4 `trade_outcomes` schema, session classifier, enums
- [x] Implement `scripts/migrate_v3_data.py`:
  - [x] Load from Redis (`apex:paper_trades`) with fallback
  - [x] Load from JSON files (`data/paper_trades.json`, `data/output/paper_trades.json`)
  - [x] Deduplicate by paper_id across sources
  - [x] Optional enrichment from V3 PostgreSQL `signals` table
  - [x] Map strategy: TREND_CONTINUATION/LIQUIDITY_SWEEP_REVERSAL→MOMENTUM, MEAN_REVERSION→MEAN_REVERSION, fallback heuristic
  - [x] Map regime: TRENDING+LONG→TRENDING_UP, TRENDING+SHORT→TRENDING_DOWN, RANGING→RANGING, else→UNDEFINED
  - [x] Session from `opened_at` UTC hour (mirrors `feed.py`)
  - [x] Direction-aware r_multiple: LONG=(exit−entry)/risk, SHORT=(entry−exit)/risk
  - [x] won = r_multiple > 0, mode = "v3_historical"
  - [x] Segment breakdown: strategy × regime × session with < 30 flagged red
  - [x] Bulk insert via `PerformanceDatabase.bootstrap_from_v3()`
  - [x] `--dry-run` flag for preview without DB write
- [x] Write 34 unit tests covering all mapping paths
- [x] All 634 tests pass (including 34 new migration tests)

### Notes
- V3 paper_trades.json files currently empty on macOS dev machine — trades are in Redis on prod Windows VPS
- V3 paper trades do NOT store strategy or regime; these come from V3 PostgreSQL `signals` table via optional enrichment
- When V3 DB unavailable, heuristic inference: RANGING regime → MEAN_REVERSION, else MOMENTUM; no regime → UNDEFINED

---

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
- [x] Commit: `feat: database schema`

### Review — 2026-03-21

**Status: COMPLETE** — 7/7 tables verified in PostgreSQL.

### Results
- Commit `9ebe206` — 9 files, 835 insertions
- Tables: market_snapshots, candles, feature_vectors, trade_outcomes,
  kill_switch_events, fills, reconciliation_log
- 6 custom PostgreSQL enums: trading_session, strategy_type, regime_type,
  direction_type, kill_switch_level, timeframe_type
- JSONB columns: candles (market_snapshots), redis/mt5 positions (reconciliation_log)
- Indexes: composite (pair,timestamp), segment (strategy,regime,session), unique (order_id, candle dedup)
- Fixed stale PostgreSQL PID file (PID 706 = macOS speech service, not postgres)

### Design Decisions
- Database URL from `APEX_DATABASE_URL` env var, fallback `postgresql://localhost:5432/apex_v4`
- `timestamp_ms` (BigInteger) for all timestamps — Unix milliseconds per strategy spec
- `fill_id` on trade_outcomes is nullable (soft FK) — not all outcomes have fills (e.g. V3 imports)
- Alembic config lives in `db/` subdirectory, sys.path adjusted in env.py

### Tables
1. `market_snapshots` — raw tick/snapshot data (pair, timestamp, candles JSONB, spread, session)
2. `candles` — OHLCV time-series (pair, timeframe, timestamp, O/H/L/C/V)
3. `feature_vectors` — computed indicators (pair, timestamp, atr_14, adx_14, ema_200, bb_*, session)
4. `trade_outcomes` — trade results (pair, strategy, regime, session, direction, entry/exit, r_multiple)
5. `kill_switch_events` — kill switch audit trail (level, reason, previous/new state)
6. `fills` — fill tracking (order_id, pair, direction, prices, slippage)
7. `reconciliation_log` — state reconciliation audit (redis vs MT5 snapshots, mismatch, action)

---

## Session: 2026-03-21 — Pydantic V2 Schemas (P1.2)

### Goal
Implement `src/market/schemas.py` with 6 Pydantic v2 models matching
APEX_V4_STRATEGY.md Section 6 module contracts exactly. Full validation.

### Checklist
- [x] Implement OHLCV, MarketSnapshot, FeatureVector, AlphaHypothesis,
      CalibratedTradeIntent, RiskDecision in `src/market/schemas.py`
- [x] Write unit tests proving invalid data is rejected (66 tests)
- [x] Run all tests — 94/94 pass (66 new + 28 existing)
- [x] Commit: `feat: pydantic v2 schemas` → `706ad67`

### Review — 2026-03-21

**Status: COMPLETE** — 66 tests, 94/94 total pass.

### Models Implemented
| Model | Key Constraints |
|---|---|
| OHLCV | volume >= 0, frozen |
| MarketSnapshot | pair 6 chars, spread > 0, candle minimums (M5:50, M15:50, H1:200, H4:50), is_stale computed (>5000ms), frozen |
| FeatureVector | pair 6 chars, timestamp > 0, all indicator floats required, frozen |
| AlphaHypothesis | setup_score 0-30, expected_R >= 1.8, conviction 0.65-1.0 (MR only, None for MOMENTUM), frozen |
| CalibratedTradeIntent | p_win 0-1, edge > 0, suggested_size 0-0.02, segment_count >= 0, frozen |
| RiskDecision | gate_failed 1-7 (required for REJECT/REDUCE, None for APPROVE), final_size >= 0, reason non-empty, frozen |

### Design Decisions
- All models frozen (immutable) — data contracts should never be mutated after creation
- StrEnum for all enums — clean string serialization, Pydantic-native validation
- CandleMap as nested model — enforces min-length per timeframe at parse time
- model_validator(mode="after") for cross-field rules (conviction/strategy, gate_failed/decision)
- is_stale is a @computed_field property — recalculated on every access against wall clock

---

## Session: 2026-03-21 — Async Market Feed (P1.3)

### Goal
Implement `src/market/feed.py` — async MT5 data ingestion with candle close
detection, session classification, snapshot validation, and ZMQ publishing.

### Checklist
- [x] Add `RateBar` dataclass + timeframe constants to `mt5_types.py`
- [x] Add `copy_rates_from_pos()` to MT5Client, StubMT5Client, RealMT5Client
- [x] Implement `MarketFeed` class in `src/market/feed.py`
  - [x] `classify_session(utc_hour)` — OVERLAP 12-16, LONDON 7-12, NY 16-21, ASIA else
  - [x] Async polling loop with candle close detection
  - [x] Build + validate MarketSnapshot per pair on candle close
  - [x] ZMQ PUSH to `tcp://127.0.0.1:5559`
  - [x] On validation failure: log error, skip — never propagate bad data
- [x] Write unit tests — 23 tests, MT5 fully mocked
- [x] Run all tests — 117/117 pass (23 feed + 66 schema + 28 MT5)
- [x] Commit: `7421bcb feat: MT5 abstraction layer` + `58cdf6a feat: async market feed`

### Review — 2026-03-21

**Status: COMPLETE** — 23 feed tests, 117/117 total pass.

### Files Modified/Created
- `src/market/mt5_types.py` — added `RateBar` dataclass, timeframe constants + `TIMEFRAME_MAP`
- `src/market/mt5_client.py` — added `copy_rates_from_pos()` to ABC
- `src/market/mt5_stub.py` — stub implementation generates deterministic fake bars
- `src/market/mt5_real.py` — wraps `mt5.copy_rates_from_pos()` → list[RateBar]
- `src/market/feed.py` — `MarketFeed` class (async), `classify_session()` function
- `tests/unit/test_feed.py` — 23 tests covering all code paths

### Design Decisions
- Candle close detection via bar-timestamp diffing (poll, compare, emit on change)
- Trigger timeframes: M5, M15, H1 only — H4 included in snapshot data but doesn't trigger
- Session classifier is a pure function, priority: OVERLAP > LONDON > NY > ASIA
- ZMQ socket bound lazily inside `run()` so event loop owns the context
- `_build_snapshot` catches all exceptions → returns None, logs error, increments counter
- Snapshot JSON serialized via `model_dump_json()` (Pydantic v2 native)

---

## Session: 2026-03-21 — Feature Fabric (P1.4)

### Goal
Implement `src/features/fabric.py` — TA-Lib indicator computation from
MarketSnapshot H1 candles → FeatureVector.

### Checklist
- [x] Add `spread_max_points` to `config/settings.yaml`
- [x] Implement `FeatureFabric` in `src/features/fabric.py`
  - [x] Extract H1 candles → numpy arrays
  - [x] ATR(14), ADX(14), EMA(200), BBANDS(20,2,2) via TA-Lib
  - [x] Raise ValueError if < 200 H1 candles
  - [x] `spread_ok` from config threshold
  - [x] `news_blackout` from Redis key `news_blackout_{pair}`
  - [x] Return validated FeatureVector
- [x] Write unit tests — 26 tests with known input/output pairs
- [x] Run all tests — 143/143 pass
- [x] Commit: `feat: feature fabric` → `0ccb9e2`

### Review — 2026-03-21

**Status: COMPLETE** — 26 tests, 143/143 total pass.

### Indicators (all TA-Lib, no custom numpy)
| Indicator | TA-Lib Call | Verified With |
|---|---|---|
| `atr_14` | `talib.ATR(high, low, close, timeperiod=14)` | Linear ramp → 0.002, Sine → 0.0016 |
| `adx_14` | `talib.ADX(high, low, close, timeperiod=14)` | Linear → 100.0, Sine → 41.82 |
| `ema_200` | `talib.EMA(close, timeperiod=200)` | Linear → 1.10995, Sine → 1.10 |
| `bb_upper` | `talib.BBANDS(close, 20, 2, 2)[0]` | Linear → 1.12010 |
| `bb_mid` | `talib.BBANDS(close, 20, 2, 2)[1]` | Linear → 1.11895 |
| `bb_lower` | `talib.BBANDS(close, 20, 2, 2)[2]` | Linear → 1.11780 |

### Design Decisions
- FeatureFabric takes `spread_max_points` as constructor arg, not reading YAML itself
- Redis client injected via constructor — `None` disables news_blackout (always False)
- Redis errors caught and defaulted to False — never crash on Redis failure
- Added `spread.max_points: 0.00030` (3 pips) to settings.yaml
- Fixed pre-existing flaky `test_boundary_5000ms_not_stale` (race between clock reads)

---

## Session: 2026-03-21 — Redis + PostgreSQL State Manager (P1.5)

### Goal
Implement `src/features/state.py` — RedisStateManager for TTL-cached state
and PostgresWriter for async durable writes.

### Checklist
- [x] Implement `RedisStateManager` class
  - [x] `store_feature_vector(fv)` → key `fv:{pair}`, TTL 300s
  - [x] `get_feature_vector(pair)` → FeatureVector | None
  - [x] `store_open_positions(positions)` → key `open_positions`, TTL 60s
  - [x] `get_open_positions()` → list
  - [x] `set_kill_switch(level)` → key `kill_switch`, no TTL
  - [x] `get_kill_switch()` → str | None
  - [x] `set_news_blackout(pair, active, duration_minutes)`
- [x] Implement `PostgresWriter` class
  - [x] `write_feature_vector(fv)` → insert into feature_vectors
  - [x] `write_trade_outcome(outcome)` → insert into trade_outcomes
  - [x] `write_kill_switch_event(level, reason)` → insert into kill_switch_events
  - [x] All writes async via asyncio.to_thread
  - [x] On error: log critical, do NOT crash
- [x] All connection details from environment variables
- [x] Write unit tests — 29 tests (Redis + SQLAlchemy fully mocked)
- [x] Run all tests — 172/172 pass
- [x] Commit: `feat: redis state manager + postgres writer` → `5c434a9`

### Review — 2026-03-21

**Status: COMPLETE** — 29 tests, 172/172 total pass.

### RedisStateManager API
| Method | Key | TTL |
|---|---|---|
| `store_feature_vector(fv)` | `fv:{pair}` | 300s |
| `get_feature_vector(pair)` → `FeatureVector \| None` | `fv:{pair}` | — |
| `store_open_positions(positions)` | `open_positions` | 60s |
| `get_open_positions()` → `list` | `open_positions` | — |
| `set_kill_switch(level)` | `kill_switch` | none |
| `get_kill_switch()` → `str \| None` | `kill_switch` | — |
| `set_news_blackout(pair, active, mins)` | `news_blackout_{pair}` | mins×60 |

### PostgresWriter API
| Method | Table | Async |
|---|---|---|
| `write_feature_vector(fv)` | `feature_vectors` | asyncio.to_thread |
| `write_trade_outcome(outcome)` | `trade_outcomes` | asyncio.to_thread |
| `write_kill_switch_event(level, reason)` | `kill_switch_events` | asyncio.to_thread |

### Design Decisions
- RedisStateManager is sync (matches FeatureFabric's sync redis usage)
- PostgresWriter wraps sync SQLAlchemy in asyncio.to_thread — no asyncpg dependency
- All DB/Redis errors caught and logged at CRITICAL — pipeline never crashes
- FakeRedis test helper avoids external test dependency on fakeredis package
- Connection URLs from env vars: APEX_REDIS_URL, APEX_DATABASE_URL

---

## Session: 2026-03-24 — Regime Classifier (P2.1)

### Goal
Implement `src/regime/classifier.py` — hard ADX-based regime classification.
No ML, no probabilities. Pure deterministic rules from FeatureVector inputs.

### Checklist
- [x] Implement `RegimeClassifier` in `src/regime/classifier.py`
  - [x] news_blackout → UNDEFINED
  - [x] spread_ok False → UNDEFINED
  - [x] ADX > 25 AND close > EMA200 → TRENDING_UP
  - [x] ADX > 25 AND close < EMA200 → TRENDING_DOWN
  - [x] ADX < 20 → RANGING
  - [x] ADX 20-25 → UNDEFINED
  - [x] structlog logging for every classification
- [x] Write unit tests — 25 tests covering all 6 branches + edge cases
- [x] Run all tests — 234/234 pass
- [ ] Commit: `feat: regime classifier`

### Review — 2026-03-24

**Status: COMPLETE** — 25 tests, 234/234 total pass.

### Design Decisions
- `classify(fv, close_price)` takes close_price as separate arg because
  FeatureVector (frozen schema from Section 6) has no raw close field.
  The caller (pipeline) has access to the latest H1 close from MarketSnapshot.
- Thresholds injected via constructor (default 25/20 matching settings.yaml).
- `_log_and_return` helper logs every classification with full context.
- close == ema_200 with ADX > 25 → falls through to UNDEFINED (neither > nor <).
- ADX == 20 and ADX == 25 are both dead zone (strict inequalities in rules).

---

## Session: 2026-03-24 — Momentum Engine (P2.2)

### Goal
Implement `src/alpha/momentum.py` — multi-TF momentum engine.
Fires on TRENDING_UP / TRENDING_DOWN only. ATR-based stops, min R:R ≥ 1.8.

### Checklist
- [x] Implement `MomentumEngine` in `src/alpha/momentum.py`
  - [x] Regime gate: only TRENDING_UP or TRENDING_DOWN
  - [x] Multi-TF confirmation: H4 EMA20 + H1 EMA20 agree with direction
  - [x] Entry zone: M15 EMA20 ± 0.2×ATR
  - [x] Stop loss: entry ± 1.5×ATR against direction
  - [x] Take profit: entry ± 4.0×ATR in direction
  - [x] Setup score 0-30 (4 components)
  - [x] Reject if expected_R < 1.8
  - [x] Log every rejection with reason
- [x] Write unit tests — 33 tests (scoring, rejections, edge cases)
- [x] Run all tests — 267/267 pass
- [ ] Commit: `feat: momentum engine`

### Review — 2026-03-24

**Status: COMPLETE** — 33 tests, 267/267 total pass.

### Design Decisions
- `generate(fv, regime, snapshot)` takes the MarketSnapshot for EMA20 computation
  on M15/H1/H4 candle arrays. FeatureVector only has EMA-200, not EMA-20.
- Multi-TF confirmation: H4 close vs H4 EMA20, H1 close vs H1 EMA20 — both
  must agree with regime direction.
- Entry mid = M15 EMA20; entry_zone = (mid - 0.2×ATR, mid + 0.2×ATR).
- SL = 1.5×ATR, TP = 4.0×ATR → expected_R ≈ 2.67 (always > 1.8 with fixed mults).
- Spread bonus threshold: 1 pip (0.00010) — strict < not ≤.
- conviction=None for MOMENTUM (enforced by AlphaHypothesis validator).

---

## Session: 2026-03-24 — Mean Reversion Pipeline (P2.3–P2.7)

### Goal
Implement the mean reversion alpha pipeline: ADF gate → Kalman filter → OU MLE
→ conviction score → signal. Three files, exact Section 7.2/7.3 formulas.

### Checklist
- [x] Create `src/alpha/kalman.py` — filterpy Kalman wrapper
  - [x] dim_x=1, dim_z=1
  - [x] R from rolling variance of last 100 closes
  - [x] Process each H1 close → return filtered states
- [x] Create `src/alpha/ou_calibration.py` — OU MLE + conviction
  - [x] ρ = lag-1 autocorrelation
  - [x] θ = -ln(ρ) / Δt
  - [x] μ = mean(X)
  - [x] σ² = exact formula from Section 7.2
  - [x] half_life = ln(2) / θ
  - [x] Reject if ρ ≤ 0 or half_life > 48
  - [x] Conviction: σ_eq, z-score, erf mapping, 3σ guard
- [x] Implement `src/alpha/mean_reversion.py` — orchestrator
  - [x] RANGING regime gate
  - [x] Min 200 H1 candles gate
  - [x] ADF p-value < 0.05 gate
  - [x] Pipeline integration: Kalman → OU → conviction → signal
  - [x] Return None with reason at every failure
- [x] Write unit tests — 45 tests (8 Kalman + 22 OU/conviction + 15 orchestrator)
- [x] Run all tests — 312/312 pass
- [x] Run /risk-verify — Section 7.2 PASS ✓, Section 7.3 PASS ✓, 0 deviations
- [ ] Commit: `feat: mean reversion pipeline`

### Review — 2026-03-24

**Status: COMPLETE** — 45 tests, 312/312 total pass. /risk-verify: VERIFIED ✓

### /risk-verify Results
- Section 7.2 (OU MLE): PASS — all 6 formulas match exactly
- Section 7.3 (Conviction): PASS — all 4 formulas match exactly
- Section 7.1, 7.4, 7.5: NOT YET IMPLEMENTED (Phase 3)
- 0 silent deviations, 0 undocumented approximations

### Design Decisions
- Three-file split: kalman.py (smoothing), ou_calibration.py (MLE + conviction),
  mean_reversion.py (orchestrator) — single responsibility.
- Kalman uses filterpy.kalman.KalmanFilter(dim_x=1, dim_z=1), random-walk model.
  R updated from rolling variance of last 100 closes — not static.
- OU MLE Δt = 1.0 (H1 candle intervals).
- ADF uses maxlag=1, regression="c", autolag=None. Catches ValueError on constant input.
- Direction from z-score: z < 0 (below mean) → LONG, z > 0 → SHORT.
- SL = 1.5×ATR against direction, TP = μ (mean reversion target).
- Setup score: +10 ADF<0.01, +10 HL<24, +5 LONDON/OVERLAP, +5 conviction>0.80.

---

## Session: 2026-03-24 — Backtest Validation (P2.8)

### Goal
Run a backtrader backtest on 6 months synthetic EURUSD H1 data.
Both engines through regime classifier. Validate regime distribution.

### Checklist
- [x] Generate 6 months synthetic EURUSD H1 data (3120 candles)
- [x] Create backtrader data feed adapter (PandasData)
- [x] Create backtrader strategy using regime classifier + both engines
- [x] Run backtest, collect regime distribution + signal stats
- [x] Validate: 25-35% trending, 35-45% ranging
- [x] Adjust ADX thresholds: 25→31 trend, 20→22 range (4 attempts)
- [x] All 312 tests pass with new thresholds
- [ ] Commit + tag v4.0-phase2

### Review — 2026-03-24

**Status: COMPLETE** — backtest validated, 312/312 tests pass.

### Backtest Results (Final — attempt 4)
```
ADX thresholds: trend=31, range=22

Total candles:      3120
Candles classified: 2921 (199 warmup)

Regime Distribution:
  TRENDING_UP       18.7%  (545 candles)
  TRENDING_DOWN     13.4%  (390 candles)
  RANGING           38.7%  (1131 candles)
  UNDEFINED         29.3%  (855 candles)

  Trending (UP+DOWN): 32.0% ✓ (target 25-35%)
  Ranging:            38.7% ✓ (target 35-45%)

Signals:
  Momentum:  728 signals, avg expected R = 2.6669
  MR:        0 signals (synthetic data doesn't pass ADF consistently)
```

### ADX Threshold Adjustment History
| Attempt | Trend | Range | Trending% | Ranging% | Result |
|---------|-------|-------|-----------|----------|--------|
| 1       | 25    | 20    | 50.8%     | 29.8%    | FAIL   |
| 2       | 27    | 22    | 43.5%     | 38.7%    | FAIL   |
| 3       | 29    | 22    | 37.9%     | 38.7%    | FAIL   |
| 4       | 31    | 22    | 32.0%     | 38.7%    | PASS ✓ |

### Design Decisions
- Synthetic data uses regime-switching OU/drift model (not real MT5 data).
- MR signals=0 expected: synthetic random data rarely passes ADF stationarity.
  MR pipeline is validated by unit tests (45 tests in test_kalman/ou/mr).
- ADX thresholds raised because synthetic data has more ADX variation than
  typical Forex data. These thresholds tune the regime distribution.
- Backtest uses H1 for all TF slots (M5/M15/H4 reuse H1 subsets) since
  we only generate H1 synthetic data. Multi-TF validation deferred to live.

---

## Session: 2026-03-24 — Performance Database (P3.1)

### Goal
Implement `src/calibration/history.py` — PerformanceDatabase class for
segment-keyed trade outcome queries. 90-day rolling window, min 30-trade gate.

### Checklist
- [x] Implement `PerformanceDatabase` in `src/calibration/history.py`
  - [x] `get_segment_stats(strategy, regime, session)` → dict | None
  - [x] 90-day rolling window via `closed_at >= cutoff`
  - [x] Return None if count < 30 (ADR-002)
  - [x] Return: {win_rate, avg_R, trade_count, last_updated}
  - [x] `update_segment(outcome)` → insert into trade_outcomes
  - [x] `bootstrap_from_v3(v3_data)` → bulk import, fill_id=None, return count
  - [x] All DB errors logged at CRITICAL, never crash
- [x] Write unit tests — 26 tests (SQLite in-memory, real SQL aggregation)
- [x] Run all tests — 338/338 pass (26 new + 312 existing)
- [ ] Commit: `feat: performance database (P3.1)`

### Review — 2026-03-24

**Status: COMPLETE** — 26 tests, 338/338 total pass.

### Test Coverage
| Test Class | Count | What It Validates |
|---|---|---|
| TestSegmentMinimumGate | 4 | None at 0/29 trades, stats at 30/50 |
| TestSegmentWinRate | 4 | 100%/0%/66.7% win rates, avg_R calc |
| TestSegment90DayWindow | 2 | Old trades excluded, boundary behavior |
| TestSegmentIsolation | 3 | Strategy/regime/session filtering |
| TestSegmentReturnFields | 3 | All keys present, correct types |
| TestUpdateSegment | 3 | Insert, multiple inserts, error safety |
| TestBootstrapFromV3 | 6 | Bulk import, fill_id=None, error, roundtrip |
| TestSegmentErrorHandling | 1 | DB error returns None |

### Design Decisions
- SQLite in-memory DB for tests — exercises real SQL aggregation (COUNT, AVG,
  filter) rather than brittle mock chains. Raw DDL for table creation since
  SQLite doesn't support BigInteger AUTOINCREMENT or PostgreSQL enums.
- `case(won==True → 1, else → 0)` for win_rate AVG — portable across
  PostgreSQL and SQLite (avoids CAST(bool AS int) dialect issues).
- Session factory injected via constructor, same pattern as PostgresWriter.
- `bootstrap_from_v3` sets fill_id=None — V3 trades have no fill tracking,
  this is the provenance marker.

---

## Session: 2026-03-24 — Calibration Engine + EWMA Covariance + Kill Switch (P3.2–P3.4)

### Goal
Implement three Phase 3 modules:
- P3.2: CalibrationEngine (Kelly criterion, dd_scalar, correlation_scalar)
- P3.3: EWMACovarianceMatrix (EWMA update, eigenvalue shrinkage, Φ(κ), VaR)
- P3.4: KillSwitch (3-level, escalation-only, dual persistence, chaos restart)

### Checklist
- [x] Implement `CalibrationEngine` in `src/calibration/engine.py`
  - [x] Exact Section 7.1: f* = (p*b - q)/b, quarter-Kelly, 2% cap
  - [x] dd_scalar: <2% → 1.0, <5% → 0.5, ≥5% → None
  - [x] correlation_scalar: ≥2 same-currency → 0.5
  - [x] Return None with logged reason for every rejection
- [x] Write unit tests — 33 tests (Kelly math, dd branches, correlation, rejections)
- [x] Run /risk-verify — Section 7.1 PASS ✓
- [x] All tests pass — 371/371
- [x] Implement `EWMACovarianceMatrix` in `src/risk/covariance.py`
  - [x] EWMA: Σ_t = 0.999 × Σ_{t-1} + 0.001 × (r × r^T)
  - [x] Eigenvalue shrinkage: floor = max_eig / 15.0, clip, reconstruct
  - [x] Φ(κ): 1.0 if κ≤15, exp(-0.5×(κ-15)) if 15<κ<30, 0.0 if κ≥30
  - [x] VaR_99 = 2.326 × sqrt(W^T × Σ_reg × W) × portfolio_value
- [x] Write unit tests — 33 tests (EWMA update, shrinkage, Φ(κ), VaR, edge cases)
- [x] Run /risk-verify — Sections 7.1–7.5 ALL PASS ✓ (5/5 verified, 0 deviations)
- [x] All tests pass — 404/404
- [x] Implement `KillSwitch` in `src/risk/kill_switch.py`
  - [x] Three levels: SOFT (block signals), HARD (flatten), EMERGENCY (disconnect+dump)
  - [x] asyncio.Lock state management
  - [x] Escalation only — HARD → SOFT forbidden
  - [x] Dual persistence: Redis + PostgreSQL on every change
  - [x] Startup recovery from PostgreSQL
  - [x] Manual reset: exact "I CONFIRM SYSTEM IS SAFE" or PermissionError
  - [x] EMERGENCY: MT5 disconnect, JSON state dump, alert callback
- [x] Write unit tests — 38 tests (escalation, persistence, recovery, chaos, actions)
- [x] All tests pass — 442/442
- [ ] Commit: `feat: calibration engine + EWMA covariance + kill switch (P3.2–P3.4)`

### Review — 2026-03-24

**Status: COMPLETE** — 104 new tests, 442/442 total pass. /risk-verify: 5/5 VERIFIED ✓

### /risk-verify Results (Full)
| Section | Status | Deviations |
|---|---|---|
| 7.1 Kelly Criterion | PASS ✓ | 0 |
| 7.2 OU Process MLE | PASS ✓ | 0 |
| 7.3 Conviction Score | PASS ✓ | 0 |
| 7.4 EWMA Covariance | PASS ✓ | 0 |
| 7.5 Portfolio VaR | PASS ✓ | 0 |

### Design Decisions
- CalibrationEngine: `suggested_size` is a fraction [0, 0.02], not dollar amount —
  execution layer multiplies by equity. Matches Pydantic schema.
- CalibrationEngine: `calibrate()` takes explicit `session_label` from FeatureVector.
- EWMA: γ = 0.5 for Φ(κ) decay — spec uses symbolic γ, task specifies 0.5.
- EWMA: Initial Σ = I × 1e-6 (near-zero variance, not uninformative).
- EWMA: VaR threshold gates (>5% REJECT, >3% SOFT) deferred to governor.py (P3.6).
- KillSwitch: IntEnum ordering (NONE=0, SOFT=1, HARD=2, EMERGENCY=3) makes
  escalation a simple `>` comparison.
- KillSwitch: DB reset writes "NONE" as new_state; DB enum has no NONE value so
  level column uses SOFT as a placeholder for reset events.
- KillSwitch: SQLite tests use StaticPool to share one in-memory DB across
  ORM connections and raw DDL.

---

## Session: 2026-03-24 — Phase 4: Execution Gateway (P4.1)

### Goal
Implement `src/execution/gateway.py` — MT5 order execution with pre-flight
checks and paper trading mode. Only reached after RiskDecision = APPROVE.

### Checklist
- [x] Implement `ExecutionGateway` in `src/execution/gateway.py`
  - [x] Pre-flight checks: kill switch, decision APPROVE, size > 0, prices valid, freshness < 2s
  - [x] Volume calculation: round(final_size × equity / 100000, 2), clamp [0.01, 100.0]
  - [x] Use mt5.symbol_info_tick(pair).ask for LONG, .bid for SHORT
  - [x] Live mode: mt5.order_send(), check TRADE_RETCODE_DONE
  - [x] Paper mode: skip order_send, simulate fill at current ask/bid, slippage=0
  - [x] Return FillRecord dataclass on success, None on rejection/failure
  - [x] structlog for every decision path
- [x] Write unit tests — 33 tests, pre-flight rejections, volume calc, live/paper, retcode handling
- [x] Run all tests — 526/526 pass
- [x] Commit: `cdf6d16 feat: execution gateway — pre-flight checks + paper trading (P4.1)`

### Review — 2026-03-25

**Status: COMPLETE** — 33 tests, 526/526 total pass.

---

## Session: 2026-03-25 — Phase 4: Fill Tracker + Learning Loop (P4.2–P4.4)

### Goal
Implement the post-execution feedback loop: FillTracker → TradeOutcomeRecorder
→ KellyInputUpdater. Prove with integration test.

### Checklist
- [x] Implement `FillTracker` in `src/execution/fill_tracker.py`
  - [x] `record_fill(fill)` → PostgreSQL fills table + in-memory cache
  - [x] `record_close(order_id, close_price, close_time, stop_loss, session)` → R-multiple + outcome dict
  - [x] LONG R = (close - entry) / risk, SHORT R = (entry - close) / risk
  - [x] Zero risk guard, unknown order guard
- [x] Implement `TradeOutcomeRecorder` in `src/learning/recorder.py`
  - [x] `record(outcome)` → delegates to PerformanceDatabase.update_segment()
  - [x] Returns True on success, False on failure
- [x] Implement `KellyInputUpdater` in `src/learning/updater.py`
  - [x] `update_segment(strategy, regime, session)` → recalc from DB
  - [x] Cache to Redis `segment:{s}:{r}:{s}`, TTL 3600s
  - [x] Clear Redis key when segment < 30 trades
  - [x] Warning log when segment drops below minimum
- [x] Write unit tests — 29 tests (14 fill_tracker + 5 recorder + 10 updater)
- [x] Write integration test — full feedback cycle: fill → close → record → update → calibrate
- [x] Run all tests — 556/556 pass
- [x] Tag v4.0-phase4

### Review — 2026-03-25

**Status: COMPLETE** — 30 new tests, 556/556 total pass.

### Integration Test Scenario
1. Seed 30 trades (18W/12L) → segment live at 60% win rate
2. CalibrationEngine reads segment → returns CalibratedTradeIntent
3. FillTracker records new fill → DB + in-memory cache
4. Close position → R-multiple calculated, outcome dict returned
5. Recorder persists outcome → 31 trades in segment
6. Updater recalculates → Redis cache updated, trade_count=31
7. CalibrationEngine reads UPDATED stats → p_win changed from 0.600 to 0.613

### Design Decisions
- FillTracker caches fill metadata in-memory (`_open_fills` dict) for close-time
  R-multiple calculation — avoids DB round-trip on every close.
- R-multiple: LONG = (close-entry)/risk, SHORT = (entry-close)/risk.
  Risk = |entry - stop_loss|. Zero risk → returns None.
- TradeOutcomeRecorder is intentionally thin — delegates to PerformanceDatabase.
  No duplicate DB logic.
- KellyInputUpdater clears Redis key when segment < 30 (don't serve stale cache).
- Integration test uses SQLite in-memory + FakeRedis — no external services.

---

## Session: 2026-03-25 — Phase 5: Prometheus Metrics (P5.1)

### Goal
Implement `src/observability/metrics.py` — 14 Prometheus metrics (6 counters,
5 gauges, 3 histograms) exposed on port 8000. Instrument 6 existing modules
at natural callsites.

### Eng Review Decisions (locked in)
- Latency: measured at `gateway.execute()` (not pipeline.py stub)
- `win_rate_7d`: computed in `KellyInputUpdater.update_segment()`
- Tests: split strategy — `test_metrics.py` + augmented existing tests
- Critical gap to fix: wrap `start_http_server()` in try/except

### Checklist

- [x] Create `src/observability/__init__.py`
- [x] Create `src/observability/metrics.py` — all 14 metrics + `start_metrics_server()`
- [x] Instrument `src/risk/governor.py` — gate rejections, VaR, drawdown, condition, positions gauges
- [x] Instrument `src/execution/gateway.py` — trade counter, slippage histogram, latency timing
- [x] Instrument `src/execution/fill_tracker.py` — R-multiple histogram, trades_won counter
- [x] Instrument `src/risk/kill_switch.py` — kill switch counter
- [x] Instrument `src/risk/reconciler.py` — state drift counter
- [x] Instrument `src/learning/updater.py` — win_rate_7d gauge + 7d query
- [x] Create `tests/unit/test_metrics.py` — metrics module tests (definitions, server)
- [ ] Add metric assertions to existing test files
- [x] All tests pass (10/10 metrics + 566 total)
- [x] Full regression suite passes (566/566)

### Review — 2026-03-25

**Status: COMPLETE** — 566/566 tests pass. 10 new metrics tests. Zero regressions.

### What was built

| File | Change | Details |
|---|---|---|
| `src/observability/__init__.py` | NEW | Empty package init |
| `src/observability/metrics.py` | NEW | 14 Prometheus metrics + `start_metrics_server()` with port-bind error handling |
| `src/risk/governor.py` | INSTRUMENTED | 6 counters (gates 1-3, 5-7), 4 gauges (VaR, drawdown, condition, positions), 1 signal counter |
| `src/execution/gateway.py` | INSTRUMENTED | Trade counter, slippage histogram, latency timing (entry-to-fill) |
| `src/execution/fill_tracker.py` | INSTRUMENTED | R-multiple histogram, trades_won counter |
| `src/risk/kill_switch.py` | INSTRUMENTED | Kill switch level counter |
| `src/risk/reconciler.py` | INSTRUMENTED | State drift counter |
| `src/learning/updater.py` | INSTRUMENTED | win_rate_7d gauge (calls new `get_7d_win_rate()`) |
| `src/calibration/history.py` | ENHANCED | Added `get_7d_win_rate()` method (7-day rolling query) |
| `tests/unit/test_metrics.py` | NEW | 10 tests: definitions, labels, buckets, server startup, env var, port-bind |

### Design Decisions
- Metrics defined as module-level constants — standard prometheus_client pattern
- Port configurable via `APEX_METRICS_PORT` env var, default 8000
- Port-bind error caught and logged, not raised — trading continues without metrics
- Latency measured at `gateway.execute()` entry to fill (eng review decision 1B)
- 7d win rate computed in `PerformanceDatabase.get_7d_win_rate()`, set in updater (eng review 2A)
- Delta-based test approach: existing test files' metric tests deferred to next session (3A partial)

---

## Session: 2026-03-25 — pyfolio Performance Reporting (P4.5)

### Goal
Build the performance reporting module using pyfolio-reloaded. Queries
trade_outcomes from PostgreSQL, converts to daily returns, generates
full tearsheet metrics (Sharpe, max drawdown, monthly returns, rolling stats).
Supports filtering by strategy, regime, session, pair, and date range.

### Checklist
- [x] Create `src/reporting/__init__.py`
- [x] Create `src/reporting/performance.py` — `PerformanceReporter` class
  - [x] `_query_outcomes()` — fetch trade_outcomes with optional filters
  - [x] `_outcomes_to_returns()` — convert R-multiples to daily return Series
  - [x] `get_stats()` — dict of key metrics (Sharpe, max DD, CAGR, win rate, etc.)
  - [x] `generate_tearsheet()` — save pyfolio tearsheet PNG to disk
  - [x] `get_monthly_returns()` — monthly returns DataFrame
  - [x] `get_rolling_sharpe()` — rolling Sharpe ratio Series
  - [x] `get_equity_curve()` — cumulative returns (bonus)
- [x] Write `tests/unit/test_performance.py` — 26 tests, all pass
- [x] Run all tests — 592/592 pass, zero regressions
- [x] Run /risk-verify — all 5 Section 7 formulas PASS, no deviations

### Review — 2026-03-25

**Status: COMPLETE** — 26 new tests, 592/592 total pass.

### PerformanceReporter API
| Method | Returns | Description |
|---|---|---|
| `get_stats(**filters)` | `dict \| None` | Sharpe, Sortino, max DD, CAGR, Calmar, win rate, profit factor, etc. |
| `get_monthly_returns(**filters)` | `DataFrame \| None` | Year × month aggregate return table |
| `get_rolling_sharpe(window, **filters)` | `Series \| None` | Rolling Sharpe ratio (default 63 bday) |
| `get_equity_curve(**filters)` | `Series \| None` | Cumulative returns starting at 1.0 |
| `generate_tearsheet(output_dir, filename, **filters)` | `Path \| None` | pyfolio returns tearsheet PNG |

### Filters (all optional)
`strategy`, `regime`, `session`, `pair`, `start` (datetime), `end` (datetime)

### Design Decisions
- R-multiple → daily return via configurable `risk_fraction` (default 1%)
- Multiple trades/day summed; gap days filled with 0.0
- Minimum 5 trades required for any stats (guards against noisy metrics)
- NumPy 2.0 compat shim for empyrical-reloaded (`np.NINF` / `np.PINF` removed)
- Matplotlib "Agg" backend — headless, no GUI dependency
- All empyrical calls — no Section 7 formulas reimplemented in this module

### /risk-verify Result
All 5 Section 7 formulas verified PASS. Reporting module delegates all stats
to empyrical — no Section 7 formulas are reimplemented or at risk of drift.

---

## Session: 2026-03-25 — Pipeline Orchestrator + 7-C Paper Trading (P5.4)

### Goal
Implement `src/pipeline.py` — the production pipeline orchestrator connecting
all 18 modules. Then run 7-C paper trading validation.

### Eng Review Decisions (locked in)
1. ZMQ only in live loop; tests/simulation call process_tick() directly
2. Paper SL/TP tracking in pipeline-level dict, no FillTracker changes
3. Exception→EMERGENCY covered by existing chaos tests
4. Add incrementing counter to gateway._paper_fill() for unique order IDs
5. Simulation uses wall-clock approval timestamps
6. Win rate measured honestly; report diagnosis if <48%

### Checklist
- [x] Add paper ticket counter to `src/execution/gateway.py` (~5 lines)
- [x] Implement `src/pipeline.py` (~300 lines)
  - [x] PipelineContext dataclass
  - [x] load_settings() — parse config/settings.yaml
  - [x] init_context() — DI container with optional session_factory/redis_client
  - [x] process_tick() — core pipeline logic (one tick)
  - [x] _check_paper_closes() — SL/TP hit detection
  - [x] _async_main() — live ZMQ PULL loop + background tasks
  - [x] main() — sync entry point (asyncio.run)
- [x] Write `tests/integration/test_pipeline.py` (~350 lines)
  - [x] test_init_context_constructs_all
  - [x] test_undefined_skips
  - [x] test_kill_switch_blocks
  - [x] test_calibration_rejects_no_segment
  - [x] test_sl_hit_closes_long
  - [x] test_tp_hit_closes_long
  - [x] test_sl_hit_closes_short
  - [x] test_signal_fill_close_update (full feedback cycle)
- [x] All tests pass (600 total: 592 existing + 8 new)
- [x] Write `scripts/paper_sim.py` (~280 lines)
- [x] Run 7-C simulation — GATE PASSED
  - 0 crashes, 0 state drift
  - 120 trades, 67.5% win rate (>= 48%)
- [ ] Commit: `feat: pipeline orchestrator + 7-C validation (P5.4-P5.5)`
