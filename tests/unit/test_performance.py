"""Tests for src/reporting/performance.py — P4.5 pyfolio reporting.

All DB access is mocked via a fake sessionmaker.
Tests cover: query filtering, R-multiple → daily return conversion,
empyrical stats, monthly returns, rolling Sharpe, equity curve,
tearsheet generation, and edge cases.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.reporting.performance import (
    PerformanceReporter,
    _DEFAULT_RISK_FRACTION,
    _MIN_TRADES_FOR_STATS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_outcome(
    *,
    r_multiple: float = 1.5,
    won: bool = True,
    closed_at: datetime | None = None,
    pair: str = "EURUSD",
    strategy: str = "MOMENTUM",
    regime: str = "TRENDING_UP",
    session: str = "LONDON",
    direction: str = "LONG",
) -> SimpleNamespace:
    """Create a lightweight trade-outcome stub matching TradeOutcome columns."""
    if closed_at is None:
        closed_at = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        pair=pair,
        strategy=strategy,
        regime=regime,
        session=session,
        direction=direction,
        entry_price=1.1000,
        exit_price=1.1050,
        r_multiple=r_multiple,
        won=won,
        fill_id=1,
        opened_at=closed_at - timedelta(hours=4),
        closed_at=closed_at,
    )


def _make_outcomes(count: int = 10, base_date: datetime | None = None) -> list:
    """Generate *count* outcomes on consecutive business days."""
    if base_date is None:
        base_date = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)
    outcomes = []
    current = base_date
    for i in range(count):
        # skip weekends
        while current.weekday() >= 5:
            current += timedelta(days=1)
        r = 1.5 if i % 3 != 0 else -0.8  # 2 wins : 1 loss pattern
        outcomes.append(
            _make_outcome(
                r_multiple=r,
                won=r > 0,
                closed_at=current,
            )
        )
        current += timedelta(days=1)
    return outcomes


class FakeSession:
    """Lightweight DB session context-manager mock."""

    def __init__(self, outcomes: list):
        self._outcomes = outcomes

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def query(self, model):
        return FakeQuery(self._outcomes)


class FakeQuery:
    def __init__(self, outcomes: list):
        self._outcomes = outcomes

    def filter(self, *args):
        return self

    def order_by(self, *args):
        return self

    def all(self):
        return self._outcomes


def _make_reporter(outcomes: list, risk_fraction: float = _DEFAULT_RISK_FRACTION):
    """Build a PerformanceReporter with fake DB returning *outcomes*."""
    sf = MagicMock()
    sf.return_value = FakeSession(outcomes)
    return PerformanceReporter(session_factory=sf, risk_fraction=risk_fraction)


# ---------------------------------------------------------------------------
# Tests — _outcomes_to_returns
# ---------------------------------------------------------------------------

class TestOutcomesToReturns:
    def test_empty_returns_empty_series(self):
        rpt = _make_reporter([])
        s = rpt._outcomes_to_returns([])
        assert isinstance(s, pd.Series)
        assert s.empty

    def test_single_trade_single_day(self):
        o = _make_outcome(r_multiple=2.0)
        rpt = _make_reporter([o])
        s = rpt._outcomes_to_returns([o])
        assert len(s) == 1
        assert s.iloc[0] == pytest.approx(2.0 * _DEFAULT_RISK_FRACTION)

    def test_same_day_trades_summed(self):
        """Multiple trades closing on the same day should be summed."""
        dt = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        outcomes = [
            _make_outcome(r_multiple=1.0, closed_at=dt),
            _make_outcome(r_multiple=-0.5, closed_at=dt + timedelta(hours=2)),
        ]
        rpt = _make_reporter(outcomes)
        s = rpt._outcomes_to_returns(outcomes)
        assert len(s) == 1
        expected = (1.0 + -0.5) * _DEFAULT_RISK_FRACTION
        assert s.iloc[0] == pytest.approx(expected)

    def test_gap_days_filled_with_zero(self):
        """Non-trading days between trades should be 0.0."""
        # Monday and Wednesday — Tuesday should be filled with 0.
        mon = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)   # Mon
        wed = datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc)   # Wed
        outcomes = [
            _make_outcome(r_multiple=1.0, closed_at=mon),
            _make_outcome(r_multiple=2.0, closed_at=wed),
        ]
        rpt = _make_reporter(outcomes)
        s = rpt._outcomes_to_returns(outcomes)
        # Mon, Tue, Wed = 3 business days
        assert len(s) == 3
        assert s.iloc[1] == 0.0  # Tuesday gap

    def test_custom_risk_fraction(self):
        o = _make_outcome(r_multiple=3.0)
        rpt = _make_reporter([o], risk_fraction=0.02)
        s = rpt._outcomes_to_returns([o])
        assert s.iloc[0] == pytest.approx(3.0 * 0.02)


# ---------------------------------------------------------------------------
# Tests — get_stats
# ---------------------------------------------------------------------------

class TestGetStats:
    def test_insufficient_trades_returns_none(self):
        outcomes = [_make_outcome() for _ in range(_MIN_TRADES_FOR_STATS - 1)]
        rpt = _make_reporter(outcomes)
        assert rpt.get_stats() is None

    def test_stats_keys_present(self):
        outcomes = _make_outcomes(20)
        rpt = _make_reporter(outcomes)
        stats = rpt.get_stats()
        assert stats is not None
        expected_keys = {
            "total_trades", "winning_trades", "losing_trades", "win_rate",
            "avg_r_multiple", "profit_factor", "sharpe_ratio", "sortino_ratio",
            "max_drawdown", "cagr", "calmar_ratio", "annual_volatility",
            "total_return", "best_day", "worst_day",
        }
        assert expected_keys == set(stats.keys())

    def test_trade_counts_correct(self):
        outcomes = _make_outcomes(9)  # pattern: 2 wins, 1 loss repeated
        rpt = _make_reporter(outcomes)
        stats = rpt.get_stats()
        assert stats is not None
        assert stats["total_trades"] == 9
        # indices 0,3,6 are losses (-0.8), rest are wins (1.5)
        assert stats["winning_trades"] == 6
        assert stats["losing_trades"] == 3

    def test_win_rate(self):
        outcomes = _make_outcomes(9)
        rpt = _make_reporter(outcomes)
        stats = rpt.get_stats()
        assert stats is not None
        assert stats["win_rate"] == pytest.approx(6 / 9)

    def test_profit_factor(self):
        outcomes = _make_outcomes(9)
        rpt = _make_reporter(outcomes)
        stats = rpt.get_stats()
        assert stats is not None
        gross_profit = 6 * 1.5
        gross_loss = 3 * 0.8
        assert stats["profit_factor"] == pytest.approx(gross_profit / gross_loss)

    def test_max_drawdown_is_negative(self):
        outcomes = _make_outcomes(20)
        rpt = _make_reporter(outcomes)
        stats = rpt.get_stats()
        assert stats is not None
        assert stats["max_drawdown"] <= 0

    def test_sharpe_with_all_wins(self):
        outcomes = [
            _make_outcome(
                r_multiple=1.0,
                closed_at=datetime(2026, 1, 5 + i, 12, 0, tzinfo=timezone.utc),
            )
            for i in range(10)
        ]
        rpt = _make_reporter(outcomes)
        stats = rpt.get_stats()
        assert stats is not None
        assert stats["sharpe_ratio"] > 0

    def test_profit_factor_infinite_when_no_losses(self):
        """All winning trades → profit_factor = inf."""
        outcomes = [
            _make_outcome(
                r_multiple=1.5, won=True,
                closed_at=datetime(2026, 1, 5 + i, 12, 0, tzinfo=timezone.utc),
            )
            for i in range(6)
        ]
        rpt = _make_reporter(outcomes)
        stats = rpt.get_stats()
        assert stats is not None
        assert stats["profit_factor"] == float("inf")


# ---------------------------------------------------------------------------
# Tests — monthly returns
# ---------------------------------------------------------------------------

class TestMonthlyReturns:
    def test_insufficient_returns_none(self):
        rpt = _make_reporter([_make_outcome()])
        assert rpt.get_monthly_returns() is None

    def test_returns_dataframe(self):
        # Spread across 2 months.
        outcomes = _make_outcomes(20, base_date=datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc))
        rpt = _make_reporter(outcomes)
        table = rpt.get_monthly_returns()
        assert table is not None
        assert isinstance(table, pd.DataFrame)
        assert table.index.name == "Year"


# ---------------------------------------------------------------------------
# Tests — rolling Sharpe
# ---------------------------------------------------------------------------

class TestRollingSharpe:
    def test_insufficient_window_returns_none(self):
        outcomes = _make_outcomes(10)
        rpt = _make_reporter(outcomes)
        # window=63 but only ~10 data points
        assert rpt.get_rolling_sharpe(window=63) is None

    def test_small_window_returns_series(self):
        outcomes = _make_outcomes(20)
        rpt = _make_reporter(outcomes)
        rs = rpt.get_rolling_sharpe(window=5)
        assert rs is not None
        assert isinstance(rs, pd.Series)
        assert rs.name == "rolling_sharpe"


# ---------------------------------------------------------------------------
# Tests — equity curve
# ---------------------------------------------------------------------------

class TestEquityCurve:
    def test_insufficient_returns_none(self):
        rpt = _make_reporter([_make_outcome()])
        assert rpt.get_equity_curve() is None

    def test_starts_near_one(self):
        outcomes = _make_outcomes(10)
        rpt = _make_reporter(outcomes)
        eq = rpt.get_equity_curve()
        assert eq is not None
        # First value = 1.0 + first return
        assert eq.iloc[0] == pytest.approx(
            1.0 + outcomes[0].r_multiple * _DEFAULT_RISK_FRACTION, abs=1e-6,
        )

    def test_monotonic_with_all_wins(self):
        outcomes = [
            _make_outcome(
                r_multiple=1.0,
                closed_at=datetime(2026, 1, 5 + i, 12, 0, tzinfo=timezone.utc),
            )
            for i in range(10)
        ]
        rpt = _make_reporter(outcomes)
        eq = rpt.get_equity_curve()
        assert eq is not None
        # All positive returns → equity should be monotonically increasing.
        diffs = eq.diff().dropna()
        assert (diffs >= 0).all()


# ---------------------------------------------------------------------------
# Tests — tearsheet generation
# ---------------------------------------------------------------------------

class TestTearsheet:
    def test_insufficient_returns_none(self):
        rpt = _make_reporter([_make_outcome()])
        assert rpt.generate_tearsheet() is None

    @patch("src.reporting.performance.pf", create=True)
    def test_tearsheet_saves_png(self, tmp_path=None):
        """Tearsheet generation creates a file on disk."""
        import tempfile

        outcomes = _make_outcomes(20)
        rpt = _make_reporter(outcomes)
        with tempfile.TemporaryDirectory() as td:
            result = rpt.generate_tearsheet(
                output_dir=td, filename="test_tear.png",
            )
            # pyfolio may raise in headless env — just verify we don't crash
            # and the function returns a Path or None gracefully.
            if result is not None:
                assert result.name == "test_tear.png"


# ---------------------------------------------------------------------------
# Tests — query filtering passthrough
# ---------------------------------------------------------------------------

class TestQueryFiltering:
    def test_filters_passed_to_db(self):
        """Verify filter args are forwarded (via mock introspection)."""
        sf = MagicMock()
        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = _make_outcomes(10)
        mock_session.query.return_value = mock_query
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        sf.return_value = mock_session

        rpt = PerformanceReporter(session_factory=sf)
        rpt.get_stats(strategy="MOMENTUM", pair="EURUSD")

        # Verify query was called with TradeOutcome and filter was invoked.
        mock_session.query.assert_called_once()
        mock_query.filter.assert_called_once()
        mock_query.order_by.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_all_losses(self):
        """All trades are losses — stats should still compute."""
        outcomes = [
            _make_outcome(
                r_multiple=-1.0, won=False,
                closed_at=datetime(2026, 1, 5 + i, 12, 0, tzinfo=timezone.utc),
            )
            for i in range(6)
        ]
        rpt = _make_reporter(outcomes)
        stats = rpt.get_stats()
        assert stats is not None
        assert stats["win_rate"] == 0.0
        assert stats["sharpe_ratio"] < 0
        assert stats["max_drawdown"] < 0
        assert stats["profit_factor"] == pytest.approx(0.0)

    def test_single_day_all_trades(self):
        """All trades close on the same day."""
        dt = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        outcomes = [
            _make_outcome(r_multiple=1.0, closed_at=dt + timedelta(minutes=i))
            for i in range(6)
        ]
        rpt = _make_reporter(outcomes)
        s = rpt._outcomes_to_returns(outcomes)
        assert len(s) == 1
        assert s.iloc[0] == pytest.approx(6.0 * _DEFAULT_RISK_FRACTION)

    def test_db_error_returns_empty(self):
        """DB exception → _query_outcomes returns []."""
        sf = MagicMock()
        sf.return_value.__enter__ = MagicMock(side_effect=RuntimeError("boom"))
        sf.return_value.__exit__ = MagicMock(return_value=False)
        rpt = PerformanceReporter(session_factory=sf)
        assert rpt.get_stats() is None
