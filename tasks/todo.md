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
  - [x] ZMQ PUSH to `ipc:///tmp/apex_market.ipc`
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
