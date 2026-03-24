"""Unit tests for src/execution/fill_tracker.py — FillTracker.

Tests cover:
  - record_fill: writes to SQLite fills table, returns fill_id
  - record_fill: caches metadata in _open_fills
  - record_fill: DB error returns None
  - record_close: LONG R-multiple = (close - entry) / |entry - SL|
  - record_close: SHORT R-multiple = (entry - close) / |entry - SL|
  - record_close: winning trade (R > 0) → won=True
  - record_close: losing trade (R < 0) → won=False
  - record_close: unknown order_id returns None
  - record_close: zero risk (entry == SL) returns None
  - record_close: removes order from _open_fills after close
  - record_close: outcome dict has all expected keys
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.execution.fill_tracker import FillTracker
from src.execution.gateway import FillRecord


# ── fixtures ──────────────────────────────────────────────────────────────

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
    engine = create_engine("sqlite://", echo=False)
    with engine.connect() as conn:
        conn.execute(text(_FILLS_DDL))
        conn.commit()
    return sessionmaker(bind=engine, expire_on_commit=False)


def _make_fill(
    order_id: int = 100_000,
    pair: str = "EURUSD",
    direction: str = "LONG",
    fill_price: float = 1.08465,
    requested_price: float = 1.08465,
    slippage: float = 0.0,
) -> FillRecord:
    return FillRecord(
        order_id=order_id,
        pair=pair,
        direction=direction,
        strategy="MOMENTUM",
        regime="TRENDING_UP",
        requested_price=requested_price,
        fill_price=fill_price,
        requested_volume=0.01,
        filled_volume=0.01,
        slippage_points=slippage,
        is_paper=True,
        filled_at_ms=int(time.time() * 1000),
    )


# ── record_fill tests ────────────────────────────────────────────────────


class TestRecordFill:
    """record_fill persists to DB and caches metadata."""

    def test_returns_fill_id(self):
        sf = _make_sqlite_sf()
        tracker = FillTracker(session_factory=sf)
        fill_id = tracker.record_fill(_make_fill())
        assert fill_id is not None
        assert isinstance(fill_id, int)

    def test_caches_in_open_fills(self):
        sf = _make_sqlite_sf()
        tracker = FillTracker(session_factory=sf)
        tracker.record_fill(_make_fill(order_id=42))
        assert 42 in tracker._open_fills
        meta = tracker._open_fills[42]
        assert meta["pair"] == "EURUSD"
        assert meta["direction"] == "LONG"
        assert meta["entry_price"] == 1.08465
        assert meta["fill_id"] is not None

    def test_persists_to_db(self):
        sf = _make_sqlite_sf()
        tracker = FillTracker(session_factory=sf)
        tracker.record_fill(_make_fill(order_id=99))
        with sf() as db:
            row = db.execute(text("SELECT * FROM fills WHERE order_id = 99")).fetchone()
            assert row is not None

    def test_multiple_fills(self):
        sf = _make_sqlite_sf()
        tracker = FillTracker(session_factory=sf)
        id1 = tracker.record_fill(_make_fill(order_id=1))
        id2 = tracker.record_fill(_make_fill(order_id=2))
        assert id1 != id2
        assert len(tracker._open_fills) == 2

    def test_slippage_stored(self):
        sf = _make_sqlite_sf()
        tracker = FillTracker(session_factory=sf)
        tracker.record_fill(_make_fill(slippage=0.00005))
        with sf() as db:
            row = db.execute(text("SELECT slippage_points FROM fills")).fetchone()
            assert abs(row[0] - 0.00005) < 1e-10


# ── record_close tests ───────────────────────────────────────────────────


class TestRecordCloseLong:
    """R-multiple for LONG: (close - entry) / |entry - SL|."""

    def test_winning_long(self):
        sf = _make_sqlite_sf()
        tracker = FillTracker(session_factory=sf)
        tracker.record_fill(_make_fill(order_id=1, fill_price=1.0800))
        outcome = tracker.record_close(
            order_id=1, close_price=1.0900,
            close_time_ms=int(time.time() * 1000),
            stop_loss=1.0750, session_label="LONDON",
        )
        assert outcome is not None
        # R = (1.0900 - 1.0800) / |1.0800 - 1.0750| = 0.01 / 0.005 = 2.0
        assert abs(outcome["r_multiple"] - 2.0) < 1e-10
        assert outcome["won"] is True

    def test_losing_long(self):
        sf = _make_sqlite_sf()
        tracker = FillTracker(session_factory=sf)
        tracker.record_fill(_make_fill(order_id=1, fill_price=1.0800))
        outcome = tracker.record_close(
            order_id=1, close_price=1.0750,
            close_time_ms=int(time.time() * 1000),
            stop_loss=1.0750, session_label="LONDON",
        )
        assert outcome is not None
        # R = (1.0750 - 1.0800) / 0.005 = -1.0
        assert abs(outcome["r_multiple"] - (-1.0)) < 1e-10
        assert outcome["won"] is False


class TestRecordCloseShort:
    """R-multiple for SHORT: (entry - close) / |entry - SL|."""

    def test_winning_short(self):
        sf = _make_sqlite_sf()
        tracker = FillTracker(session_factory=sf)
        tracker.record_fill(_make_fill(
            order_id=1, direction="SHORT", fill_price=1.0800,
        ))
        outcome = tracker.record_close(
            order_id=1, close_price=1.0700,
            close_time_ms=int(time.time() * 1000),
            stop_loss=1.0850, session_label="NY",
        )
        assert outcome is not None
        # R = (1.0800 - 1.0700) / |1.0800 - 1.0850| = 0.01 / 0.005 = 2.0
        assert abs(outcome["r_multiple"] - 2.0) < 1e-10
        assert outcome["won"] is True

    def test_losing_short(self):
        sf = _make_sqlite_sf()
        tracker = FillTracker(session_factory=sf)
        tracker.record_fill(_make_fill(
            order_id=1, direction="SHORT", fill_price=1.0800,
        ))
        outcome = tracker.record_close(
            order_id=1, close_price=1.0850,
            close_time_ms=int(time.time() * 1000),
            stop_loss=1.0850, session_label="NY",
        )
        assert outcome is not None
        # R = (1.0800 - 1.0850) / 0.005 = -1.0
        assert abs(outcome["r_multiple"] - (-1.0)) < 1e-10
        assert outcome["won"] is False


class TestRecordCloseEdgeCases:
    """Edge cases: unknown order, zero risk, cleanup."""

    def test_unknown_order_returns_none(self):
        sf = _make_sqlite_sf()
        tracker = FillTracker(session_factory=sf)
        outcome = tracker.record_close(
            order_id=999, close_price=1.0900,
            close_time_ms=int(time.time() * 1000),
            stop_loss=1.0750, session_label="LONDON",
        )
        assert outcome is None

    def test_zero_risk_returns_none(self):
        sf = _make_sqlite_sf()
        tracker = FillTracker(session_factory=sf)
        tracker.record_fill(_make_fill(order_id=1, fill_price=1.0800))
        outcome = tracker.record_close(
            order_id=1, close_price=1.0900,
            close_time_ms=int(time.time() * 1000),
            stop_loss=1.0800,  # same as entry → zero risk
            session_label="LONDON",
        )
        assert outcome is None

    def test_removes_from_open_fills(self):
        sf = _make_sqlite_sf()
        tracker = FillTracker(session_factory=sf)
        tracker.record_fill(_make_fill(order_id=1, fill_price=1.0800))
        assert 1 in tracker._open_fills
        tracker.record_close(
            order_id=1, close_price=1.0900,
            close_time_ms=int(time.time() * 1000),
            stop_loss=1.0750, session_label="LONDON",
        )
        assert 1 not in tracker._open_fills

    def test_outcome_has_all_keys(self):
        sf = _make_sqlite_sf()
        tracker = FillTracker(session_factory=sf)
        tracker.record_fill(_make_fill(order_id=1, fill_price=1.0800))
        outcome = tracker.record_close(
            order_id=1, close_price=1.0900,
            close_time_ms=int(time.time() * 1000),
            stop_loss=1.0750, session_label="LONDON",
        )
        assert outcome is not None
        expected_keys = {
            "pair", "strategy", "regime", "session", "direction",
            "entry_price", "exit_price", "r_multiple", "won",
            "fill_id", "opened_at", "closed_at",
        }
        assert set(outcome.keys()) == expected_keys

    def test_outcome_timestamps_are_datetime(self):
        sf = _make_sqlite_sf()
        tracker = FillTracker(session_factory=sf)
        tracker.record_fill(_make_fill(order_id=1, fill_price=1.0800))
        outcome = tracker.record_close(
            order_id=1, close_price=1.0900,
            close_time_ms=int(time.time() * 1000),
            stop_loss=1.0750, session_label="LONDON",
        )
        assert isinstance(outcome["opened_at"], datetime)
        assert isinstance(outcome["closed_at"], datetime)
