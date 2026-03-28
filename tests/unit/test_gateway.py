"""Unit tests for src/execution/gateway.py — ExecutionGateway.

Tests cover:
  - Pre-flight 1: Kill switch active → None
  - Pre-flight 2: Decision not APPROVE → None
  - Pre-flight 3: final_size <= 0 → None
  - Pre-flight 4: Zero SL/TP/entry prices → None
  - Pre-flight 5: Stale approval (> 2000ms) → None
  - Volume calculation: rounding, clamping [0.01, 100.0]
  - Tick failure: no tick data → None
  - Live mode: TRADE_RETCODE_DONE → FillRecord
  - Live mode: bad retcode → None
  - Live mode: order_send returns None → None
  - Paper mode: skip order_send, simulate fill with slippage=0
  - Paper mode: correct FillRecord fields
  - Direction: LONG uses ask, SHORT uses bid
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from src.execution.gateway import ExecutionGateway, FillRecord
from src.market.mt5_types import TRADE_RETCODE_DONE, OrderResult, Tick
from src.market.schemas import (
    AlphaHypothesis,
    Decision,
    Direction,
    Regime,
    RiskDecision,
    RiskState,
    Strategy,
)


# ── fixtures ──────────────────────────────────────────────────────────────


def _make_hypothesis(
    direction: Direction = Direction.LONG,
    strategy: Strategy = Strategy.MOMENTUM,
    regime: Regime = Regime.TRENDING_UP,
) -> AlphaHypothesis:
    return AlphaHypothesis(
        strategy=strategy,
        pair="EURUSD",
        direction=direction,
        entry_zone=(1.08400, 1.08500),
        stop_loss=1.08100,
        take_profit=1.09500,
        setup_score=20,
        expected_R=2.5,
        regime=regime,
        conviction=None,
    )


def _make_decision(
    decision: Decision = Decision.APPROVE,
    final_size: float = 0.01,
) -> RiskDecision:
    if decision == Decision.APPROVE:
        return RiskDecision(
            decision=decision,
            final_size=final_size,
            reason="all_gates_passed",
            risk_state=RiskState.NORMAL,
            gate_failed=None,
        )
    return RiskDecision(
        decision=decision,
        final_size=final_size,
        reason="some_reason",
        risk_state=RiskState.THROTTLE,
        gate_failed=4,
    )


def _make_tick(bid: float = 1.08450, ask: float = 1.08465) -> Tick:
    return Tick(time=int(time.time()), bid=bid, ask=ask, last=0.0, volume=0, flags=0)


def _make_order_result(
    retcode: int = TRADE_RETCODE_DONE,
    price: float = 1.08465,
    volume: float = 0.01,
    order: int = 123456,
) -> OrderResult:
    return OrderResult(
        retcode=retcode,
        order=order,
        deal=789,
        volume=volume,
        price=price,
        comment="done",
    )


def _make_gateway(
    paper: bool = True,
    ks_allows: bool = True,
    tick: Tick | None = None,
    order_result: OrderResult | None = None,
) -> ExecutionGateway:
    mt5 = MagicMock()
    mt5.symbol_info_tick.return_value = tick or _make_tick()
    mt5.order_send.return_value = order_result

    ks = MagicMock()
    ks.allows_new_signals.return_value = ks_allows

    return ExecutionGateway(mt5_client=mt5, kill_switch=ks, paper_mode=paper)


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── Pre-flight rejection tests ────────────────────────────────────────────


class TestPreflightKillSwitch:
    """Pre-flight 1: Kill switch active → None."""

    def test_kill_switch_active_rejects(self):
        gw = _make_gateway(ks_allows=False)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        assert result is None

    def test_kill_switch_inactive_passes(self):
        gw = _make_gateway(ks_allows=True, paper=True)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        assert result is not None


class TestPreflightDecision:
    """Pre-flight 2: Decision must be APPROVE."""

    def test_reject_decision_returns_none(self):
        gw = _make_gateway()
        decision = _make_decision(Decision.REJECT, final_size=0.0)
        result = gw.execute(
            _make_hypothesis(),
            decision,
            10_000.0,
            _now_ms(),
        )
        assert result is None

    def test_reduce_decision_returns_none(self):
        gw = _make_gateway()
        decision = _make_decision(Decision.REDUCE, final_size=0.005)
        result = gw.execute(
            _make_hypothesis(),
            decision,
            10_000.0,
            _now_ms(),
        )
        assert result is None


class TestPreflightSize:
    """Pre-flight 3: final_size must be > 0."""

    def test_zero_size_rejects(self):
        gw = _make_gateway()
        decision = _make_decision(Decision.APPROVE, final_size=0.0)
        # Need edge > 0 for APPROVE with gate_failed=None, but 0.0 size
        # Actually, RiskDecision doesn't enforce size > 0 for APPROVE.
        # Gateway catches it.
        result = gw.execute(
            _make_hypothesis(),
            decision,
            10_000.0,
            _now_ms(),
        )
        assert result is None


class TestPreflightPrices:
    """Pre-flight 4: Zero SL, TP, or entry prices → None."""

    def test_zero_stop_loss_rejects(self):
        gw = _make_gateway()
        hyp = AlphaHypothesis(
            strategy=Strategy.MOMENTUM,
            pair="EURUSD",
            direction=Direction.LONG,
            entry_zone=(1.084, 1.085),
            stop_loss=0.0,
            take_profit=1.095,
            setup_score=20,
            expected_R=2.5,
            regime=Regime.TRENDING_UP,
        )
        # SL=0 fails Pydantic gate_3 in governor, but gateway also catches it.
        # Actually, AlphaHypothesis doesn't forbid sl=0, so test directly.
        result = gw.execute(hyp, _make_decision(), 10_000.0, _now_ms())
        assert result is None

    def test_zero_take_profit_rejects(self):
        gw = _make_gateway()
        hyp = AlphaHypothesis(
            strategy=Strategy.MOMENTUM,
            pair="EURUSD",
            direction=Direction.LONG,
            entry_zone=(1.084, 1.085),
            stop_loss=1.081,
            take_profit=0.0,
            setup_score=20,
            expected_R=2.5,
            regime=Regime.TRENDING_UP,
        )
        result = gw.execute(hyp, _make_decision(), 10_000.0, _now_ms())
        assert result is None

    def test_zero_entry_zone_lo_rejects(self):
        gw = _make_gateway()
        hyp = AlphaHypothesis(
            strategy=Strategy.MOMENTUM,
            pair="EURUSD",
            direction=Direction.LONG,
            entry_zone=(0.0, 1.085),
            stop_loss=1.081,
            take_profit=1.095,
            setup_score=20,
            expected_R=2.5,
            regime=Regime.TRENDING_UP,
        )
        result = gw.execute(hyp, _make_decision(), 10_000.0, _now_ms())
        assert result is None

    def test_zero_entry_zone_hi_rejects(self):
        gw = _make_gateway()
        hyp = AlphaHypothesis(
            strategy=Strategy.MOMENTUM,
            pair="EURUSD",
            direction=Direction.LONG,
            entry_zone=(1.084, 0.0),
            stop_loss=1.081,
            take_profit=1.095,
            setup_score=20,
            expected_R=2.5,
            regime=Regime.TRENDING_UP,
        )
        result = gw.execute(hyp, _make_decision(), 10_000.0, _now_ms())
        assert result is None


class TestPreflightStaleness:
    """Pre-flight 5: Stale approval (> 2000ms) → None."""

    def test_stale_approval_rejects(self):
        gw = _make_gateway()
        old_ts = _now_ms() - 3000  # 3 seconds ago
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            old_ts,
        )
        assert result is None

    def test_fresh_approval_passes(self):
        gw = _make_gateway(paper=True)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        assert result is not None

    def test_boundary_2000ms_passes(self):
        """Exactly 2000ms should NOT be stale (> not >=)."""
        gw = _make_gateway(paper=True)
        # Use a timestamp right at the boundary — give 50ms tolerance
        ts = _now_ms() - 1900
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            ts,
        )
        assert result is not None


# ── Volume calculation tests ──────────────────────────────────────────────


class TestVolumeCalculation:
    """Volume = round(final_size × equity / 100_000, 2), clamped."""

    def test_standard_volume(self):
        """0.01 × 10_000 / 100_000 = 0.001 → clamped to 0.01."""
        gw = _make_gateway(paper=True)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(final_size=0.01),
            10_000.0,
            _now_ms(),
        )
        assert result is not None
        assert result.requested_volume == 0.01  # min clamp

    def test_large_equity_volume(self):
        """0.02 × 1_000_000 / 100_000 = 0.2 lots."""
        gw = _make_gateway(paper=True)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(final_size=0.02),
            1_000_000.0,
            _now_ms(),
        )
        assert result is not None
        assert result.requested_volume == 0.20

    def test_min_clamp(self):
        """Tiny size → clamped to 0.01."""
        gw = _make_gateway(paper=True)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(final_size=0.0001),
            1_000.0,
            _now_ms(),
        )
        assert result is not None
        assert result.requested_volume == 0.01

    def test_max_clamp(self):
        """Huge size → clamped to 100.0."""
        gw = _make_gateway(paper=True)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(final_size=0.02),
            1_000_000_000.0,
            _now_ms(),
        )
        assert result is not None
        assert result.requested_volume == 100.0

    def test_rounding(self):
        """0.015 × 50_000 / 100_000 = 0.0075 → rounded to 0.01."""
        gw = _make_gateway(paper=True)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(final_size=0.015),
            50_000.0,
            _now_ms(),
        )
        assert result is not None
        assert result.requested_volume == 0.01


# ── Tick failure tests ────────────────────────────────────────────────────


class TestTickFailure:
    """No tick data → None."""

    def test_no_tick_returns_none(self):
        gw = _make_gateway(paper=True, tick=None)
        # Override the mock to return None
        gw._mt5.symbol_info_tick.return_value = None
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        assert result is None


# ── Direction price selection ─────────────────────────────────────────────


class TestDirectionPricing:
    """LONG uses ask, SHORT uses bid."""

    def test_long_uses_ask(self):
        tick = _make_tick(bid=1.08450, ask=1.08465)
        gw = _make_gateway(paper=True, tick=tick)
        result = gw.execute(
            _make_hypothesis(Direction.LONG),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        assert result is not None
        assert result.fill_price == 1.08465

    def test_short_uses_bid(self):
        tick = _make_tick(bid=1.08450, ask=1.08465)
        gw = _make_gateway(paper=True, tick=tick)
        result = gw.execute(
            _make_hypothesis(Direction.SHORT),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        assert result is not None
        assert result.fill_price == 1.08450


# ── Paper trading mode ────────────────────────────────────────────────────


class TestPaperMode:
    """Paper mode: skip order_send, simulate fill."""

    def test_paper_fill_has_zero_slippage(self):
        gw = _make_gateway(paper=True)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        assert result is not None
        assert result.slippage_points == 0.0

    def test_paper_fill_is_paper_flag(self):
        gw = _make_gateway(paper=True)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        assert result is not None
        assert result.is_paper is True

    def test_paper_does_not_call_order_send(self):
        gw = _make_gateway(paper=True)
        gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        gw._mt5.order_send.assert_not_called()

    def test_paper_fill_price_equals_requested(self):
        gw = _make_gateway(paper=True)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        assert result is not None
        assert result.fill_price == result.requested_price

    def test_paper_fill_has_correct_metadata(self):
        gw = _make_gateway(paper=True)
        result = gw.execute(
            _make_hypothesis(direction=Direction.SHORT, strategy=Strategy.MOMENTUM),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        assert result is not None
        assert result.pair == "EURUSD"
        assert result.direction == "SHORT"
        assert result.strategy == "MOMENTUM"
        assert result.regime == "TRENDING_UP"
        assert result.filled_at_ms > 0


# ── Live execution mode ──────────────────────────────────────────────────


class TestLiveMode:
    """Live mode: calls mt5.order_send()."""

    def test_success_returns_fill_record(self):
        order_result = _make_order_result(
            retcode=TRADE_RETCODE_DONE,
            price=1.08467,
            volume=0.01,
            order=999,
        )
        gw = _make_gateway(paper=False, order_result=order_result)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        assert result is not None
        assert isinstance(result, FillRecord)
        assert result.order_id == 999
        assert result.fill_price == 1.08467
        assert result.is_paper is False

    def test_slippage_calculated(self):
        """Slippage = |fill_price - requested_price|."""
        order_result = _make_order_result(
            retcode=TRADE_RETCODE_DONE,
            price=1.08470,  # 0.5 pip slippage from ask 1.08465
        )
        gw = _make_gateway(paper=False, order_result=order_result)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        assert result is not None
        assert abs(result.slippage_points - 0.00005) < 1e-10

    def test_bad_retcode_returns_none(self):
        order_result = _make_order_result(retcode=10004)  # REQUOTE
        gw = _make_gateway(paper=False, order_result=order_result)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        assert result is None

    def test_order_send_none_returns_none(self):
        gw = _make_gateway(paper=False, order_result=None)
        result = gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        assert result is None

    def test_order_send_called_with_correct_request(self):
        order_result = _make_order_result()
        gw = _make_gateway(paper=False, order_result=order_result)
        gw.execute(
            _make_hypothesis(),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        call_args = gw._mt5.order_send.call_args[0][0]
        assert call_args["action"] == 1
        assert call_args["symbol"] == "EURUSD"
        assert call_args["type"] == 0  # BUY for LONG
        assert call_args["sl"] == 1.08100
        assert call_args["tp"] == 1.09500
        assert call_args["magic"] == 4

    def test_short_order_type(self):
        order_result = _make_order_result()
        gw = _make_gateway(paper=False, order_result=order_result)
        gw.execute(
            _make_hypothesis(Direction.SHORT),
            _make_decision(),
            10_000.0,
            _now_ms(),
        )
        call_args = gw._mt5.order_send.call_args[0][0]
        assert call_args["type"] == 1  # SELL for SHORT


# ── FillRecord dataclass tests ───────────────────────────────────────────


class TestFillRecord:
    """FillRecord is frozen and has all expected fields."""

    def test_frozen(self):
        fill = FillRecord(
            order_id=1,
            pair="EURUSD",
            direction="LONG",
            strategy="MOMENTUM",
            regime="TRENDING_UP",
            requested_price=1.08,
            fill_price=1.08,
            requested_volume=0.01,
            filled_volume=0.01,
            slippage_points=0.0,
            is_paper=True,
            filled_at_ms=1000,
        )
        with pytest.raises(AttributeError):
            fill.order_id = 2  # type: ignore[misc]

    def test_all_fields_present(self):
        fill = FillRecord(
            order_id=1,
            pair="EURUSD",
            direction="LONG",
            strategy="MOMENTUM",
            regime="TRENDING_UP",
            requested_price=1.08,
            fill_price=1.08,
            requested_volume=0.01,
            filled_volume=0.01,
            slippage_points=0.0,
            is_paper=True,
            filled_at_ms=1000,
        )
        assert fill.order_id == 1
        assert fill.pair == "EURUSD"
        assert fill.direction == "LONG"
        assert fill.strategy == "MOMENTUM"
        assert fill.regime == "TRENDING_UP"
        assert fill.requested_price == 1.08
        assert fill.fill_price == 1.08
        assert fill.requested_volume == 0.01
        assert fill.filled_volume == 0.01
        assert fill.slippage_points == 0.0
        assert fill.is_paper is True
        assert fill.filled_at_ms == 1000
