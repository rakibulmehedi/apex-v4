"""
src/regime/classifier.py — Hard ADX-based regime classification.

Phase 2 (P2.1): Deterministic regime rules from FeatureVector.

Rules (ADR-001), evaluated in exact order:
  1. news_blackout is True          → UNDEFINED
  2. spread_ok is False             → UNDEFINED
  3. ADX > 25 AND close > EMA200   → TRENDING_UP
  4. ADX > 25 AND close < EMA200   → TRENDING_DOWN
  5. ADX < 20                       → RANGING
  6. ADX 20-25 (everything else)    → UNDEFINED

No ML. No probabilities.
"""
from __future__ import annotations

import structlog

from src.market.schemas import FeatureVector, Regime

logger = structlog.get_logger(__name__)

# Defaults match config/settings.yaml regime section.
_DEFAULT_ADX_TREND = 25.0
_DEFAULT_ADX_RANGE = 20.0


class RegimeClassifier:
    """Classify market regime from a FeatureVector using hard ADX rules.

    Parameters
    ----------
    adx_trend_threshold : float
        ADX value above which the market is considered trending (default 25).
    adx_range_threshold : float
        ADX value below which the market is considered ranging (default 20).
    """

    def __init__(
        self,
        adx_trend_threshold: float = _DEFAULT_ADX_TREND,
        adx_range_threshold: float = _DEFAULT_ADX_RANGE,
    ) -> None:
        self._adx_trend = adx_trend_threshold
        self._adx_range = adx_range_threshold

    def classify(self, fv: FeatureVector, close_price: float) -> Regime:
        """Return the regime for the given FeatureVector.

        Parameters
        ----------
        fv : FeatureVector
            Computed indicators for a single pair.
        close_price : float
            Latest H1 close price — used for close vs EMA-200 comparison.

        Returns
        -------
        Regime
            One of TRENDING_UP, TRENDING_DOWN, RANGING, or UNDEFINED.
        """
        # Rule 1: news blackout → no trade
        if fv.news_blackout:
            return self._log_and_return(
                fv, close_price, Regime.UNDEFINED, "news_blackout",
            )

        # Rule 2: spread too wide → no trade
        if not fv.spread_ok:
            return self._log_and_return(
                fv, close_price, Regime.UNDEFINED, "spread_not_ok",
            )

        # Rule 3: strong trend, price above EMA → trending up
        if fv.adx_14 > self._adx_trend and close_price > fv.ema_200:
            return self._log_and_return(
                fv, close_price, Regime.TRENDING_UP, "adx_above_trend_close_above_ema",
            )

        # Rule 4: strong trend, price below EMA → trending down
        if fv.adx_14 > self._adx_trend and close_price < fv.ema_200:
            return self._log_and_return(
                fv, close_price, Regime.TRENDING_DOWN, "adx_above_trend_close_below_ema",
            )

        # Rule 5: weak ADX → ranging
        if fv.adx_14 < self._adx_range:
            return self._log_and_return(
                fv, close_price, Regime.RANGING, "adx_below_range",
            )

        # Rule 6: ADX in dead zone (20-25) → undefined
        return self._log_and_return(
            fv, close_price, Regime.UNDEFINED, "adx_dead_zone",
        )

    def _log_and_return(
        self,
        fv: FeatureVector,
        close_price: float,
        regime: Regime,
        reason: str,
    ) -> Regime:
        """Log classification details and return the regime."""
        logger.info(
            "regime_classified",
            pair=fv.pair,
            adx_14=fv.adx_14,
            ema_200=fv.ema_200,
            close=close_price,
            close_vs_ema="above" if close_price > fv.ema_200 else "below" if close_price < fv.ema_200 else "equal",
            spread_ok=fv.spread_ok,
            news_blackout=fv.news_blackout,
            reason=reason,
            regime=regime.value,
        )
        return regime
