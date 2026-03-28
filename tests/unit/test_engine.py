"""Unit tests for src/calibration/engine.py — CalibrationEngine.

Tests cover:
  - Kelly criterion math (Section 7.1 exact formulas)
  - Drawdown scalar branches (< 2%, 2-5%, >= 5%)
  - Correlation scalar (0, 1, 2+ same-currency positions)
  - None returns: no segment data, edge <= 0, dd >= 5%
  - Edge cases: boundary values, empty positions list
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.calibration.engine import CalibrationEngine
from src.market.schemas import (
    AlphaHypothesis,
    CalibratedTradeIntent,
    Direction,
    Regime,
    Strategy,
)


# ── fixtures ──────────────────────────────────────────────────────────────


def _make_hypothesis(
    strategy: str = "MOMENTUM",
    pair: str = "EURUSD",
    direction: str = "LONG",
    regime: str = "TRENDING_UP",
) -> AlphaHypothesis:
    """Build a minimal valid AlphaHypothesis."""
    return AlphaHypothesis(
        strategy=Strategy(strategy),
        pair=pair,
        direction=Direction(direction),
        entry_zone=(1.1000, 1.1010),
        stop_loss=1.0950,
        take_profit=1.1200,
        setup_score=20,
        expected_R=2.0,
        regime=Regime(regime),
        conviction=None if strategy == "MOMENTUM" else 0.80,
    )


def _make_engine(
    segment_stats: dict | None = None,
    capital_allocation_pct: float = 1.0,
) -> CalibrationEngine:
    """Build engine with a mocked PerformanceDatabase."""
    mock_db = MagicMock()
    mock_db.get_segment_stats.return_value = segment_stats
    engine = CalibrationEngine(
        perf_db=mock_db,
        capital_allocation_pct=capital_allocation_pct,
    )
    return engine


def _default_stats(
    win_rate: float = 0.60,
    avg_r: float = 2.0,
    trade_count: int = 50,
) -> dict:
    """Standard segment stats dict."""
    return {
        "win_rate": win_rate,
        "avg_R": avg_r,
        "trade_count": trade_count,
        "last_updated": None,
    }


# ── Kelly criterion math ─────────────────────────────────────────────────


class TestKellyCriterion:
    """Verify exact Section 7.1 formulas."""

    def test_basic_kelly(self):
        """p=0.60, b=2.0 → edge=0.80, f*=0.40, f_quarter=0.10, f_final=0.02."""
        engine = _make_engine(_default_stats(win_rate=0.60, avg_r=2.0))
        hyp = _make_hypothesis()

        result = engine.calibrate(hyp, "LONDON", current_dd=0.0)

        assert result is not None
        # edge = 0.60 * 2.0 - 0.40 = 0.80
        assert result.edge == pytest.approx(0.80)
        # f* = 0.80 / 2.0 = 0.40
        # f_quarter = 0.40 * 0.25 = 0.10
        # f_final = min(0.10, 0.02) = 0.02
        assert result.suggested_size == pytest.approx(0.02)

    def test_small_edge_below_cap(self):
        """p=0.36, b=2.0 → small edge, f_quarter < 0.02, no cap hit."""
        engine = _make_engine(_default_stats(win_rate=0.36, avg_r=2.0))
        hyp = _make_hypothesis()

        result = engine.calibrate(hyp, "LONDON", current_dd=0.0)

        assert result is not None
        # edge = 0.36 * 2.0 - 0.64 = 0.72 - 0.64 = 0.08
        edge = 0.36 * 2.0 - 0.64
        f_star = edge / 2.0  # 0.04
        f_quarter = f_star * 0.25  # 0.01
        assert f_quarter < 0.02  # confirms no cap
        assert result.edge == pytest.approx(edge)
        assert result.suggested_size == pytest.approx(f_quarter)

    def test_f_quarter_exactly_at_cap(self):
        """When f_quarter == 0.02, min(0.02, 0.02) = 0.02."""
        # Need: f* = 0.08 → f_quarter = 0.02
        # f* = edge / avg_r = 0.08
        # edge = p*b - q = 0.08 * avg_r
        # Pick avg_r = 2.0: edge = 0.16 → p*2 - (1-p) = 0.16 → 3p = 1.16 → p ≈ 0.3867
        # Actually let's just pick values: avg_r=2.5, p=0.5
        # edge = 0.5*2.5 - 0.5 = 0.75, f* = 0.75/2.5 = 0.30, f_q = 0.075 → too big
        # Let me solve: f_quarter = 0.02 → f* = 0.08 → edge/b = 0.08
        # edge = p*b - q.  Let b=2.0: 0.08*2 = 0.16 = edge
        # 2p - (1-p) = 0.16 → 3p - 1 = 0.16 → p = 0.3867
        engine = _make_engine(_default_stats(win_rate=0.3867, avg_r=2.0))
        hyp = _make_hypothesis()

        result = engine.calibrate(hyp, "LONDON", current_dd=0.0)

        assert result is not None
        edge = 0.3867 * 2.0 - 0.6133
        f_star = edge / 2.0
        f_quarter = f_star * 0.25
        assert result.suggested_size == pytest.approx(min(f_quarter, 0.02))

    def test_kelly_formula_p_win_and_avg_r(self):
        """Verify f* = (p*b - q) / b with various inputs."""
        # p=0.55, b=2.5
        p, b = 0.55, 2.5
        q = 1.0 - p
        edge = p * b - q
        f_star = edge / b
        f_quarter = f_star * 0.25
        f_final = min(f_quarter, 0.02)

        engine = _make_engine(_default_stats(win_rate=p, avg_r=b))
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.0)

        assert result is not None
        assert result.p_win == pytest.approx(p)
        assert result.expected_R == pytest.approx(b)
        assert result.edge == pytest.approx(edge)
        assert result.suggested_size == pytest.approx(f_final)

    def test_output_fields(self):
        """CalibratedTradeIntent has all required fields."""
        engine = _make_engine(_default_stats(win_rate=0.60, avg_r=2.0, trade_count=42))
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.0)

        assert isinstance(result, CalibratedTradeIntent)
        assert result.p_win == pytest.approx(0.60)
        assert result.expected_R == pytest.approx(2.0)
        assert result.edge == pytest.approx(0.80)
        assert result.segment_count == 42


# ── drawdown scalar ──────────────────────────────────────────────────────


class TestDrawdownScalar:
    """dd_scalar branches per Section 7.1."""

    def test_no_drawdown(self):
        """current_dd=0.0 → dd_scalar=1.0, full size."""
        engine = _make_engine(_default_stats())
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.0)
        assert result is not None
        assert result.suggested_size == pytest.approx(0.02)

    def test_small_drawdown_under_2_percent(self):
        """current_dd=0.019 → dd_scalar=1.0."""
        engine = _make_engine(_default_stats())
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.019)
        assert result is not None
        assert result.suggested_size == pytest.approx(0.02)

    def test_drawdown_at_2_percent_boundary(self):
        """current_dd=0.02 → dd_scalar=0.5 (2% is NOT < 2%)."""
        engine = _make_engine(_default_stats())
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.02)
        assert result is not None
        assert result.suggested_size == pytest.approx(0.02 * 0.5)

    def test_drawdown_3_percent(self):
        """current_dd=0.03 → dd_scalar=0.5."""
        engine = _make_engine(_default_stats())
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.03)
        assert result is not None
        assert result.suggested_size == pytest.approx(0.02 * 0.5)

    def test_drawdown_at_5_percent_boundary(self):
        """current_dd=0.05 → None (5% is NOT < 5%)."""
        engine = _make_engine(_default_stats())
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.05)
        assert result is None

    def test_drawdown_above_5_percent(self):
        """current_dd=0.08 → None."""
        engine = _make_engine(_default_stats())
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.08)
        assert result is None

    def test_drawdown_exactly_0_percent(self):
        """current_dd=0.0 → dd_scalar=1.0."""
        assert CalibrationEngine._dd_scalar(0.0) == 1.0

    def test_dd_scalar_direct_calls(self):
        """Verify _dd_scalar static method across boundaries."""
        assert CalibrationEngine._dd_scalar(0.01) == 1.0
        assert CalibrationEngine._dd_scalar(0.02) == 0.5
        assert CalibrationEngine._dd_scalar(0.049) == 0.5
        assert CalibrationEngine._dd_scalar(0.05) is None
        assert CalibrationEngine._dd_scalar(0.10) is None


# ── correlation scalar ───────────────────────────────────────────────────


class TestCorrelationScalar:
    """correlation_scalar: >= 2 same-currency positions → 0.5."""

    def test_no_open_positions(self):
        """No positions → 1.0."""
        assert CalibrationEngine._correlation_scalar("EURUSD", None) == 1.0
        assert CalibrationEngine._correlation_scalar("EURUSD", []) == 1.0

    def test_one_correlated_position(self):
        """1 same-currency → 1.0 (threshold is 2)."""
        positions = [{"pair": "EURGBP"}]
        assert CalibrationEngine._correlation_scalar("EURUSD", positions) == 1.0

    def test_two_correlated_positions(self):
        """2 same-currency → 0.5."""
        positions = [{"pair": "EURGBP"}, {"pair": "EURJPY"}]
        assert CalibrationEngine._correlation_scalar("EURUSD", positions) == 0.5

    def test_three_correlated_positions(self):
        """3 same-currency → still 0.5."""
        positions = [{"pair": "EURGBP"}, {"pair": "EURJPY"}, {"pair": "EURCAD"}]
        assert CalibrationEngine._correlation_scalar("EURUSD", positions) == 0.5

    def test_two_unrelated_positions(self):
        """2 positions with no shared currency → 1.0."""
        positions = [{"pair": "GBPJPY"}, {"pair": "NZDCAD"}]
        assert CalibrationEngine._correlation_scalar("EURUSD", positions) == 1.0

    def test_quote_currency_match(self):
        """Match on quote currency (USD in EURUSD matches GBPUSD)."""
        positions = [{"pair": "GBPUSD"}, {"pair": "AUDUSD"}]
        assert CalibrationEngine._correlation_scalar("EURUSD", positions) == 0.5

    def test_mixed_base_and_quote_match(self):
        """One base match + one quote match = 2 → 0.5."""
        positions = [{"pair": "EURGBP"}, {"pair": "AUDUSD"}]
        assert CalibrationEngine._correlation_scalar("EURUSD", positions) == 0.5

    def test_correlation_applied_to_final_size(self):
        """2 correlated positions halve the final size."""
        engine = _make_engine(_default_stats())
        positions = [{"pair": "EURGBP"}, {"pair": "EURJPY"}]

        result = engine.calibrate(
            _make_hypothesis(),
            "LONDON",
            current_dd=0.0,
            open_positions=positions,
        )

        assert result is not None
        # f_final = 0.02, dd_scalar = 1.0, corr_scalar = 0.5
        assert result.suggested_size == pytest.approx(0.02 * 0.5)

    def test_dd_and_correlation_stack(self):
        """dd_scalar=0.5 × corr_scalar=0.5 = 0.25 multiplier."""
        engine = _make_engine(_default_stats())
        positions = [{"pair": "EURGBP"}, {"pair": "EURJPY"}]

        result = engine.calibrate(
            _make_hypothesis(),
            "LONDON",
            current_dd=0.03,
            open_positions=positions,
        )

        assert result is not None
        # f_final = 0.02, dd_scalar = 0.5, corr_scalar = 0.5
        assert result.suggested_size == pytest.approx(0.02 * 0.5 * 0.5)


# ── None returns (rejection paths) ──────────────────────────────────────


class TestRejections:
    """Every None return path is tested."""

    def test_no_segment_data(self):
        """PerformanceDatabase returns None → calibrate returns None."""
        engine = _make_engine(segment_stats=None)
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.0)
        assert result is None

    def test_edge_zero(self):
        """edge == 0 → rejected."""
        # p=0.50, b=1.0 → edge = 0.50*1.0 - 0.50 = 0.0
        engine = _make_engine(_default_stats(win_rate=0.50, avg_r=1.0))
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.0)
        assert result is None

    def test_edge_negative(self):
        """edge < 0 → rejected."""
        # p=0.40, b=1.5 → edge = 0.40*1.5 - 0.60 = 0.60 - 0.60 = 0.0 → 0
        # p=0.30, b=1.5 → edge = 0.30*1.5 - 0.70 = 0.45 - 0.70 = -0.25
        engine = _make_engine(_default_stats(win_rate=0.30, avg_r=1.5))
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.0)
        assert result is None

    def test_drawdown_5_percent_rejected(self):
        """dd >= 5% → rejected before DB lookup."""
        engine = _make_engine(_default_stats())
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.05)
        assert result is None
        # DB should NOT have been called (early exit)
        engine._perf_db.get_segment_stats.assert_not_called()

    def test_drawdown_rejects_before_segment_lookup(self):
        """dd >= 5% exits before touching the database."""
        engine = _make_engine(segment_stats=None)
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.10)
        assert result is None
        engine._perf_db.get_segment_stats.assert_not_called()


# ── segment routing ──────────────────────────────────────────────────────


class TestSegmentRouting:
    """Verify strategy/regime/session are passed to PerformanceDatabase."""

    def test_momentum_trending_up_london(self):
        """Correct segment keys forwarded to DB."""
        engine = _make_engine(_default_stats())
        hyp = _make_hypothesis(
            strategy="MOMENTUM",
            regime="TRENDING_UP",
        )
        engine.calibrate(hyp, "LONDON", current_dd=0.0)

        engine._perf_db.get_segment_stats.assert_called_once_with(
            "MOMENTUM",
            "TRENDING_UP",
            "LONDON",
        )

    def test_mean_reversion_ranging_overlap(self):
        """MR + RANGING + OVERLAP segment."""
        engine = _make_engine(_default_stats())
        hyp = _make_hypothesis(
            strategy="MEAN_REVERSION",
            regime="RANGING",
        )
        engine.calibrate(hyp, "OVERLAP", current_dd=0.0)

        engine._perf_db.get_segment_stats.assert_called_once_with(
            "MEAN_REVERSION",
            "RANGING",
            "OVERLAP",
        )


# ── mean reversion strategy ─────────────────────────────────────────────


class TestMeanReversionCalibration:
    """MR hypotheses have conviction != None."""

    def test_mr_hypothesis_calibrates(self):
        """MR hypothesis with positive edge succeeds."""
        engine = _make_engine(_default_stats(win_rate=0.65, avg_r=1.8))
        hyp = _make_hypothesis(strategy="MEAN_REVERSION", regime="RANGING")

        result = engine.calibrate(hyp, "OVERLAP", current_dd=0.0)

        assert result is not None
        edge = 0.65 * 1.8 - 0.35
        assert result.edge == pytest.approx(edge)


# ── full integration scenario ────────────────────────────────────────────


class TestIntegrationScenario:
    """End-to-end scenarios with realistic numbers."""

    def test_conservative_trader(self):
        """p=0.55, b=2.0, dd=0.03, 1 correlated position."""
        p, b = 0.55, 2.0
        edge = p * b - (1 - p)  # 1.1 - 0.45 = 0.65
        f_star = edge / b  # 0.325
        f_quarter = f_star * 0.25  # 0.08125
        f_final = min(f_quarter, 0.02)  # 0.02
        dd_scalar = 0.5  # dd=0.03 → 0.5
        corr_scalar = 1.0  # only 1 correlated
        expected_size = f_final * dd_scalar * corr_scalar  # 0.01

        engine = _make_engine(_default_stats(win_rate=p, avg_r=b))
        positions = [{"pair": "EURGBP"}]
        result = engine.calibrate(
            _make_hypothesis(),
            "LONDON",
            current_dd=0.03,
            open_positions=positions,
        )

        assert result is not None
        assert result.suggested_size == pytest.approx(expected_size)
        assert result.suggested_size == pytest.approx(0.01)

    def test_all_scalars_applied(self):
        """dd_scalar=0.5, corr_scalar=0.5 → size = f_final * 0.25."""
        engine = _make_engine(_default_stats(win_rate=0.60, avg_r=2.0))
        positions = [{"pair": "EURGBP"}, {"pair": "EURJPY"}]

        result = engine.calibrate(
            _make_hypothesis(),
            "LONDON",
            current_dd=0.03,
            open_positions=positions,
        )

        assert result is not None
        # f_final=0.02, dd=0.5, corr=0.5
        assert result.suggested_size == pytest.approx(0.02 * 0.5 * 0.5)

    def test_marginal_edge(self):
        """Barely positive edge still produces a trade."""
        # p=0.34, b=2.0 → edge = 0.68 - 0.66 = 0.02
        engine = _make_engine(_default_stats(win_rate=0.34, avg_r=2.0))
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.0)

        assert result is not None
        edge = 0.34 * 2.0 - 0.66
        assert result.edge == pytest.approx(edge)
        assert result.edge > 0


# ── capital allocation scaling ────────────────────────────────────────


class TestCapitalAllocation:
    """capital_allocation_pct scales final_size proportionally."""

    def test_default_allocation_is_unity(self):
        """No capital_allocation_pct → multiplier is 1.0 (no change)."""
        engine = _make_engine(_default_stats())
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.0)
        assert result is not None
        assert result.suggested_size == pytest.approx(0.02)

    def test_ten_percent_allocation(self):
        """capital_allocation_pct=0.10 scales size by 10%."""
        engine = _make_engine(_default_stats(), capital_allocation_pct=0.10)
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.0)
        assert result is not None
        # f_final=0.02, dd=1.0, corr=1.0, cap=0.10
        assert result.suggested_size == pytest.approx(0.02 * 0.10)

    def test_allocation_stacks_with_dd_and_corr(self):
        """All three scalars multiply: dd=0.5, corr=0.5, cap=0.10."""
        engine = _make_engine(_default_stats(), capital_allocation_pct=0.10)
        positions = [{"pair": "EURGBP"}, {"pair": "EURJPY"}]
        result = engine.calibrate(
            _make_hypothesis(),
            "LONDON",
            current_dd=0.03,
            open_positions=positions,
        )
        assert result is not None
        # f_final=0.02, dd=0.5, corr=0.5, cap=0.10
        assert result.suggested_size == pytest.approx(0.02 * 0.5 * 0.5 * 0.10)

    def test_full_allocation(self):
        """capital_allocation_pct=1.0 is equivalent to no scaling."""
        engine = _make_engine(_default_stats(), capital_allocation_pct=1.0)
        result = engine.calibrate(_make_hypothesis(), "LONDON", current_dd=0.0)
        assert result is not None
        assert result.suggested_size == pytest.approx(0.02)
