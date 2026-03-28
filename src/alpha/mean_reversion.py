"""
src/alpha/mean_reversion.py — Mean reversion engine (orchestrator).

Phase 2 (P2.3–P2.7).
Activated when: RANGING regime.
Pipeline: ADF gate → Kalman filter → OU MLE → conviction → signal.

Output: AlphaHypothesis with strategy=MEAN_REVERSION, or None.
Returns None with a logged reason at every gate failure.

Key thresholds (from config/settings.yaml & Section 7):
  ADF p-value < 0.05 (stationarity gate)
  OU half-life ≤ 48 H1 candles
  Conviction ≥ 0.65
  |z-score| < 3.0 (3σ guard)
  expected_R ≥ 1.8
"""

from __future__ import annotations

import numpy as np
import structlog
from statsmodels.tsa.stattools import adfuller

from src.alpha.kalman import kalman_smooth
from src.alpha.ou_calibration import compute_conviction, fit_ou
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

# Minimum H1 candles required (strategy spec: 200).
_MIN_H1_CANDLES = 200

# ADF p-value threshold for stationarity.
_DEFAULT_ADF_PVALUE = 0.05

# SL multiplier against direction (ATR-based).
_SL_ATR_MULT = 1.5

# Minimum expected R:R.
_MIN_RR = 1.8


class MeanReversionEngine:
    """Generate mean-reversion trade hypotheses on RANGING regimes.

    Parameters
    ----------
    adf_pvalue : float
        Maximum ADF p-value to accept stationarity (default 0.05).
    min_rr : float
        Minimum expected R:R ratio (default 1.8).
    zscore_guard : float
        Maximum |z| before regime break rejection (default 3.0).
    min_conviction : float
        Minimum conviction to accept (default 0.65).
    """

    def __init__(
        self,
        adf_pvalue: float = _DEFAULT_ADF_PVALUE,
        min_rr: float = _MIN_RR,
        zscore_guard: float = 3.0,
        min_conviction: float = 0.65,
    ) -> None:
        self._adf_pvalue = adf_pvalue
        self._min_rr = min_rr
        self._zscore_guard = zscore_guard
        self._min_conviction = min_conviction

    def generate(
        self,
        fv: FeatureVector,
        regime: Regime,
        snapshot: MarketSnapshot,
    ) -> AlphaHypothesis | None:
        """Attempt to produce a MEAN_REVERSION AlphaHypothesis.

        Returns None (with a logged reason) when any gate fails.
        """
        # ── Gate 1: regime must be RANGING ────────────────────────────
        if regime != Regime.RANGING:
            logger.info(
                "mr_rejected",
                pair=fv.pair,
                reason="regime_not_ranging",
                regime=regime.value,
            )
            return None

        # ── Gate 2: minimum 200 H1 candles ───────────────────────────
        h1_candles = snapshot.candles.H1
        if len(h1_candles) < _MIN_H1_CANDLES:
            logger.info(
                "mr_rejected",
                pair=fv.pair,
                reason="insufficient_h1_candles",
                count=len(h1_candles),
            )
            return None

        closes = np.array(
            [c.close for c in h1_candles],
            dtype=np.float64,
        )

        # ── Gate 3: ADF stationarity test ─────────────────────────────
        try:
            adf_result = adfuller(closes, maxlag=1, regression="c", autolag=None)
        except ValueError:
            logger.info(
                "mr_rejected",
                pair=fv.pair,
                reason="adf_invalid_input",
            )
            return None
        adf_pvalue = float(adf_result[1])

        if adf_pvalue >= self._adf_pvalue:
            logger.info(
                "mr_rejected",
                pair=fv.pair,
                reason="adf_not_stationary",
                adf_pvalue=round(adf_pvalue, 4),
            )
            return None

        # ── Kalman filter ─────────────────────────────────────────────
        filtered = kalman_smooth(closes)

        # ── OU MLE calibration ────────────────────────────────────────
        ou_params = fit_ou(filtered)
        if ou_params is None:
            logger.info(
                "mr_rejected",
                pair=fv.pair,
                reason="ou_fit_failed",
            )
            return None

        # ── Conviction score ──────────────────────────────────────────
        x_current = float(filtered[-1])
        conv_result = compute_conviction(
            x_current,
            ou_params,
            zscore_guard=self._zscore_guard,
            min_conviction=self._min_conviction,
        )
        if conv_result is None:
            logger.info(
                "mr_rejected",
                pair=fv.pair,
                reason="conviction_gate_failed",
            )
            return None

        # ── Direction from z-score ────────────────────────────────────
        # z < 0 → price below mean → go LONG (expect reversion up)
        # z > 0 → price above mean → go SHORT (expect reversion down)
        direction = Direction.LONG if conv_result.z_score < 0 else Direction.SHORT

        # ── Entry zone, SL, TP ────────────────────────────────────────
        atr = fv.atr_14
        entry_mid = float(closes[-1])  # latest H1 close
        entry_zone = (
            round(entry_mid - 0.2 * atr, 5),
            round(entry_mid + 0.2 * atr, 5),
        )

        # TP = μ (mean reversion target).
        take_profit = round(ou_params.mu, 5)

        # SL = entry ± 1.5×ATR against direction.
        if direction == Direction.LONG:
            stop_loss = round(entry_mid - _SL_ATR_MULT * atr, 5)
        else:
            stop_loss = round(entry_mid + _SL_ATR_MULT * atr, 5)

        # ── Expected R ────────────────────────────────────────────────
        tp_distance = abs(take_profit - entry_mid)
        sl_distance = abs(entry_mid - stop_loss)

        if sl_distance == 0:
            logger.info(
                "mr_rejected",
                pair=fv.pair,
                reason="zero_sl_distance",
            )
            return None

        expected_r = round(tp_distance / sl_distance, 4)

        if expected_r < self._min_rr:
            logger.info(
                "mr_rejected",
                pair=fv.pair,
                reason="expected_r_below_min",
                expected_r=expected_r,
            )
            return None

        # ── Setup score (0–30) ────────────────────────────────────────
        score = 0
        score += 10 if adf_pvalue < 0.01 else 0  # +10 strong stationarity
        score += 10 if ou_params.half_life < 24 else 0  # +10 fast reversion
        score += (
            5
            if fv.session
            in (  # +5 LONDON/OVERLAP
                TradingSession.LONDON,
                TradingSession.OVERLAP,
            )
            else 0
        )
        score += 5 if conv_result.conviction > 0.80 else 0  # +5 high conviction

        # ── Build hypothesis ──────────────────────────────────────────
        hypothesis = AlphaHypothesis(
            strategy=Strategy.MEAN_REVERSION,
            pair=fv.pair,
            direction=direction,
            entry_zone=entry_zone,
            stop_loss=stop_loss,
            take_profit=take_profit,
            setup_score=score,
            expected_R=expected_r,
            regime=regime,
            conviction=conv_result.conviction,
        )

        logger.info(
            "mr_signal",
            pair=fv.pair,
            direction=direction.value,
            entry_zone=entry_zone,
            stop_loss=stop_loss,
            take_profit=take_profit,
            expected_r=expected_r,
            conviction=conv_result.conviction,
            z_score=conv_result.z_score,
            half_life=ou_params.half_life,
            setup_score=score,
        )
        return hypothesis
