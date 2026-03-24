"""Integration test: full feedback cycle.

Proves end-to-end:
  fill → FillTracker → TradeOutcomeRecorder → KellyInputUpdater → CalibrationEngine

Scenario:
  1. Seed 30 trades into a segment (minimum for calibration)
  2. CalibrationEngine reads segment → returns CalibratedTradeIntent
  3. Simulate a new fill via FillTracker
  4. Close the position → FillTracker returns outcome dict
  5. TradeOutcomeRecorder persists outcome → now 31 trades
  6. KellyInputUpdater recalculates segment → caches to Redis
  7. CalibrationEngine reads UPDATED stats (win_rate changed)

Uses SQLite in-memory DB + mock Redis — no external services required.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.calibration.engine import CalibrationEngine
from src.calibration.history import PerformanceDatabase
from src.execution.fill_tracker import FillTracker
from src.execution.gateway import FillRecord
from src.learning.recorder import TradeOutcomeRecorder
from src.learning.updater import KellyInputUpdater
from src.market.schemas import (
    AlphaHypothesis,
    Direction,
    Regime,
    Strategy,
    TradingSession,
)


# ── SQLite setup ─────────────────────────────────────────────────────────

_TRADE_OUTCOMES_DDL = """
CREATE TABLE trade_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair VARCHAR(6) NOT NULL,
    strategy VARCHAR(20) NOT NULL,
    regime VARCHAR(20) NOT NULL,
    session VARCHAR(20) NOT NULL,
    direction VARCHAR(10) NOT NULL,
    entry_price FLOAT NOT NULL,
    exit_price FLOAT NOT NULL,
    r_multiple FLOAT NOT NULL,
    won BOOLEAN NOT NULL,
    fill_id BIGINT,
    opened_at DATETIME NOT NULL,
    closed_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

_FILLS_DDL = """
CREATE TABLE fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id BIGINT NOT NULL,
    pair VARCHAR(6) NOT NULL,
    direction VARCHAR(10) NOT NULL,
    strategy VARCHAR(20) NOT NULL,
    regime VARCHAR(20) NOT NULL,
    requested_size FLOAT NOT NULL,
    actual_size FLOAT NOT NULL,
    requested_price FLOAT NOT NULL,
    actual_fill_price FLOAT NOT NULL,
    slippage_points FLOAT NOT NULL,
    filled_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""


def _make_sqlite_sf():
    """In-memory SQLite with both fills and trade_outcomes tables."""
    engine = create_engine("sqlite://", echo=False)
    with engine.connect() as conn:
        conn.execute(text(_TRADE_OUTCOMES_DDL))
        conn.execute(text(_FILLS_DDL))
        conn.commit()
    return sessionmaker(bind=engine, expire_on_commit=False)


# ── Fake Redis (dict-backed) ─────────────────────────────────────────────

class FakeRedis:
    """Minimal Redis mock using a dict."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = value

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)


# ── helpers ──────────────────────────────────────────────────────────────

def _seed_trades(
    sf,
    count: int = 30,
    win_count: int = 18,
) -> None:
    """Insert *count* trades: *win_count* wins, rest losses."""
    now = datetime.now(timezone.utc)
    for i in range(count):
        won = i < win_count
        outcome = {
            "pair": "EURUSD",
            "strategy": "MOMENTUM",
            "regime": "TRENDING_UP",
            "session": "LONDON",
            "direction": "LONG",
            "entry_price": 1.0800,
            "exit_price": 1.0900 if won else 1.0700,
            "r_multiple": 2.0 if won else -1.0,
            "won": won,
            "fill_id": None,
            "opened_at": now - timedelta(days=30, hours=i),
            "closed_at": now - timedelta(days=30, hours=i - 4),
        }
        perf_db = PerformanceDatabase(session_factory=sf)
        perf_db.update_segment(outcome)


# ── integration test ─────────────────────────────────────────────────────

class TestFeedbackLoop:
    """Full feedback cycle: fill → close → record → update → calibrate."""

    def test_end_to_end_feedback_cycle(self):
        # ── Setup ────────────────────────────────────────────────────
        sf = _make_sqlite_sf()
        fake_redis = FakeRedis()

        perf_db = PerformanceDatabase(session_factory=sf)
        fill_tracker = FillTracker(session_factory=sf)
        recorder = TradeOutcomeRecorder(perf_db=perf_db)
        updater = KellyInputUpdater(perf_db=perf_db, redis_client=fake_redis)
        cal_engine = CalibrationEngine(perf_db=perf_db)

        # ── Step 1: Seed 30 trades (18 wins, 12 losses) ─────────────
        _seed_trades(sf, count=30, win_count=18)

        # Verify segment is live.
        stats_before = perf_db.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert stats_before is not None
        assert stats_before["trade_count"] == 30
        # win_rate = 18/30 = 0.6
        assert abs(stats_before["win_rate"] - 0.6) < 0.01

        # ── Step 2: CalibrationEngine reads segment → returns intent ─
        hypothesis = AlphaHypothesis(
            strategy=Strategy.MOMENTUM,
            pair="EURUSD",
            direction=Direction.LONG,
            entry_zone=(1.0840, 1.0850),
            stop_loss=1.0800,
            take_profit=1.0950,
            setup_score=20,
            expected_R=2.5,
            regime=Regime.TRENDING_UP,
        )

        intent_before = cal_engine.calibrate(
            hypothesis=hypothesis,
            session_label="LONDON",
            current_dd=0.01,
        )
        assert intent_before is not None
        assert intent_before.p_win == stats_before["win_rate"]

        # ── Step 3: Simulate a new fill ──────────────────────────────
        fill = FillRecord(
            order_id=500_001,
            pair="EURUSD",
            direction="LONG",
            strategy="MOMENTUM",
            regime="TRENDING_UP",
            requested_price=1.0845,
            fill_price=1.0845,
            requested_volume=0.01,
            filled_volume=0.01,
            slippage_points=0.0,
            is_paper=True,
            filled_at_ms=int(time.time() * 1000),
        )
        fill_id = fill_tracker.record_fill(fill)
        assert fill_id is not None

        # ── Step 4: Close the position → outcome dict ────────────────
        outcome = fill_tracker.record_close(
            order_id=500_001,
            close_price=1.0900,
            close_time_ms=int(time.time() * 1000),
            stop_loss=1.0800,
            session_label="LONDON",
        )
        assert outcome is not None
        # R = (1.09 - 1.0845) / |1.0845 - 1.08| = 0.0055 / 0.0045 ≈ 1.222
        assert outcome["won"] is True
        assert outcome["r_multiple"] > 0

        # ── Step 5: Recorder persists outcome → 31 trades ────────────
        ok = recorder.record(outcome)
        assert ok is True

        # ── Step 6: Updater recalculates segment → caches to Redis ───
        updated_stats = updater.update_segment("MOMENTUM", "TRENDING_UP", "LONDON")
        assert updated_stats is not None
        assert updated_stats["trade_count"] == 31

        # Verify Redis cache exists.
        cached = fake_redis.get("segment:MOMENTUM:TRENDING_UP:LONDON")
        assert cached is not None
        cached_data = json.loads(cached)
        assert cached_data["trade_count"] == 31

        # ── Step 7: CalibrationEngine reads UPDATED stats ────────────
        intent_after = cal_engine.calibrate(
            hypothesis=hypothesis,
            session_label="LONDON",
            current_dd=0.01,
        )
        assert intent_after is not None
        assert intent_after.segment_count == 31

        # Win rate changed: was 18/30 = 0.600, now 19/31 ≈ 0.6129
        assert intent_after.p_win != intent_before.p_win
        assert abs(intent_after.p_win - 19 / 31) < 0.01
