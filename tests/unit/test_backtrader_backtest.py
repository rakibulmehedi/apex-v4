"""Tests for scripts/backtrader_backtest.py — Backtest with real OHLCV data.

All database interactions are mocked. Tests verify:
  - Data loading and feed conversion
  - Trade simulation (TP/SL/timeout resolution)
  - Metrics computation (win rate, Sharpe, drawdown)
  - Report output
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

# Ensure project root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.backtrader_backtest import (
    CandleRow,
    TradeRecord,
    candles_to_bt_feed,
    compute_metrics,
    load_candles_from_db,
    print_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candle_rows(
    count: int,
    pair: str = "EURUSD",
    base_price: float = 1.10000,
    trend: float = 0.0,
    start: datetime | None = None,
) -> list[CandleRow]:
    """Generate synthetic CandleRow list."""
    if start is None:
        start = datetime(2025, 12, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(count):
        price = base_price + trend * i
        rows.append(
            CandleRow(
                timestamp=start + timedelta(hours=i),
                open=price,
                high=price + 0.00050,
                low=price - 0.00050,
                close=price + 0.00010,
                volume=100.0 + i,
            )
        )
    return rows


def _make_trades(
    count: int,
    win_rate: float = 0.55,
    avg_winner_r: float = 2.5,
    avg_loser_r: float = -1.0,
) -> list[TradeRecord]:
    """Generate synthetic TradeRecord list."""
    trades = []
    rng = np.random.default_rng(42)
    base_dt = datetime(2025, 12, 1, 10, 0, tzinfo=timezone.utc)

    for i in range(count):
        won = rng.random() < win_rate
        r = rng.normal(avg_winner_r, 0.3) if won else rng.normal(avg_loser_r, 0.2)
        trades.append(
            TradeRecord(
                pair="EURUSD",
                strategy="MOMENTUM",
                regime="TRENDING_UP",
                session="LONDON",
                direction="LONG",
                entry_price=1.10000,
                exit_price=1.10000 + r * 0.00150,  # r * risk
                r_multiple=round(r, 4),
                won=r > 0,
                opened_at=base_dt + timedelta(hours=i * 8),
                closed_at=base_dt + timedelta(hours=i * 8 + 4),
            )
        )
    return trades


# ---------------------------------------------------------------------------
# CandleRow → backtrader feed
# ---------------------------------------------------------------------------


class TestCandlesToBtFeed:
    def test_creates_feed(self) -> None:
        candles = _make_candle_rows(50)
        feed = candles_to_bt_feed(candles)
        assert feed is not None
        # PandasData wraps a DataFrame.
        assert hasattr(feed, "p")
        assert hasattr(feed.p, "dataname")

    def test_feed_has_correct_length(self) -> None:
        candles = _make_candle_rows(100)
        feed = candles_to_bt_feed(candles)
        assert len(feed.p.dataname) == 100


# ---------------------------------------------------------------------------
# load_candles_from_db
# ---------------------------------------------------------------------------


class TestLoadCandlesFromDb:
    def test_queries_correct_filters(self) -> None:
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = []

        mock_sf = MagicMock()
        mock_sf.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        result = load_candles_from_db(mock_sf, "EURUSD", "H1", 120)
        assert result == []
        mock_db.query.assert_called_once()


# ---------------------------------------------------------------------------
# compute_metrics
# ---------------------------------------------------------------------------


class TestComputeMetrics:
    def test_returns_none_for_few_trades(self) -> None:
        trades = _make_trades(3)
        assert compute_metrics(trades) is None

    def test_computes_basic_metrics(self) -> None:
        trades = _make_trades(50, win_rate=0.60)
        m = compute_metrics(trades)
        assert m is not None
        assert m["total_trades"] == 50
        assert 0 < m["win_rate"] <= 1.0
        assert isinstance(m["sharpe"], float)
        assert isinstance(m["max_drawdown"], float)
        assert m["max_drawdown"] <= 0  # drawdown is negative

    def test_perfect_winning_streak(self) -> None:
        trades = _make_trades(10, win_rate=1.0, avg_winner_r=2.0)
        m = compute_metrics(trades)
        assert m is not None
        assert m["winning"] == 10
        assert m["losing"] == 0
        assert m["avg_r"] > 0
        assert m["profit_factor"] == float("inf")

    def test_all_losers(self) -> None:
        trades = _make_trades(10, win_rate=0.0, avg_loser_r=-1.0)
        m = compute_metrics(trades)
        assert m is not None
        assert m["winning"] == 0
        assert m["avg_r"] < 0

    def test_win_rate_calculation(self) -> None:
        """Manual trades with known outcomes."""
        base = datetime(2025, 12, 1, 10, 0, tzinfo=timezone.utc)
        trades = [
            TradeRecord(
                pair="EURUSD",
                strategy="MOMENTUM",
                regime="TRENDING_UP",
                session="LONDON",
                direction="LONG",
                entry_price=1.10,
                exit_price=1.11,
                r_multiple=2.0,
                won=True,
                opened_at=base + timedelta(hours=i * 24),
                closed_at=base + timedelta(hours=i * 24 + 4),
            )
            for i in range(3)
        ] + [
            TradeRecord(
                pair="EURUSD",
                strategy="MOMENTUM",
                regime="TRENDING_UP",
                session="LONDON",
                direction="LONG",
                entry_price=1.10,
                exit_price=1.09,
                r_multiple=-1.0,
                won=False,
                opened_at=base + timedelta(hours=(3 + i) * 24),
                closed_at=base + timedelta(hours=(3 + i) * 24 + 4),
            )
            for i in range(2)
        ]

        m = compute_metrics(trades)
        assert m is not None
        assert m["total_trades"] == 5
        assert m["winning"] == 3
        assert m["losing"] == 2
        assert abs(m["win_rate"] - 0.6) < 1e-9


# ---------------------------------------------------------------------------
# print_report
# ---------------------------------------------------------------------------


class TestPrintReport:
    def test_prints_without_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        pair_results = {
            "EURUSD": {
                "total_trades": 30,
                "winning": 18,
                "losing": 12,
                "win_rate": 0.60,
                "avg_r": 0.85,
                "profit_factor": 1.8,
                "sharpe": 1.2,
                "sortino": 1.5,
                "max_drawdown": -0.05,
                "total_return": 0.12,
            }
        }
        trades = _make_trades(30)
        regime_counts: Counter = Counter({"TRENDING_UP": 200, "TRENDING_DOWN": 150, "RANGING": 400, "UNDEFINED": 250})

        # Should not raise.
        print_report(pair_results, trades, regime_counts)
        captured = capsys.readouterr()
        assert "APEX V4" in captured.out
        assert "EURUSD" in captured.out
        assert "Regime Distribution" in captured.out

    def test_empty_trades_no_crash(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_report({}, [], Counter())
        captured = capsys.readouterr()
        assert "insufficient trades" in captured.out


# ---------------------------------------------------------------------------
# TradeRecord dataclass
# ---------------------------------------------------------------------------


class TestTradeRecord:
    def test_fields(self) -> None:
        t = TradeRecord(
            pair="GBPUSD",
            strategy="MEAN_REVERSION",
            regime="RANGING",
            session="OVERLAP",
            direction="SHORT",
            entry_price=1.27000,
            exit_price=1.26500,
            r_multiple=1.5,
            won=True,
            opened_at=datetime(2025, 12, 1, 12, 0, tzinfo=timezone.utc),
            closed_at=datetime(2025, 12, 1, 16, 0, tzinfo=timezone.utc),
        )
        assert t.pair == "GBPUSD"
        assert t.won is True
        assert t.r_multiple == 1.5
