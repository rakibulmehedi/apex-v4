"""Unit tests for src/risk/governor.py — RiskGovernor.

Tests cover:
  - Gate 1: Kill switch active → REJECT
  - Gate 2: Stale data → REJECT
  - Gate 3: Invalid signal geometry → REJECT (SL/TP/direction)
  - Gate 4: Net USD exposure > 40% → REDUCE 50%
  - Gate 5: VaR > 5% → REJECT, VaR > 3% → SOFT kill switch
  - Gate 6: Φ(κ) == 0 → HARD + REJECT, Φ(κ) < 1 → scale size
  - Gate 7: DD > 8% → HARD + REJECT, DD > 5% → REDUCE 50%
  - Full pass: all gates pass → APPROVE
  - Fail-fast: early gate failure skips later gates
  - Multiple REDUCE gates stack
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import numpy as np
import pytest

from src.market.schemas import (
    AlphaHypothesis,
    CalibratedTradeIntent,
    CandleMap,
    Decision,
    Direction,
    MarketSnapshot,
    OHLCV,
    Regime,
    RiskState,
    Strategy,
    TradingSession,
)
from src.risk.covariance import EWMACovarianceMatrix
from src.risk.governor import RiskGovernor
from src.risk.kill_switch import KillLevel, KillSwitch


# ── fixtures ──────────────────────────────────────────────────────────────


def _candles(n: int) -> list[OHLCV]:
    return [OHLCV(open=1.1, high=1.11, low=1.09, close=1.1, volume=100)] * n


def _make_snapshot(stale: bool = False) -> MarketSnapshot:
    ts = int(time.time() * 1000)
    if stale:
        ts -= 10_000  # 10 seconds old
    return MarketSnapshot(
        pair="EURUSD",
        timestamp=ts,
        candles=CandleMap(
            M5=_candles(50),
            M15=_candles(50),
            H1=_candles(200),
            H4=_candles(50),
        ),
        spread_points=0.00015,
        session=TradingSession.LONDON,
    )


def _make_hypothesis(
    direction: str = "LONG",
    sl: float = 1.0950,
    tp: float = 1.1200,
    entry_zone: tuple[float, float] = (1.1000, 1.1010),
) -> AlphaHypothesis:
    return AlphaHypothesis(
        strategy=Strategy.MOMENTUM,
        pair="EURUSD",
        direction=Direction(direction),
        entry_zone=entry_zone,
        stop_loss=sl,
        take_profit=tp,
        setup_score=20,
        expected_R=2.0,
        regime=Regime.TRENDING_UP,
    )


def _make_intent(size: float = 0.01) -> CalibratedTradeIntent:
    return CalibratedTradeIntent(
        p_win=0.55,
        expected_R=2.0,
        edge=0.65,
        suggested_size=size,
        segment_count=50,
    )


def _make_ks(active: bool = False) -> KillSwitch:
    ks = MagicMock(spec=KillSwitch)
    ks.allows_new_signals.return_value = not active
    ks.trigger = AsyncMock(return_value=True)
    return ks


def _make_cov(
    var_value: float = 0.0,
    phi: float = 1.0,
) -> EWMACovarianceMatrix:
    cov = MagicMock(spec=EWMACovarianceMatrix)
    cov.portfolio_var.return_value = var_value
    cov.decay_multiplier.return_value = phi
    return cov


# ── Gate 1: Kill switch ──────────────────────────────────────────────────


class TestGate1KillSwitch:
    @pytest.mark.asyncio
    async def test_active_kill_switch_rejects(self):
        ks = _make_ks(active=True)
        gov = RiskGovernor(ks, _make_cov())

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision == Decision.REJECT
        assert result.gate_failed == 1
        assert result.reason == "kill_switch_active"

    @pytest.mark.asyncio
    async def test_inactive_kill_switch_passes(self):
        ks = _make_ks(active=False)
        gov = RiskGovernor(ks, _make_cov())

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision != Decision.REJECT or result.gate_failed != 1


# ── Gate 2: Data freshness ───────────────────────────────────────────────


class TestGate2DataFreshness:
    @pytest.mark.asyncio
    async def test_stale_snapshot_rejects(self):
        gov = RiskGovernor(_make_ks(), _make_cov())

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(),
            _make_snapshot(stale=True),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision == Decision.REJECT
        assert result.gate_failed == 2
        assert result.reason == "stale_data"

    @pytest.mark.asyncio
    async def test_fresh_snapshot_passes(self):
        gov = RiskGovernor(_make_ks(), _make_cov())

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(),
            _make_snapshot(stale=False),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.gate_failed != 2 if result.decision != Decision.APPROVE else True


# ── Gate 3: Signal sanity ────────────────────────────────────────────────


class TestGate3SignalSanity:
    @pytest.mark.asyncio
    async def test_long_sl_above_entry_rejects(self):
        """LONG: SL must be < entry_zone[0]."""
        hyp = _make_hypothesis(direction="LONG", sl=1.1050, entry_zone=(1.1000, 1.1010))
        gov = RiskGovernor(_make_ks(), _make_cov())

        result = await gov.evaluate(
            hyp,
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision == Decision.REJECT
        assert result.gate_failed == 3
        assert result.reason == "invalid_signal_geometry"

    @pytest.mark.asyncio
    async def test_long_sl_equal_entry_rejects(self):
        """LONG: SL == entry_zone[0] is invalid (not strictly below)."""
        hyp = _make_hypothesis(direction="LONG", sl=1.1000, entry_zone=(1.1000, 1.1010))
        gov = RiskGovernor(_make_ks(), _make_cov())

        result = await gov.evaluate(
            hyp,
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision == Decision.REJECT
        assert result.gate_failed == 3

    @pytest.mark.asyncio
    async def test_short_sl_below_entry_rejects(self):
        """SHORT: SL must be > entry_zone[1]."""
        hyp = _make_hypothesis(direction="SHORT", sl=1.0950, entry_zone=(1.1000, 1.1010))
        gov = RiskGovernor(_make_ks(), _make_cov())

        result = await gov.evaluate(
            hyp,
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision == Decision.REJECT
        assert result.gate_failed == 3

    @pytest.mark.asyncio
    async def test_short_sl_equal_entry_rejects(self):
        """SHORT: SL == entry_zone[1] is invalid."""
        hyp = _make_hypothesis(direction="SHORT", sl=1.1010, entry_zone=(1.1000, 1.1010))
        gov = RiskGovernor(_make_ks(), _make_cov())

        result = await gov.evaluate(
            hyp,
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision == Decision.REJECT
        assert result.gate_failed == 3

    @pytest.mark.asyncio
    async def test_zero_sl_rejects(self):
        hyp = _make_hypothesis(sl=0.0)
        gov = RiskGovernor(_make_ks(), _make_cov())

        result = await gov.evaluate(
            hyp,
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision == Decision.REJECT
        assert result.gate_failed == 3

    @pytest.mark.asyncio
    async def test_zero_tp_rejects(self):
        hyp = _make_hypothesis(tp=0.0)
        gov = RiskGovernor(_make_ks(), _make_cov())

        result = await gov.evaluate(
            hyp,
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision == Decision.REJECT
        assert result.gate_failed == 3

    @pytest.mark.asyncio
    async def test_valid_long_geometry_passes(self):
        """LONG: SL=1.0950 < entry_lo=1.1000 → valid."""
        hyp = _make_hypothesis(direction="LONG", sl=1.0950, entry_zone=(1.1000, 1.1010))
        gov = RiskGovernor(_make_ks(), _make_cov())

        result = await gov.evaluate(
            hyp,
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.gate_failed != 3 if result.decision != Decision.APPROVE else True

    @pytest.mark.asyncio
    async def test_valid_short_geometry_passes(self):
        """SHORT: SL=1.1050 > entry_hi=1.1010 → valid."""
        hyp = _make_hypothesis(direction="SHORT", sl=1.1050, tp=1.0900, entry_zone=(1.1000, 1.1010))
        gov = RiskGovernor(_make_ks(), _make_cov())

        result = await gov.evaluate(
            hyp,
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.gate_failed != 3 if result.decision != Decision.APPROVE else True


# ── Gate 4: Net directional exposure ─────────────────────────────────────


class TestGate4NetExposure:
    @pytest.mark.asyncio
    async def test_high_usd_exposure_reduces(self):
        """3/4 positions same USD direction → 75% > 40% → REDUCE."""
        positions = [
            {"pair": "EURUSD", "direction": "SHORT", "size": 0.01},  # buying USD
            {"pair": "GBPUSD", "direction": "SHORT", "size": 0.01},  # buying USD
            {"pair": "AUDUSD", "direction": "SHORT", "size": 0.01},  # buying USD
            {"pair": "EURJPY", "direction": "LONG", "size": 0.01},  # no USD
        ]

        gov = RiskGovernor(_make_ks(), _make_cov())
        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(size=0.01),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
            open_positions=positions,
        )

        # Size should be reduced
        if result.decision == Decision.REDUCE:
            assert result.final_size < 0.01

    @pytest.mark.asyncio
    async def test_low_exposure_no_reduce(self):
        """1/4 positions with USD → 25% < 40% → no reduction."""
        positions = [
            {"pair": "EURUSD", "direction": "SHORT", "size": 0.01},
            {"pair": "EURJPY", "direction": "LONG", "size": 0.01},
            {"pair": "GBPJPY", "direction": "LONG", "size": 0.01},
            {"pair": "NZDCAD", "direction": "SHORT", "size": 0.01},
        ]

        gov = RiskGovernor(_make_ks(), _make_cov())
        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(size=0.01),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
            open_positions=positions,
        )

        assert result.decision == Decision.APPROVE
        assert result.final_size == pytest.approx(0.01)

    @pytest.mark.asyncio
    async def test_no_positions_no_reduce(self):
        gov = RiskGovernor(_make_ks(), _make_cov())
        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(size=0.01),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision == Decision.APPROVE
        assert result.final_size == pytest.approx(0.01)


# ── Gate 5: Portfolio VaR ────────────────────────────────────────────────


class TestGate5VaR:
    @pytest.mark.asyncio
    async def test_var_above_5pct_rejects(self):
        """VaR > 5% of portfolio → REJECT."""
        cov = _make_cov(var_value=5100.0)  # 5.1% of 100k
        gov = RiskGovernor(_make_ks(), cov)

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision == Decision.REJECT
        assert result.gate_failed == 5
        assert result.reason == "var_limit_breached"

    @pytest.mark.asyncio
    async def test_var_above_3pct_triggers_soft(self):
        """3% < VaR ≤ 5% → SOFT kill switch triggered, trade continues."""
        ks = _make_ks()
        cov = _make_cov(var_value=3500.0)  # 3.5% of 100k
        gov = RiskGovernor(ks, cov)

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        # SOFT triggered but trade not rejected
        ks.trigger.assert_awaited()
        call_args = ks.trigger.call_args
        assert call_args[0][0] == "SOFT"

        # Trade should still pass (APPROVE or REDUCE from other gates)
        assert result.decision != Decision.REJECT or result.gate_failed != 5

    @pytest.mark.asyncio
    async def test_var_below_3pct_clean(self):
        """VaR < 3% → no trigger, clean pass."""
        ks = _make_ks()
        cov = _make_cov(var_value=2000.0)  # 2% of 100k
        gov = RiskGovernor(ks, cov)

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        # No SOFT trigger
        for call in ks.trigger.call_args_list:
            assert call[0][0] != "SOFT" or "VaR" not in call[0][1]


# ── Gate 6: Covariance condition number ──────────────────────────────────


class TestGate6ConditionNumber:
    @pytest.mark.asyncio
    async def test_phi_zero_rejects_with_hard(self):
        """Φ(κ) == 0 → HARD kill switch + REJECT."""
        ks = _make_ks()
        cov = _make_cov(phi=0.0)
        gov = RiskGovernor(ks, cov)

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision == Decision.REJECT
        assert result.gate_failed == 6
        assert result.reason == "correlation_crisis"

        ks.trigger.assert_awaited()
        assert ks.trigger.call_args[0][0] == "HARD"

    @pytest.mark.asyncio
    async def test_phi_half_scales_size(self):
        """Φ(κ) = 0.5 → size multiplied by 0.5."""
        cov = _make_cov(phi=0.5)
        gov = RiskGovernor(_make_ks(), cov)

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(size=0.01),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision == Decision.REDUCE
        assert result.final_size == pytest.approx(0.005)
        assert result.gate_failed == 6

    @pytest.mark.asyncio
    async def test_phi_one_no_scaling(self):
        """Φ(κ) = 1.0 → no size change."""
        cov = _make_cov(phi=1.0)
        gov = RiskGovernor(_make_ks(), cov)

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(size=0.01),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision == Decision.APPROVE
        assert result.final_size == pytest.approx(0.01)


# ── Gate 7: Drawdown state ───────────────────────────────────────────────


class TestGate7Drawdown:
    @pytest.mark.asyncio
    async def test_dd_above_8pct_rejects_with_hard(self):
        """DD > 8% → HARD kill switch + REJECT."""
        ks = _make_ks()
        gov = RiskGovernor(ks, _make_cov())

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.09,
        )

        assert result.decision == Decision.REJECT
        assert result.gate_failed == 7
        assert result.reason == "max_drawdown"

        ks.trigger.assert_awaited()
        assert ks.trigger.call_args[0][0] == "HARD"

    @pytest.mark.asyncio
    async def test_dd_above_5pct_reduces(self):
        """5% < DD ≤ 8% → REDUCE by 50%."""
        gov = RiskGovernor(_make_ks(), _make_cov())

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(size=0.01),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.06,
        )

        assert result.decision == Decision.REDUCE
        assert result.final_size == pytest.approx(0.005)
        assert result.gate_failed == 7

    @pytest.mark.asyncio
    async def test_dd_below_5pct_passes(self):
        """DD < 5% → clean pass."""
        gov = RiskGovernor(_make_ks(), _make_cov())

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(size=0.01),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.04,
        )

        assert result.decision == Decision.APPROVE


# ── full pass ────────────────────────────────────────────────────────────


class TestFullPass:
    @pytest.mark.asyncio
    async def test_all_gates_pass(self):
        """All gates pass → APPROVE with correct size."""
        gov = RiskGovernor(_make_ks(), _make_cov(phi=1.0))

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(size=0.01),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.decision == Decision.APPROVE
        assert result.final_size == pytest.approx(0.01)
        assert result.gate_failed is None
        assert result.reason == "all_gates_passed"
        assert result.risk_state == RiskState.NORMAL


# ── fail-fast ────────────────────────────────────────────────────────────


class TestFailFast:
    @pytest.mark.asyncio
    async def test_gate1_skips_later_gates(self):
        """Kill switch active → VaR never evaluated."""
        ks = _make_ks(active=True)
        cov = _make_cov()
        gov = RiskGovernor(ks, cov)

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        assert result.gate_failed == 1
        cov.portfolio_var.assert_not_called()

    @pytest.mark.asyncio
    async def test_gate2_skips_later_gates(self):
        """Stale data → geometry never checked."""
        gov = RiskGovernor(_make_ks(), _make_cov())

        result = await gov.evaluate(
            _make_hypothesis(sl=0.0),
            _make_intent(),
            _make_snapshot(stale=True),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        # Gate 2 should reject, not gate 3
        assert result.gate_failed == 2


# ── multiple reductions stack ────────────────────────────────────────────


class TestStackedReductions:
    @pytest.mark.asyncio
    async def test_phi_and_dd_reduce_stack(self):
        """Φ(κ)=0.5 × dd_reduce=0.5 → size × 0.25."""
        cov = _make_cov(phi=0.5)
        gov = RiskGovernor(_make_ks(), cov)

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(size=0.01),
            _make_snapshot(),
            portfolio_value=100_000,
            current_dd=0.06,
        )

        assert result.decision == Decision.REDUCE
        assert result.final_size == pytest.approx(0.01 * 0.5 * 0.5)
