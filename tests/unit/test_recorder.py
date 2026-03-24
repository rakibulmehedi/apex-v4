"""Unit tests for src/learning/recorder.py — TradeOutcomeRecorder.

Tests cover:
  - record: inserts into trade_outcomes via PerformanceDatabase
  - record: returns True on success
  - record: returns False on failure
  - record: passes all outcome fields through
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.learning.recorder import TradeOutcomeRecorder


# ── fixtures ──────────────────────────────────────────────────────────────

def _make_outcome() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "pair": "EURUSD",
        "strategy": "MOMENTUM",
        "regime": "TRENDING_UP",
        "session": "LONDON",
        "direction": "LONG",
        "entry_price": 1.0800,
        "exit_price": 1.0900,
        "r_multiple": 2.0,
        "won": True,
        "fill_id": 42,
        "opened_at": now - timedelta(hours=4),
        "closed_at": now,
    }


# ── tests ─────────────────────────────────────────────────────────────────


class TestRecorderSuccess:
    """record() delegates to PerformanceDatabase.update_segment."""

    def test_returns_true(self):
        mock_db = MagicMock()
        recorder = TradeOutcomeRecorder(perf_db=mock_db)
        assert recorder.record(_make_outcome()) is True

    def test_calls_update_segment(self):
        mock_db = MagicMock()
        recorder = TradeOutcomeRecorder(perf_db=mock_db)
        outcome = _make_outcome()
        recorder.record(outcome)
        mock_db.update_segment.assert_called_once_with(outcome)

    def test_passes_all_fields(self):
        mock_db = MagicMock()
        recorder = TradeOutcomeRecorder(perf_db=mock_db)
        outcome = _make_outcome()
        recorder.record(outcome)
        passed = mock_db.update_segment.call_args[0][0]
        assert passed["pair"] == "EURUSD"
        assert passed["r_multiple"] == 2.0
        assert passed["fill_id"] == 42


class TestRecorderFailure:
    """record() catches exceptions and returns False."""

    def test_returns_false_on_exception(self):
        mock_db = MagicMock()
        mock_db.update_segment.side_effect = RuntimeError("DB down")
        recorder = TradeOutcomeRecorder(perf_db=mock_db)
        assert recorder.record(_make_outcome()) is False

    def test_multiple_records(self):
        mock_db = MagicMock()
        recorder = TradeOutcomeRecorder(perf_db=mock_db)
        assert recorder.record(_make_outcome()) is True
        assert recorder.record(_make_outcome()) is True
        assert mock_db.update_segment.call_count == 2
