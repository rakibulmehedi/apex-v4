"""
src/alpha/momentum.py — Multi-timeframe momentum engine.

Phase 2 (P2.2).
Activated when: TRENDING_UP or TRENDING_DOWN regime.
Output: AlphaHypothesis with strategy=MOMENTUM, or None.

Signal logic:
  - Direction from regime (TRENDING_UP → LONG, TRENDING_DOWN → SHORT)
  - Multi-TF confirmation: H4 EMA20 and H1 EMA20 agree with direction
  - Entry zone: M15 EMA20 ± (0.2 × ATR14)
  - Stop loss: entry ± (1.5 × ATR14) against direction
  - Take profit: entry ± (4.0 × ATR14) in direction
  - Setup score 0–30: +10 H4, +10 ADX>30, +5 session, +5 spread
  - Reject if expected_R < 1.8
"""
from __future__ import annotations

import numpy as np
import structlog
import talib

from src.market.schemas import (
    AlphaHypothesis,
    Direction,
    FeatureVector,
    MarketSnapshot,
    Regime,
    Strategy,
    TradingSession,
)

logger = structlog.get_logger(__name__)

# ATR multipliers for entry, stop, and take-profit.
_ENTRY_ATR_MULT = 0.2
_SL_ATR_MULT = 1.5
_TP_ATR_MULT = 4.0  # 2.0 × atr_14 × 2.0

# Minimum risk-reward ratio (strategy spec).
_MIN_RR = 1.8

# EMA period for multi-TF confirmation and entry zone.
_EMA_PERIOD = 20

# Spread threshold for +5 setup score (1 pip).
_SPREAD_1PIP = 0.00010


class MomentumEngine:
    """Generate momentum trade hypotheses on trending regimes.

    Parameters
    ----------
    min_rr : float
        Minimum expected R:R to emit a hypothesis (default 1.8).
    """

    def __init__(self, min_rr: float = _MIN_RR) -> None:
        self._min_rr = min_rr

    def generate(
        self,
        fv: FeatureVector,
        regime: Regime,
        snapshot: MarketSnapshot,
    ) -> AlphaHypothesis | None:
        """Attempt to produce a MOMENTUM AlphaHypothesis.

        Returns None (with a logged reason) when any gate fails.
        """
        # ── Gate 1: regime must be trending ───────────────────────────
        if regime not in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
            logger.info(
                "momentum_rejected", pair=fv.pair,
                reason="regime_not_trending", regime=regime.value,
            )
            return None

        direction = (
            Direction.LONG if regime == Regime.TRENDING_UP
            else Direction.SHORT
        )

        # ── Compute EMAs from snapshot candles ────────────────────────
        h4_ema20 = _ema20_last(snapshot.candles.H4)
        h1_ema20 = _ema20_last(snapshot.candles.H1)
        m15_ema20 = _ema20_last(snapshot.candles.M15)

        if h4_ema20 is None or h1_ema20 is None or m15_ema20 is None:
            logger.info(
                "momentum_rejected", pair=fv.pair,
                reason="insufficient_candles_for_ema20",
            )
            return None

        # ── Gate 2: multi-TF confirmation ─────────────────────────────
        h1_close = snapshot.candles.H1[-1].close
        h4_close = snapshot.candles.H4[-1].close

        h1_confirms = (
            (h1_close > h1_ema20) if direction == Direction.LONG
            else (h1_close < h1_ema20)
        )
        h4_confirms = (
            (h4_close > h4_ema20) if direction == Direction.LONG
            else (h4_close < h4_ema20)
        )

        if not (h1_confirms and h4_confirms):
            logger.info(
                "momentum_rejected", pair=fv.pair,
                reason="multi_tf_disagreement",
                direction=direction.value,
                h1_confirms=h1_confirms,
                h4_confirms=h4_confirms,
            )
            return None

        # ── Entry zone ────────────────────────────────────────────────
        atr = fv.atr_14
        entry_mid = m15_ema20
        entry_band = _ENTRY_ATR_MULT * atr
        entry_zone = (
            round(entry_mid - entry_band, 5),
            round(entry_mid + entry_band, 5),
        )

        # ── Stop loss & take profit ──────────────────────────────────
        if direction == Direction.LONG:
            stop_loss = round(entry_mid - _SL_ATR_MULT * atr, 5)
            take_profit = round(entry_mid + _TP_ATR_MULT * atr, 5)
        else:
            stop_loss = round(entry_mid + _SL_ATR_MULT * atr, 5)
            take_profit = round(entry_mid - _TP_ATR_MULT * atr, 5)

        # ── Expected R ────────────────────────────────────────────────
        sl_distance = abs(entry_mid - stop_loss)
        tp_distance = abs(take_profit - entry_mid)

        if sl_distance == 0:
            logger.info(
                "momentum_rejected", pair=fv.pair,
                reason="zero_sl_distance",
            )
            return None

        expected_r = round(tp_distance / sl_distance, 4)

        if expected_r < self._min_rr:
            logger.info(
                "momentum_rejected", pair=fv.pair,
                reason="expected_r_below_min",
                expected_r=expected_r,
                min_rr=self._min_rr,
            )
            return None

        # ── Setup score (0–30) ────────────────────────────────────────
        score = 0
        score += 10 if h4_confirms else 0       # +10 H4 confirms
        score += 10 if fv.adx_14 > 30.0 else 0  # +10 ADX > 30
        score += 5 if fv.session in (           # +5 LONDON/OVERLAP
            TradingSession.LONDON, TradingSession.OVERLAP,
        ) else 0
        score += 5 if (                          # +5 spread < 1 pip
            fv.spread_ok and snapshot.spread_points < _SPREAD_1PIP
        ) else 0

        # ── Build hypothesis ─────────────────────────────────────────
        hypothesis = AlphaHypothesis(
            strategy=Strategy.MOMENTUM,
            pair=fv.pair,
            direction=direction,
            entry_zone=entry_zone,
            stop_loss=stop_loss,
            take_profit=take_profit,
            setup_score=score,
            expected_R=expected_r,
            regime=regime,
            conviction=None,
        )

        logger.info(
            "momentum_signal",
            pair=fv.pair,
            direction=direction.value,
            entry_zone=entry_zone,
            stop_loss=stop_loss,
            take_profit=take_profit,
            expected_r=expected_r,
            setup_score=score,
        )
        return hypothesis


def _ema20_last(candles: list) -> float | None:
    """Compute EMA(20) from candle close prices, return the last value.

    Returns None if fewer than 20 candles are available.
    """
    if len(candles) < _EMA_PERIOD:
        return None
    closes = np.array([c.close for c in candles], dtype=np.float64)
    ema = talib.EMA(closes, timeperiod=_EMA_PERIOD)
    last = ema[-1]
    if np.isnan(last):
        return None
    return float(last)
