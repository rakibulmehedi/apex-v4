"""Unit tests for src/market/schemas.py — Pydantic v2 data contracts.

Validates every constraint from APEX_V4_STRATEGY.md Section 6:
- OHLCV, MarketSnapshot, FeatureVector, AlphaHypothesis,
  CalibratedTradeIntent, RiskDecision
- Invalid data must be rejected by Pydantic validators.
"""
from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from src.market.schemas import (
    OHLCV,
    AlphaHypothesis,
    CalibratedTradeIntent,
    CandleMap,
    Decision,
    Direction,
    FeatureVector,
    MarketSnapshot,
    Regime,
    RiskDecision,
    RiskState,
    Strategy,
    TradingSession,
)


# ── helpers ──────────────────────────────────────────────────────────────

def _ohlcv(**overrides) -> dict:
    base = {"open": 1.1000, "high": 1.1050, "low": 1.0950, "close": 1.1020, "volume": 100.0}
    base.update(overrides)
    return base


def _candle_list(n: int) -> list[dict]:
    return [_ohlcv() for _ in range(n)]


def _candle_map(**overrides) -> dict:
    base = {"M5": _candle_list(50), "M15": _candle_list(50), "H1": _candle_list(200), "H4": _candle_list(50)}
    base.update(overrides)
    return base


def _now_ms() -> int:
    return int(time.time() * 1000)


def _snapshot(**overrides) -> dict:
    base = {
        "pair": "EURUSD",
        "timestamp": _now_ms(),
        "candles": _candle_map(),
        "spread_points": 1.5,
        "session": "LONDON",
    }
    base.update(overrides)
    return base


def _feature_vector(**overrides) -> dict:
    base = {
        "pair": "EURUSD",
        "timestamp": _now_ms(),
        "atr_14": 0.0012,
        "adx_14": 28.5,
        "ema_200": 1.0950,
        "bb_upper": 1.1100,
        "bb_lower": 1.0800,
        "bb_mid": 1.0950,
        "session": "NY",
        "spread_ok": True,
        "news_blackout": False,
    }
    base.update(overrides)
    return base


def _alpha(**overrides) -> dict:
    base = {
        "strategy": "MEAN_REVERSION",
        "pair": "EURUSD",
        "direction": "LONG",
        "entry_zone": (1.0950, 1.0970),
        "stop_loss": 1.0900,
        "take_profit": 1.1100,
        "setup_score": 22,
        "expected_R": 2.5,
        "regime": "RANGING",
        "conviction": 0.78,
    }
    base.update(overrides)
    return base


def _calibrated(**overrides) -> dict:
    base = {
        "p_win": 0.55,
        "expected_R": 2.0,
        "edge": 0.10,
        "suggested_size": 0.015,
        "segment_count": 45,
    }
    base.update(overrides)
    return base


def _risk_decision(**overrides) -> dict:
    base = {
        "decision": "APPROVE",
        "final_size": 0.01,
        "reason": "All gates passed",
        "risk_state": "NORMAL",
        "gate_failed": None,
    }
    base.update(overrides)
    return base


# ═════════════════════════════════════════════════════════════════════════
# OHLCV
# ═════════════════════════════════════════════════════════════════════════

class TestOHLCV:
    def test_valid(self):
        c = OHLCV(**_ohlcv())
        assert c.open == 1.1000
        assert c.volume == 100.0

    def test_negative_volume_rejected(self):
        with pytest.raises(ValidationError, match="volume"):
            OHLCV(**_ohlcv(volume=-1.0))

    def test_frozen(self):
        c = OHLCV(**_ohlcv())
        with pytest.raises(ValidationError):
            c.open = 9.9  # type: ignore[misc]


# ═════════════════════════════════════════════════════════════════════════
# MarketSnapshot
# ═════════════════════════════════════════════════════════════════════════

class TestMarketSnapshot:
    def test_valid(self):
        snap = MarketSnapshot(**_snapshot())
        assert snap.type == "MarketSnapshot"
        assert snap.pair == "EURUSD"

    # ── pair validation ──
    def test_pair_too_short(self):
        with pytest.raises(ValidationError, match="pair"):
            MarketSnapshot(**_snapshot(pair="EUR"))

    def test_pair_too_long(self):
        with pytest.raises(ValidationError, match="pair"):
            MarketSnapshot(**_snapshot(pair="EURUSDX"))

    # ── spread_points > 0 ──
    def test_spread_zero_rejected(self):
        with pytest.raises(ValidationError, match="spread_points"):
            MarketSnapshot(**_snapshot(spread_points=0))

    def test_spread_negative_rejected(self):
        with pytest.raises(ValidationError, match="spread_points"):
            MarketSnapshot(**_snapshot(spread_points=-1.0))

    # ── session enum ──
    def test_invalid_session_rejected(self):
        with pytest.raises(ValidationError, match="session"):
            MarketSnapshot(**_snapshot(session="TOKYO"))

    def test_all_sessions_accepted(self):
        for s in TradingSession:
            snap = MarketSnapshot(**_snapshot(session=s.value))
            assert snap.session == s

    # ── candle minimum counts ──
    def test_m5_too_few_candles(self):
        with pytest.raises(ValidationError, match="M5"):
            MarketSnapshot(**_snapshot(candles=_candle_map(M5=_candle_list(49))))

    def test_m15_too_few_candles(self):
        with pytest.raises(ValidationError, match="M15"):
            MarketSnapshot(**_snapshot(candles=_candle_map(M15=_candle_list(49))))

    def test_h1_too_few_candles(self):
        with pytest.raises(ValidationError, match="H1"):
            MarketSnapshot(**_snapshot(candles=_candle_map(H1=_candle_list(199))))

    def test_h4_too_few_candles(self):
        with pytest.raises(ValidationError, match="H4"):
            MarketSnapshot(**_snapshot(candles=_candle_map(H4=_candle_list(49))))

    # ── is_stale computed field ──
    def test_fresh_snapshot_not_stale(self):
        snap = MarketSnapshot(**_snapshot(timestamp=_now_ms()))
        assert snap.is_stale is False

    def test_old_snapshot_is_stale(self):
        old_ts = _now_ms() - 6000  # 6 seconds ago
        snap = MarketSnapshot(**_snapshot(timestamp=old_ts))
        assert snap.is_stale is True

    def test_boundary_not_stale(self):
        # 4990ms ago should NOT be stale (> 5000 threshold).
        # Using 4990 instead of 5000 avoids race between _now_ms() and
        # the is_stale property re-reading the clock.
        boundary_ts = _now_ms() - 4990
        snap = MarketSnapshot(**_snapshot(timestamp=boundary_ts))
        assert snap.is_stale is False

    # ── timestamp > 0 ──
    def test_zero_timestamp_rejected(self):
        with pytest.raises(ValidationError, match="timestamp"):
            MarketSnapshot(**_snapshot(timestamp=0))

    def test_negative_timestamp_rejected(self):
        with pytest.raises(ValidationError, match="timestamp"):
            MarketSnapshot(**_snapshot(timestamp=-1))

    # ── frozen ──
    def test_frozen(self):
        snap = MarketSnapshot(**_snapshot())
        with pytest.raises(ValidationError):
            snap.pair = "GBPUSD"  # type: ignore[misc]


# ═════════════════════════════════════════════════════════════════════════
# FeatureVector
# ═════════════════════════════════════════════════════════════════════════

class TestFeatureVector:
    def test_valid(self):
        fv = FeatureVector(**_feature_vector())
        assert fv.type == "FeatureVector"
        assert fv.atr_14 == 0.0012

    def test_pair_too_short(self):
        with pytest.raises(ValidationError, match="pair"):
            FeatureVector(**_feature_vector(pair="EUR"))

    def test_invalid_session(self):
        with pytest.raises(ValidationError, match="session"):
            FeatureVector(**_feature_vector(session="SYDNEY"))

    def test_zero_timestamp_rejected(self):
        with pytest.raises(ValidationError, match="timestamp"):
            FeatureVector(**_feature_vector(timestamp=0))


# ═════════════════════════════════════════════════════════════════════════
# AlphaHypothesis
# ═════════════════════════════════════════════════════════════════════════

class TestAlphaHypothesis:
    def test_valid_mean_reversion(self):
        ah = AlphaHypothesis(**_alpha())
        assert ah.strategy == Strategy.MEAN_REVERSION
        assert ah.conviction == 0.78

    def test_valid_momentum_no_conviction(self):
        ah = AlphaHypothesis(**_alpha(strategy="MOMENTUM", conviction=None))
        assert ah.conviction is None

    # ── setup_score 0-30 ──
    def test_setup_score_negative_rejected(self):
        with pytest.raises(ValidationError, match="setup_score"):
            AlphaHypothesis(**_alpha(setup_score=-1))

    def test_setup_score_31_rejected(self):
        with pytest.raises(ValidationError, match="setup_score"):
            AlphaHypothesis(**_alpha(setup_score=31))

    def test_setup_score_boundary_0(self):
        ah = AlphaHypothesis(**_alpha(setup_score=0))
        assert ah.setup_score == 0

    def test_setup_score_boundary_30(self):
        ah = AlphaHypothesis(**_alpha(setup_score=30))
        assert ah.setup_score == 30

    # ── expected_R >= 1.8 ──
    def test_expected_r_below_threshold(self):
        with pytest.raises(ValidationError, match="expected_R"):
            AlphaHypothesis(**_alpha(expected_R=1.79))

    def test_expected_r_at_threshold(self):
        ah = AlphaHypothesis(**_alpha(expected_R=1.8))
        assert ah.expected_R == 1.8

    # ── conviction 0.65-1.0 ──
    def test_conviction_below_threshold(self):
        with pytest.raises(ValidationError, match="conviction"):
            AlphaHypothesis(**_alpha(conviction=0.64))

    def test_conviction_above_1(self):
        with pytest.raises(ValidationError, match="conviction"):
            AlphaHypothesis(**_alpha(conviction=1.01))

    def test_conviction_at_boundaries(self):
        ah_low = AlphaHypothesis(**_alpha(conviction=0.65))
        assert ah_low.conviction == 0.65
        ah_high = AlphaHypothesis(**_alpha(conviction=1.0))
        assert ah_high.conviction == 1.0

    # ── conviction must be None for MOMENTUM ──
    def test_momentum_with_conviction_rejected(self):
        with pytest.raises(ValidationError, match="conviction must be None"):
            AlphaHypothesis(**_alpha(strategy="MOMENTUM", conviction=0.75))

    # ── direction enum ──
    def test_invalid_direction(self):
        with pytest.raises(ValidationError, match="direction"):
            AlphaHypothesis(**_alpha(direction="FLAT"))

    # ── strategy enum ──
    def test_invalid_strategy(self):
        with pytest.raises(ValidationError, match="strategy"):
            AlphaHypothesis(**_alpha(strategy="SCALPING"))

    # ── regime enum ──
    def test_invalid_regime(self):
        with pytest.raises(ValidationError, match="regime"):
            AlphaHypothesis(**_alpha(regime="VOLATILE"))

    def test_all_regimes_accepted(self):
        for r in Regime:
            ah = AlphaHypothesis(**_alpha(regime=r.value))
            assert ah.regime == r

    # ── entry_zone ──
    def test_entry_zone_must_be_pair(self):
        with pytest.raises(ValidationError, match="entry_zone"):
            AlphaHypothesis(**_alpha(entry_zone=(1.0,)))

    # ── pair ──
    def test_pair_wrong_length(self):
        with pytest.raises(ValidationError, match="pair"):
            AlphaHypothesis(**_alpha(pair="EU"))


# ═════════════════════════════════════════════════════════════════════════
# CalibratedTradeIntent
# ═════════════════════════════════════════════════════════════════════════

class TestCalibratedTradeIntent:
    def test_valid(self):
        ct = CalibratedTradeIntent(**_calibrated())
        assert ct.edge == 0.10

    # ── edge > 0 ──
    def test_edge_zero_rejected(self):
        with pytest.raises(ValidationError, match="edge"):
            CalibratedTradeIntent(**_calibrated(edge=0.0))

    def test_edge_negative_rejected(self):
        with pytest.raises(ValidationError, match="edge"):
            CalibratedTradeIntent(**_calibrated(edge=-0.05))

    # ── suggested_size 0.0-0.02 ──
    def test_size_above_2pct_rejected(self):
        with pytest.raises(ValidationError, match="suggested_size"):
            CalibratedTradeIntent(**_calibrated(suggested_size=0.021))

    def test_size_negative_rejected(self):
        with pytest.raises(ValidationError, match="suggested_size"):
            CalibratedTradeIntent(**_calibrated(suggested_size=-0.01))

    def test_size_at_boundary(self):
        ct = CalibratedTradeIntent(**_calibrated(suggested_size=0.02))
        assert ct.suggested_size == 0.02

    def test_size_zero_accepted(self):
        ct = CalibratedTradeIntent(**_calibrated(suggested_size=0.0))
        assert ct.suggested_size == 0.0

    # ── p_win 0-1 ──
    def test_p_win_above_1_rejected(self):
        with pytest.raises(ValidationError, match="p_win"):
            CalibratedTradeIntent(**_calibrated(p_win=1.01))

    def test_p_win_negative_rejected(self):
        with pytest.raises(ValidationError, match="p_win"):
            CalibratedTradeIntent(**_calibrated(p_win=-0.1))

    # ── segment_count >= 0 ──
    def test_negative_segment_count_rejected(self):
        with pytest.raises(ValidationError, match="segment_count"):
            CalibratedTradeIntent(**_calibrated(segment_count=-1))


# ═════════════════════════════════════════════════════════════════════════
# RiskDecision
# ═════════════════════════════════════════════════════════════════════════

class TestRiskDecision:
    def test_valid_approve(self):
        rd = RiskDecision(**_risk_decision())
        assert rd.decision == Decision.APPROVE
        assert rd.gate_failed is None

    def test_valid_reject(self):
        rd = RiskDecision(**_risk_decision(decision="REJECT", gate_failed=3))
        assert rd.gate_failed == 3

    def test_valid_reduce(self):
        rd = RiskDecision(**_risk_decision(decision="REDUCE", gate_failed=5))
        assert rd.decision == Decision.REDUCE

    # ── gate_failed consistency ──
    def test_approve_with_gate_failed_rejected(self):
        with pytest.raises(ValidationError, match="gate_failed must be None"):
            RiskDecision(**_risk_decision(decision="APPROVE", gate_failed=1))

    def test_reject_without_gate_failed_rejected(self):
        with pytest.raises(ValidationError, match="gate_failed is required"):
            RiskDecision(**_risk_decision(decision="REJECT", gate_failed=None))

    def test_reduce_without_gate_failed_rejected(self):
        with pytest.raises(ValidationError, match="gate_failed is required"):
            RiskDecision(**_risk_decision(decision="REDUCE", gate_failed=None))

    # ── gate_failed 1-7 ──
    def test_gate_failed_zero_rejected(self):
        with pytest.raises(ValidationError, match="gate_failed"):
            RiskDecision(**_risk_decision(decision="REJECT", gate_failed=0))

    def test_gate_failed_8_rejected(self):
        with pytest.raises(ValidationError, match="gate_failed"):
            RiskDecision(**_risk_decision(decision="REJECT", gate_failed=8))

    def test_gate_failed_boundaries(self):
        rd1 = RiskDecision(**_risk_decision(decision="REJECT", gate_failed=1))
        assert rd1.gate_failed == 1
        rd7 = RiskDecision(**_risk_decision(decision="REJECT", gate_failed=7))
        assert rd7.gate_failed == 7

    # ── decision enum ──
    def test_invalid_decision(self):
        with pytest.raises(ValidationError, match="decision"):
            RiskDecision(**_risk_decision(decision="MAYBE"))

    # ── risk_state enum ──
    def test_invalid_risk_state(self):
        with pytest.raises(ValidationError, match="risk_state"):
            RiskDecision(**_risk_decision(risk_state="PANIC"))

    def test_all_risk_states_accepted(self):
        for rs in RiskState:
            rd = RiskDecision(**_risk_decision(risk_state=rs.value))
            assert rd.risk_state == rs

    # ── final_size >= 0 ──
    def test_negative_final_size_rejected(self):
        with pytest.raises(ValidationError, match="final_size"):
            RiskDecision(**_risk_decision(final_size=-0.01))

    # ── reason non-empty ──
    def test_empty_reason_rejected(self):
        with pytest.raises(ValidationError, match="reason"):
            RiskDecision(**_risk_decision(reason=""))
