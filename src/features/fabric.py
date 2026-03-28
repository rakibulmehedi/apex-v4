"""Feature Fabric — TA-Lib indicator computation.

Architecture ref: APEX_V4_STRATEGY.md Section 5, Feature Fabric layer.
Phase: P1.4.

Input:  ``MarketSnapshot`` (H1 candles used for all indicators)
Output: ``FeatureVector``

All indicator calculations use TA-Lib exclusively — no custom numpy
implementations.
"""

from __future__ import annotations

import numpy as np
import structlog
import talib

from src.market.schemas import FeatureVector, MarketSnapshot, TradingSession

logger = structlog.get_logger(__name__)

# Minimum H1 candles required (strategy spec: 200).
_MIN_H1_CANDLES = 200


class FeatureFabric:
    """Compute a ``FeatureVector`` from a ``MarketSnapshot``.

    Parameters
    ----------
    spread_max_points : float
        Maximum acceptable spread (raw ask−bid).  ``spread_ok`` is True
        when ``snapshot.spread_points < spread_max_points``.
    redis_client
        Any object that exposes ``.get(key)`` returning ``bytes | None``
        (e.g. ``redis.Redis``).  Used to read ``news_blackout_{pair}``.
        Pass ``None`` to disable news-blackout lookup (always False).
    """

    def __init__(
        self,
        spread_max_points: float,
        redis_client: object | None = None,
    ) -> None:
        self._spread_max = spread_max_points
        self._redis = redis_client

    def compute(self, snapshot: MarketSnapshot) -> FeatureVector:
        """Build a validated ``FeatureVector`` from *snapshot*.

        Raises
        ------
        ValueError
            If the snapshot contains fewer than 200 H1 candles.
        """
        h1_candles = snapshot.candles.H1
        if len(h1_candles) < _MIN_H1_CANDLES:
            raise ValueError(f"Need at least {_MIN_H1_CANDLES} H1 candles, got {len(h1_candles)}")

        # ── extract numpy arrays from H1 candles ────────────────────
        high = np.array([c.high for c in h1_candles], dtype=np.float64)
        low = np.array([c.low for c in h1_candles], dtype=np.float64)
        close = np.array([c.close for c in h1_candles], dtype=np.float64)

        # ── TA-Lib indicators ───────────────────────────────────────
        atr_arr = talib.ATR(high, low, close, timeperiod=14)
        adx_arr = talib.ADX(high, low, close, timeperiod=14)
        ema_arr = talib.EMA(close, timeperiod=200)
        bb_upper_arr, bb_mid_arr, bb_lower_arr = talib.BBANDS(
            close,
            timeperiod=20,
            nbdevup=2,
            nbdevdn=2,
        )

        # Take the last valid value of each indicator.
        atr_14 = float(atr_arr[-1])
        adx_14 = float(adx_arr[-1])
        ema_200 = float(ema_arr[-1])
        bb_upper = float(bb_upper_arr[-1])
        bb_mid = float(bb_mid_arr[-1])
        bb_lower = float(bb_lower_arr[-1])

        # ── spread gate ─────────────────────────────────────────────
        spread_ok = snapshot.spread_points < self._spread_max

        # ── news blackout from Redis ────────────────────────────────
        news_blackout = self._check_news_blackout(snapshot.pair)

        fv = FeatureVector(
            pair=snapshot.pair,
            timestamp=snapshot.timestamp,
            atr_14=atr_14,
            adx_14=adx_14,
            ema_200=ema_200,
            bb_upper=bb_upper,
            bb_mid=bb_mid,
            bb_lower=bb_lower,
            session=snapshot.session,
            spread_ok=spread_ok,
            news_blackout=news_blackout,
        )

        logger.debug(
            "feature_vector computed",
            pair=fv.pair,
            atr_14=fv.atr_14,
            adx_14=fv.adx_14,
            spread_ok=fv.spread_ok,
        )
        return fv

    def _check_news_blackout(self, pair: str) -> bool:
        """Return True if Redis key ``news_blackout_{pair}`` is set."""
        if self._redis is None:
            return False
        try:
            val = self._redis.get(f"news_blackout_{pair}")
            return val is not None
        except Exception:
            logger.warning(
                "Redis news_blackout lookup failed — defaulting to False",
                pair=pair,
                exc_info=True,
            )
            return False
