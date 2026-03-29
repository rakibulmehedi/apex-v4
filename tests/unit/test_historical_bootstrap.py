"""Tests for scripts/historical_bootstrap.py — Historical data bootstrap logic.

All MT5 and database interactions are mocked. Tests verify:
  - Candle fetching orchestration
  - Snapshot building from historical bars
  - Trade outcome simulation (TP hit, SL hit, timeout)
  - Session classification
  - Segment summary output
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.market.mt5_types import RateBar
from src.market.schemas import Direction, Regime, Strategy, TradingSession

from scripts.historical_bootstrap import (
    _bars_to_ohlcv,
    _find_bars_up_to,
    build_snapshot,
    classify_session,
    fetch_candles,
    print_segment_summary,
    simulate_outcome,
    store_candles,
    store_outcomes,
)


# ---------------------------------------------------------------------------
# Helpers — generate synthetic bars
# ---------------------------------------------------------------------------

_H1_SECS = 3600
_M5_SECS = 300


def _make_bars(
    count: int,
    start_time: int = 1_700_000_000,
    interval: int = _H1_SECS,
    base_price: float = 1.10000,
    trend: float = 0.0,
) -> list[RateBar]:
    """Generate synthetic RateBar list with optional price trend."""
    bars = []
    for i in range(count):
        price = base_price + trend * i
        bars.append(
            RateBar(
                time=start_time + i * interval,
                open=price,
                high=price + 0.00050,
                low=price - 0.00050,
                close=price + 0.00010,
                tick_volume=100 + i,
            )
        )
    return bars


def _make_all_bars(
    h1_count: int = 250,
    start_time: int = 1_700_000_000,
) -> dict[str, list[RateBar]]:
    """Build a full set of bars for all timeframes aligned to start_time."""
    return {
        "M5": _make_bars(h1_count * 12, start_time, _M5_SECS),
        "M15": _make_bars(h1_count * 4, start_time, 900),
        "H1": _make_bars(h1_count, start_time, _H1_SECS),
        "H4": _make_bars(h1_count // 4, start_time, _H1_SECS * 4),
    }


# ---------------------------------------------------------------------------
# classify_session
# ---------------------------------------------------------------------------


class TestClassifySession:
    def test_london(self) -> None:
        assert classify_session(8) == TradingSession.LONDON

    def test_ny(self) -> None:
        assert classify_session(17) == TradingSession.NY

    def test_asia(self) -> None:
        assert classify_session(3) == TradingSession.ASIA

    def test_overlap(self) -> None:
        assert classify_session(13) == TradingSession.OVERLAP


# ---------------------------------------------------------------------------
# _bars_to_ohlcv
# ---------------------------------------------------------------------------


class TestBarsToOhlcv:
    def test_converts_ratebars(self) -> None:
        bars = _make_bars(3)
        ohlcv = _bars_to_ohlcv(bars)
        assert len(ohlcv) == 3
        assert ohlcv[0].open == bars[0].open
        assert ohlcv[0].volume == float(bars[0].tick_volume)


# ---------------------------------------------------------------------------
# _find_bars_up_to
# ---------------------------------------------------------------------------


class TestFindBarsUpTo:
    def test_returns_last_n_bars_before_cutoff(self) -> None:
        bars = _make_bars(100, start_time=1000, interval=10)
        # Cutoff at bar 50's time (1000 + 50*10 = 1500).
        result = _find_bars_up_to(bars, 1500, 20)
        assert len(result) == 20
        assert all(b.time <= 1500 for b in result)
        assert result[-1].time == 1500

    def test_insufficient_bars_returns_partial(self) -> None:
        bars = _make_bars(5, start_time=1000, interval=10)
        result = _find_bars_up_to(bars, 2000, 20)
        assert len(result) == 5


# ---------------------------------------------------------------------------
# build_snapshot
# ---------------------------------------------------------------------------


class TestBuildSnapshot:
    def test_valid_snapshot(self) -> None:
        all_bars = _make_all_bars(250)
        h1_time = all_bars["H1"][220].time
        snap = build_snapshot("EURUSD", h1_time, all_bars)
        assert snap is not None
        assert snap.pair == "EURUSD"
        assert len(snap.candles.H1) == 200
        assert len(snap.candles.M5) == 50
        assert len(snap.candles.M15) == 50
        assert len(snap.candles.H4) == 50

    def test_insufficient_data_returns_none(self) -> None:
        # Only 50 H1 bars — need 200.
        bars = {
            "M5": _make_bars(600, interval=_M5_SECS),
            "M15": _make_bars(200, interval=900),
            "H1": _make_bars(50, interval=_H1_SECS),
            "H4": _make_bars(15, interval=_H1_SECS * 4),
        }
        h1_time = bars["H1"][-1].time
        snap = build_snapshot("EURUSD", h1_time, bars)
        assert snap is None


# ---------------------------------------------------------------------------
# simulate_outcome
# ---------------------------------------------------------------------------


class _FakeHypothesis:
    """Minimal hypothesis stand-in for simulation tests."""

    def __init__(
        self,
        *,
        pair: str = "EURUSD",
        direction: Direction = Direction.LONG,
        entry_zone: tuple[float, float] = (1.10000, 1.10020),
        stop_loss: float = 1.09800,
        take_profit: float = 1.10500,
        strategy: Strategy = Strategy.MOMENTUM,
        regime: Regime = Regime.TRENDING_UP,
    ) -> None:
        self.pair = pair
        self.direction = direction
        self.entry_zone = entry_zone
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.strategy = strategy
        self.regime = regime


class TestSimulateOutcome:
    def test_long_tp_hit(self) -> None:
        """Price rallies to TP — should be a winning LONG trade."""
        hyp = _FakeHypothesis(
            direction=Direction.LONG,
            entry_zone=(1.10000, 1.10020),
            stop_loss=1.09800,
            take_profit=1.10500,
        )
        signal_time = 1_700_000_000
        # Price rallies: bars open above TP.
        m5_bars = [
            RateBar(
                time=signal_time + 300 * (i + 1),
                open=1.10010 + 0.00050 * i,
                high=1.10010 + 0.00050 * i + 0.00060,
                low=1.10010 + 0.00050 * i - 0.00020,
                close=1.10010 + 0.00050 * i + 0.00030,
                tick_volume=50,
            )
            for i in range(20)
        ]

        result = simulate_outcome(hyp, m5_bars, signal_time)
        assert result is not None
        assert result["won"] is True
        assert result["r_multiple"] > 0
        assert result["direction"] == "LONG"

    def test_long_sl_hit(self) -> None:
        """Price drops to SL — should be a losing LONG trade."""
        hyp = _FakeHypothesis(
            direction=Direction.LONG,
            entry_zone=(1.10000, 1.10020),
            stop_loss=1.09800,
            take_profit=1.10500,
        )
        signal_time = 1_700_000_000
        # Price drops: bars go below SL.
        m5_bars = [
            RateBar(
                time=signal_time + 300 * (i + 1),
                open=1.10000 - 0.00030 * i,
                high=1.10000 - 0.00030 * i + 0.00010,
                low=1.10000 - 0.00030 * i - 0.00050,
                close=1.10000 - 0.00030 * i - 0.00020,
                tick_volume=50,
            )
            for i in range(20)
        ]

        result = simulate_outcome(hyp, m5_bars, signal_time)
        assert result is not None
        assert result["won"] is False
        assert result["r_multiple"] < 0

    def test_short_tp_hit(self) -> None:
        """Price drops to TP — should be a winning SHORT trade."""
        hyp = _FakeHypothesis(
            direction=Direction.SHORT,
            entry_zone=(1.10000, 1.10020),
            stop_loss=1.10300,
            take_profit=1.09500,
            strategy=Strategy.MOMENTUM,
            regime=Regime.TRENDING_DOWN,
        )
        signal_time = 1_700_000_000
        # Price drops sharply.
        m5_bars = [
            RateBar(
                time=signal_time + 300 * (i + 1),
                open=1.10000 - 0.00060 * i,
                high=1.10000 - 0.00060 * i + 0.00010,
                low=1.10000 - 0.00060 * i - 0.00070,
                close=1.10000 - 0.00060 * i - 0.00030,
                tick_volume=50,
            )
            for i in range(20)
        ]

        result = simulate_outcome(hyp, m5_bars, signal_time)
        assert result is not None
        assert result["won"] is True
        assert result["direction"] == "SHORT"

    def test_timeout_uses_last_close(self) -> None:
        """Neither SL nor TP hit — should use last bar close."""
        hyp = _FakeHypothesis(
            direction=Direction.LONG,
            entry_zone=(1.10000, 1.10020),
            stop_loss=1.08000,  # very far SL
            take_profit=1.15000,  # very far TP
        )
        signal_time = 1_700_000_000
        # Flat price, neither SL nor TP hit.
        m5_bars = [
            RateBar(
                time=signal_time + 300 * (i + 1),
                open=1.10010,
                high=1.10020,
                low=1.10000,
                close=1.10015,
                tick_volume=50,
            )
            for i in range(10)
        ]

        result = simulate_outcome(hyp, m5_bars, signal_time)
        assert result is not None
        assert result["exit_price"] == round(1.10015, 5)

    def test_no_future_bars_returns_none(self) -> None:
        hyp = _FakeHypothesis()
        signal_time = 1_700_000_000
        m5_bars = [
            RateBar(
                time=signal_time - 300,
                open=1.10,
                high=1.101,
                low=1.099,
                close=1.100,
                tick_volume=50,
            )
        ]
        result = simulate_outcome(hyp, m5_bars, signal_time)
        assert result is None

    def test_outcome_has_correct_fields(self) -> None:
        """Verify all required trade_outcome fields are present."""
        hyp = _FakeHypothesis()
        signal_time = 1_700_000_000
        m5_bars = [
            RateBar(
                time=signal_time + 300,
                open=1.10,
                high=1.11,
                low=1.09,
                close=1.105,
                tick_volume=50,
            )
        ]
        result = simulate_outcome(hyp, m5_bars, signal_time)
        assert result is not None
        required_keys = {
            "pair",
            "strategy",
            "regime",
            "session",
            "direction",
            "entry_price",
            "exit_price",
            "r_multiple",
            "won",
            "fill_id",
            "opened_at",
            "closed_at",
        }
        assert required_keys <= set(result.keys())
        assert result["fill_id"] is None
        assert isinstance(result["opened_at"], datetime)
        assert isinstance(result["closed_at"], datetime)


# ---------------------------------------------------------------------------
# fetch_candles
# ---------------------------------------------------------------------------


class TestFetchCandles:
    def test_delegates_to_mt5(self) -> None:
        mt5 = MagicMock()
        bars = _make_bars(10)
        mt5.copy_rates_from_pos.return_value = bars

        result = fetch_candles(mt5, "EURUSD", "H1", 100)
        assert result == bars
        mt5.copy_rates_from_pos.assert_called_once()

    def test_returns_empty_on_failure(self) -> None:
        mt5 = MagicMock()
        mt5.copy_rates_from_pos.return_value = None

        result = fetch_candles(mt5, "EURUSD", "H1", 100)
        assert result == []


# ---------------------------------------------------------------------------
# store_candles
# ---------------------------------------------------------------------------


class TestStoreCandles:
    def test_inserts_new_candles(self) -> None:
        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None  # No existing
        mock_session.query.return_value = mock_query

        mock_sf = MagicMock()
        mock_sf.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        bars = _make_bars(3)
        all_candles = {"EURUSD": {"H1": bars}}

        inserted = store_candles(mock_sf, all_candles)
        assert inserted == 3
        assert mock_session.add.call_count == 3
        mock_session.commit.assert_called()


# ---------------------------------------------------------------------------
# store_outcomes
# ---------------------------------------------------------------------------


class TestStoreOutcomes:
    def test_inserts_outcomes(self) -> None:
        mock_session = MagicMock()
        mock_sf = MagicMock()
        mock_sf.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        outcomes = [
            {
                "pair": "EURUSD",
                "strategy": "MOMENTUM",
                "regime": "TRENDING_UP",
                "session": "LONDON",
                "direction": "LONG",
                "entry_price": 1.10,
                "exit_price": 1.11,
                "r_multiple": 2.0,
                "won": True,
                "fill_id": None,
                "opened_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "closed_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
            }
        ]

        inserted = store_outcomes(mock_sf, outcomes)
        assert inserted == 1
        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# print_segment_summary
# ---------------------------------------------------------------------------


class TestSegmentSummary:
    def test_flags_thin_segments(self, capsys: pytest.CaptureFixture[str]) -> None:
        outcomes = [
            {
                "strategy": "MOMENTUM",
                "regime": "TRENDING_UP",
                "session": "LONDON",
                "r_multiple": 1.5,
                "won": True,
                "pair": "EURUSD",
            }
        ] * 10  # Only 10 — below 30

        thin = print_segment_summary(outcomes)
        assert len(thin) == 1
        assert thin[0] == ("MOMENTUM", "TRENDING_UP", "LONDON")

    def test_ok_segments_not_flagged(self, capsys: pytest.CaptureFixture[str]) -> None:
        outcomes = [
            {
                "strategy": "MOMENTUM",
                "regime": "TRENDING_UP",
                "session": "LONDON",
                "r_multiple": 1.5,
                "won": True,
                "pair": "EURUSD",
            }
        ] * 35  # Above 30

        thin = print_segment_summary(outcomes)
        assert len(thin) == 0
