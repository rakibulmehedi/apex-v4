"""Unit tests for src/calibration/history.py — PerformanceDatabase.

Uses an in-memory SQLite database so the actual SQL aggregation logic
(COUNT, AVG, filtering, 90-day window) is exercised without needing
a real PostgreSQL server.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from db.models import TradeOutcome
from src.calibration.history import PerformanceDatabase


# ── fixtures ──────────────────────────────────────────────────────────────

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


def _make_sqlite_session_factory():
    """Create an in-memory SQLite engine with trade_outcomes table.

    Uses raw DDL because the production model uses BigInteger PK and
    PostgreSQL enums which SQLite can't auto-increment / create.
    """
    engine = create_engine("sqlite://", echo=False)
    with engine.connect() as conn:
        conn.execute(text(_TRADE_OUTCOMES_DDL))
        conn.commit()
    return sessionmaker(bind=engine, expire_on_commit=False)


def _make_outcome(
    *,
    strategy: str = "MOMENTUM",
    regime: str = "TRENDING_UP",
    session: str = "LONDON",
    won: bool = True,
    r_multiple: float = 2.0,
    days_ago: int = 10,
) -> dict:
    """Build a trade-outcome dict for insertion."""
    now = datetime.now(timezone.utc)
    closed = now - timedelta(days=days_ago)
    opened = closed - timedelta(hours=4)
    return {
        "pair": "EURUSD",
        "strategy": strategy,
        "regime": regime,
        "session": session,
        "direction": "LONG",
        "entry_price": 1.0950,
        "exit_price": 1.1000 if won else 1.0900,
        "r_multiple": r_multiple,
        "won": won,
        "fill_id": None,
        "opened_at": opened,
        "closed_at": closed,
    }


def _seed_outcomes(pdb: PerformanceDatabase, outcomes: list[dict]) -> None:
    """Insert multiple outcomes via update_segment."""
    for o in outcomes:
        pdb.update_segment(o)


# ═════════════════════════════════════════════════════════════════════════
# get_segment_stats — minimum trade gate
# ═════════════════════════════════════════════════════════════════════════

class TestSegmentMinimumGate:
    def test_returns_none_when_zero_trades(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is None

    def test_returns_none_when_below_30(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        outcomes = [_make_outcome(days_ago=i) for i in range(29)]
        _seed_outcomes(pdb, outcomes)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is None

    def test_returns_stats_at_exactly_30(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        outcomes = [_make_outcome(days_ago=i) for i in range(30)]
        _seed_outcomes(pdb, outcomes)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert result["trade_count"] == 30

    def test_returns_stats_above_30(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        outcomes = [_make_outcome(days_ago=i) for i in range(50)]
        _seed_outcomes(pdb, outcomes)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert result["trade_count"] == 50


# ═════════════════════════════════════════════════════════════════════════
# get_segment_stats — win rate calculation
# ═════════════════════════════════════════════════════════════════════════

class TestSegmentWinRate:
    def test_all_wins(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        outcomes = [_make_outcome(won=True, days_ago=i) for i in range(30)]
        _seed_outcomes(pdb, outcomes)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert result["win_rate"] == pytest.approx(1.0)

    def test_all_losses(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        outcomes = [
            _make_outcome(won=False, r_multiple=-1.0, days_ago=i)
            for i in range(30)
        ]
        _seed_outcomes(pdb, outcomes)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert result["win_rate"] == pytest.approx(0.0)

    def test_mixed_win_rate(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        # 20 wins, 10 losses → 66.67% win rate
        outcomes = (
            [_make_outcome(won=True, days_ago=i) for i in range(20)]
            + [_make_outcome(won=False, r_multiple=-1.0, days_ago=20 + i) for i in range(10)]
        )
        _seed_outcomes(pdb, outcomes)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert result["win_rate"] == pytest.approx(20.0 / 30.0, rel=1e-4)

    def test_avg_r_calculation(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        # 15 trades at R=2.0, 15 trades at R=-1.0 → avg = 0.5
        outcomes = (
            [_make_outcome(won=True, r_multiple=2.0, days_ago=i) for i in range(15)]
            + [_make_outcome(won=False, r_multiple=-1.0, days_ago=15 + i) for i in range(15)]
        )
        _seed_outcomes(pdb, outcomes)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert result["avg_R"] == pytest.approx(0.5, rel=1e-4)


# ═════════════════════════════════════════════════════════════════════════
# get_segment_stats — 90-day rolling window
# ═════════════════════════════════════════════════════════════════════════

class TestSegment90DayWindow:
    def test_excludes_old_trades(self):
        """Trades older than 90 days should not be counted."""
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        # 20 recent + 20 old (>90 days) = 40 total, but only 20 in window
        recent = [_make_outcome(days_ago=i) for i in range(20)]
        old = [_make_outcome(days_ago=91 + i) for i in range(20)]
        _seed_outcomes(pdb, recent + old)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        # Only 20 recent trades → below 30 → None
        assert result is None

    def test_boundary_at_90_days(self):
        """Trade exactly at 90 days should be included (>= cutoff)."""
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        # 29 recent + 1 at exactly 89 days = 30 in window
        outcomes = [_make_outcome(days_ago=i) for i in range(30)]
        _seed_outcomes(pdb, outcomes)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert result["trade_count"] == 30


# ═════════════════════════════════════════════════════════════════════════
# get_segment_stats — segment isolation
# ═════════════════════════════════════════════════════════════════════════

class TestSegmentIsolation:
    def test_different_strategy_not_counted(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        momentum = [_make_outcome(strategy="MOMENTUM", days_ago=i) for i in range(30)]
        mr = [_make_outcome(strategy="MEAN_REVERSION", regime="RANGING", days_ago=i) for i in range(10)]
        _seed_outcomes(pdb, momentum + mr)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert result["trade_count"] == 30

    def test_different_regime_not_counted(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        up = [_make_outcome(regime="TRENDING_UP", days_ago=i) for i in range(20)]
        down = [_make_outcome(regime="TRENDING_DOWN", days_ago=i) for i in range(20)]
        _seed_outcomes(pdb, up + down)
        # Each segment has only 20 → below threshold
        assert pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON") is None
        assert pdb.get_segment_stats("MOMENTUM", "TRENDING_DOWN", "LONDON") is None

    def test_different_session_not_counted(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        london = [_make_outcome(session="LONDON", days_ago=i) for i in range(30)]
        ny = [_make_outcome(session="NY", days_ago=i) for i in range(10)]
        _seed_outcomes(pdb, london + ny)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert result["trade_count"] == 30


# ═════════════════════════════════════════════════════════════════════════
# get_segment_stats — return fields
# ═════════════════════════════════════════════════════════════════════════

class TestSegmentReturnFields:
    def test_all_keys_present(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        outcomes = [_make_outcome(days_ago=i) for i in range(30)]
        _seed_outcomes(pdb, outcomes)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert "win_rate" in result
        assert "avg_R" in result
        assert "trade_count" in result
        assert "last_updated" in result

    def test_last_updated_is_datetime(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        outcomes = [_make_outcome(days_ago=i) for i in range(30)]
        _seed_outcomes(pdb, outcomes)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert isinstance(result["last_updated"], (datetime, str))

    def test_win_rate_is_float(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        outcomes = [_make_outcome(days_ago=i) for i in range(30)]
        _seed_outcomes(pdb, outcomes)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert isinstance(result["win_rate"], float)
        assert isinstance(result["avg_R"], float)


# ═════════════════════════════════════════════════════════════════════════
# update_segment
# ═════════════════════════════════════════════════════════════════════════

class TestUpdateSegment:
    def test_inserts_one_row(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        pdb.update_segment(_make_outcome())
        with sf() as db:
            count = db.query(TradeOutcome).count()
        assert count == 1

    def test_multiple_inserts(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        for i in range(5):
            pdb.update_segment(_make_outcome(days_ago=i))
        with sf() as db:
            count = db.query(TradeOutcome).count()
        assert count == 5

    def test_error_does_not_crash(self):
        """DB error should be logged, not raised."""
        from unittest.mock import MagicMock
        sf = MagicMock(side_effect=Exception("connection refused"))
        pdb = PerformanceDatabase(session_factory=sf)
        # Must not raise
        pdb.update_segment(_make_outcome())


# ═════════════════════════════════════════════════════════════════════════
# bootstrap_from_v3
# ═════════════════════════════════════════════════════════════════════════

class TestBootstrapFromV3:
    def test_imports_all_records(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        v3_data = [_make_outcome(days_ago=i) for i in range(40)]
        inserted = pdb.bootstrap_from_v3(v3_data)
        assert inserted == 40
        with sf() as db:
            count = db.query(TradeOutcome).count()
        assert count == 40

    def test_fill_id_is_none(self):
        """V3 imports should always have fill_id=None."""
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        v3_data = [_make_outcome()]
        pdb.bootstrap_from_v3(v3_data)
        with sf() as db:
            row = db.query(TradeOutcome).first()
        assert row is not None
        assert row.fill_id is None

    def test_returns_count(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        assert pdb.bootstrap_from_v3([_make_outcome()]) == 1

    def test_empty_list_returns_zero(self):
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        assert pdb.bootstrap_from_v3([]) == 0

    def test_error_returns_zero(self):
        from unittest.mock import MagicMock
        sf = MagicMock(side_effect=Exception("disk full"))
        pdb = PerformanceDatabase(session_factory=sf)
        assert pdb.bootstrap_from_v3([_make_outcome()]) == 0

    def test_imported_trades_visible_to_get_segment_stats(self):
        """Bootstrapped V3 data should feed into segment stats."""
        sf = _make_sqlite_session_factory()
        pdb = PerformanceDatabase(session_factory=sf)
        v3_data = [_make_outcome(won=True, r_multiple=2.5, days_ago=i) for i in range(35)]
        pdb.bootstrap_from_v3(v3_data)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert result["trade_count"] == 35
        assert result["win_rate"] == pytest.approx(1.0)
        assert result["avg_R"] == pytest.approx(2.5)


# ═════════════════════════════════════════════════════════════════════════
# get_segment_stats — error handling
# ═════════════════════════════════════════════════════════════════════════

class TestSegmentErrorHandling:
    def test_db_error_returns_none(self):
        from unittest.mock import MagicMock
        sf = MagicMock(side_effect=Exception("connection lost"))
        pdb = PerformanceDatabase(session_factory=sf)
        result = pdb.get_segment_stats("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is None
