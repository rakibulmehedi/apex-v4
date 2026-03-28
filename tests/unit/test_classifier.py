"""Unit tests for src/regime/classifier.py — RegimeClassifier.

Covers all 6 classification branches + edge cases with synthetic FeatureVectors.
Default thresholds: adx_trend=31, adx_range=22 (from P2.8 backtest validation).
"""

from __future__ import annotations

import pytest

from src.market.schemas import FeatureVector, Regime, TradingSession
from src.regime.classifier import RegimeClassifier


# ---------------------------------------------------------------------------
# Helpers — build synthetic FeatureVectors
# ---------------------------------------------------------------------------


def _make_fv(
    adx: float = 35.0,
    ema_200: float = 1.10000,
    spread_ok: bool = True,
    news_blackout: bool = False,
    pair: str = "EURUSD",
) -> FeatureVector:
    """Create a minimal valid FeatureVector with controllable fields."""
    return FeatureVector(
        pair=pair,
        timestamp=1_700_000_000_000,
        atr_14=0.00120,
        adx_14=adx,
        ema_200=ema_200,
        bb_upper=1.10200,
        bb_mid=1.10100,
        bb_lower=1.10000,
        session=TradingSession.LONDON,
        spread_ok=spread_ok,
        news_blackout=news_blackout,
    )


# ---------------------------------------------------------------------------
# Rule 1: news_blackout → UNDEFINED
# ---------------------------------------------------------------------------


class TestNewsBlackout:
    """Rule 1: news_blackout is True → UNDEFINED (highest priority)."""

    def test_news_blackout_returns_undefined(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=35.0, news_blackout=True)
        assert clf.classify(fv, close_price=1.12000) == Regime.UNDEFINED

    def test_news_blackout_overrides_trending(self) -> None:
        """Even with strong ADX and close > EMA, news blackout wins."""
        clf = RegimeClassifier()
        fv = _make_fv(adx=40.0, ema_200=1.05, news_blackout=True)
        assert clf.classify(fv, close_price=1.12000) == Regime.UNDEFINED

    def test_news_blackout_overrides_ranging(self) -> None:
        """Even with ADX < 22, news blackout wins."""
        clf = RegimeClassifier()
        fv = _make_fv(adx=10.0, news_blackout=True)
        assert clf.classify(fv, close_price=1.12000) == Regime.UNDEFINED


# ---------------------------------------------------------------------------
# Rule 2: spread_ok is False → UNDEFINED
# ---------------------------------------------------------------------------


class TestSpreadNotOk:
    """Rule 2: spread_ok is False → UNDEFINED (second priority)."""

    def test_spread_not_ok_returns_undefined(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=35.0, spread_ok=False)
        assert clf.classify(fv, close_price=1.12000) == Regime.UNDEFINED

    def test_spread_overrides_trending(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=40.0, ema_200=1.05, spread_ok=False)
        assert clf.classify(fv, close_price=1.12000) == Regime.UNDEFINED

    def test_spread_overrides_ranging(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=10.0, spread_ok=False)
        assert clf.classify(fv, close_price=1.12000) == Regime.UNDEFINED


# ---------------------------------------------------------------------------
# Rule 3: ADX > 31 AND close > EMA200 → TRENDING_UP
# ---------------------------------------------------------------------------


class TestTrendingUp:
    """Rule 3: ADX > trend threshold (31) AND close > EMA200."""

    def test_basic_trending_up(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=35.0, ema_200=1.10000)
        assert clf.classify(fv, close_price=1.11000) == Regime.TRENDING_UP

    def test_adx_just_above_threshold(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=31.01, ema_200=1.10000)
        assert clf.classify(fv, close_price=1.10001) == Regime.TRENDING_UP

    def test_strong_trend_up(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=60.0, ema_200=1.05000)
        assert clf.classify(fv, close_price=1.12000) == Regime.TRENDING_UP


# ---------------------------------------------------------------------------
# Rule 4: ADX > 31 AND close < EMA200 → TRENDING_DOWN
# ---------------------------------------------------------------------------


class TestTrendingDown:
    """Rule 4: ADX > trend threshold (31) AND close < EMA200."""

    def test_basic_trending_down(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=35.0, ema_200=1.10000)
        assert clf.classify(fv, close_price=1.09000) == Regime.TRENDING_DOWN

    def test_adx_just_above_threshold_down(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=31.01, ema_200=1.10000)
        assert clf.classify(fv, close_price=1.09999) == Regime.TRENDING_DOWN

    def test_strong_trend_down(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=55.0, ema_200=1.15000)
        assert clf.classify(fv, close_price=1.08000) == Regime.TRENDING_DOWN


# ---------------------------------------------------------------------------
# Rule 5: ADX < 22 → RANGING
# ---------------------------------------------------------------------------


class TestRanging:
    """Rule 5: ADX < range threshold (22) → RANGING."""

    def test_basic_ranging(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=15.0)
        assert clf.classify(fv, close_price=1.10000) == Regime.RANGING

    def test_adx_just_below_range(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=21.99)
        assert clf.classify(fv, close_price=1.10000) == Regime.RANGING

    def test_very_low_adx(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=5.0)
        assert clf.classify(fv, close_price=1.10000) == Regime.RANGING


# ---------------------------------------------------------------------------
# Rule 6: ADX 22-31 (dead zone) → UNDEFINED
# ---------------------------------------------------------------------------


class TestDeadZone:
    """Rule 6: ADX between range (22) and trend (31) thresholds → UNDEFINED."""

    def test_adx_in_dead_zone(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=26.0)
        assert clf.classify(fv, close_price=1.10000) == Regime.UNDEFINED

    def test_adx_at_range_boundary(self) -> None:
        """ADX == 22.0 is NOT < 22, so it falls into dead zone."""
        clf = RegimeClassifier()
        fv = _make_fv(adx=22.0)
        assert clf.classify(fv, close_price=1.10000) == Regime.UNDEFINED

    def test_adx_at_trend_boundary(self) -> None:
        """ADX == 31.0 is NOT > 31, so it falls into dead zone."""
        clf = RegimeClassifier()
        fv = _make_fv(adx=31.0)
        assert clf.classify(fv, close_price=1.10000) == Regime.UNDEFINED

    def test_adx_just_above_range(self) -> None:
        clf = RegimeClassifier()
        fv = _make_fv(adx=22.01)
        assert clf.classify(fv, close_price=1.10000) == Regime.UNDEFINED


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Boundary conditions and special scenarios."""

    def test_close_equals_ema_with_high_adx(self) -> None:
        """close == ema_200 with ADX > 31: not > and not <, falls through."""
        clf = RegimeClassifier()
        fv = _make_fv(adx=35.0, ema_200=1.10000)
        # close == ema → neither rule 3 nor rule 4. ADX > adx_range → not rule 5.
        # Falls to rule 6 → UNDEFINED.
        assert clf.classify(fv, close_price=1.10000) == Regime.UNDEFINED

    def test_custom_thresholds(self) -> None:
        """Custom thresholds override defaults."""
        clf = RegimeClassifier(adx_trend_threshold=30.0, adx_range_threshold=15.0)
        fv = _make_fv(adx=25.0, ema_200=1.05)
        assert clf.classify(fv, close_price=1.12000) == Regime.UNDEFINED

    def test_custom_threshold_trending(self) -> None:
        clf = RegimeClassifier(adx_trend_threshold=30.0)
        fv = _make_fv(adx=31.0, ema_200=1.05)
        assert clf.classify(fv, close_price=1.12000) == Regime.TRENDING_UP

    def test_custom_threshold_ranging(self) -> None:
        clf = RegimeClassifier(adx_range_threshold=15.0)
        fv = _make_fv(adx=14.0)
        assert clf.classify(fv, close_price=1.10000) == Regime.RANGING

    def test_different_pairs(self) -> None:
        """Classifier works for any pair."""
        clf = RegimeClassifier()
        fv = _make_fv(adx=35.0, ema_200=1.26000, pair="GBPUSD")
        assert clf.classify(fv, close_price=1.27000) == Regime.TRENDING_UP

    def test_priority_news_over_spread(self) -> None:
        """Both news_blackout and spread_not_ok: news_blackout checked first."""
        clf = RegimeClassifier()
        fv = _make_fv(news_blackout=True, spread_ok=False)
        result = clf.classify(fv, close_price=1.10000)
        assert result == Regime.UNDEFINED
