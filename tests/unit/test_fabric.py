"""Unit tests for src/features/fabric.py — TA-Lib indicator computation.

Every test uses deterministic input data and verifies output against
pre-computed TA-Lib reference values.  Redis is mocked.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import numpy as np
import pytest
import talib

from src.features.fabric import FeatureFabric
from src.market.schemas import (
    CandleMap,
    FeatureVector,
    MarketSnapshot,
    OHLCV,
    TradingSession,
)


# ── fixtures ─────────────────────────────────────────────────────────────


def _make_ohlcv(o: float, h: float, l: float, c: float, v: float = 100.0) -> OHLCV:
    return OHLCV(open=o, high=h, low=l, close=c, volume=v)


def _linear_candles(n: int) -> list[OHLCV]:
    """Linear ramp: close from 1.1000 to 1.1199 over *n* bars.

    high = close + 0.0010, low = close - 0.0010.
    Deterministic — produces known TA-Lib outputs.
    """
    closes = np.linspace(1.1000, 1.1199, n)
    return [
        _make_ohlcv(
            o=float(closes[i]),
            h=float(closes[i] + 0.0010),
            l=float(closes[i] - 0.0010),
            c=float(closes[i]),
        )
        for i in range(n)
    ]


def _sine_candles(n: int) -> list[OHLCV]:
    """Sinusoidal (mean-reverting) data — different indicator behaviour."""
    t = np.arange(n, dtype=np.float64)
    closes = 1.10 + 0.005 * np.sin(2 * np.pi * t / 50)
    return [
        _make_ohlcv(
            o=float(closes[i]),
            h=float(closes[i] + 0.0008),
            l=float(closes[i] - 0.0008),
            c=float(closes[i]),
        )
        for i in range(n)
    ]


def _filler_candles(n: int) -> list[OHLCV]:
    """Minimal filler candles for M5/M15/H4 slots."""
    return [_make_ohlcv(1.1, 1.101, 1.099, 1.1) for _ in range(n)]


def _snapshot(
    h1_candles: list[OHLCV],
    *,
    pair: str = "EURUSD",
    spread: float = 0.00015,
    session: TradingSession = TradingSession.LONDON,
) -> MarketSnapshot:
    return MarketSnapshot(
        pair=pair,
        timestamp=int(time.time() * 1000),
        candles=CandleMap(
            M5=_filler_candles(50),
            M15=_filler_candles(50),
            H1=h1_candles,
            H4=_filler_candles(50),
        ),
        spread_points=spread,
        session=session,
    )


# ── reference values (pre-computed from TA-Lib with same input) ──────────

# Linear ramp: constant range → ATR = 0.002, perfect trend → ADX = 100
_LINEAR_ATR = 0.0020
_LINEAR_ADX = 100.0
_LINEAR_EMA = 1.10995
_LINEAR_BB_UPPER = 1.1201032563
_LINEAR_BB_MID = 1.11895
_LINEAR_BB_LOWER = 1.1177967437

# Sinusoidal: smaller range → ATR = 0.0016, cyclical trend → ADX ≈ 41.82
_SINE_ATR = 0.0016
_SINE_ADX = 41.8178296186
_SINE_EMA = 1.10


# ═════════════════════════════════════════════════════════════════════════
# Indicator accuracy — linear ramp
# ═════════════════════════════════════════════════════════════════════════


class TestLinearRamp:
    """Verify each indicator against known output for a linear ramp."""

    @pytest.fixture
    def fv(self) -> FeatureVector:
        fabric = FeatureFabric(spread_max_points=0.00030)
        return fabric.compute(_snapshot(_linear_candles(200)))

    def test_atr_14(self, fv: FeatureVector):
        assert fv.atr_14 == pytest.approx(_LINEAR_ATR, abs=1e-8)

    def test_adx_14(self, fv: FeatureVector):
        assert fv.adx_14 == pytest.approx(_LINEAR_ADX, abs=1e-8)

    def test_ema_200(self, fv: FeatureVector):
        assert fv.ema_200 == pytest.approx(_LINEAR_EMA, abs=1e-6)

    def test_bb_upper(self, fv: FeatureVector):
        assert fv.bb_upper == pytest.approx(_LINEAR_BB_UPPER, abs=1e-6)

    def test_bb_mid(self, fv: FeatureVector):
        assert fv.bb_mid == pytest.approx(_LINEAR_BB_MID, abs=1e-6)

    def test_bb_lower(self, fv: FeatureVector):
        assert fv.bb_lower == pytest.approx(_LINEAR_BB_LOWER, abs=1e-6)

    def test_returns_feature_vector(self, fv: FeatureVector):
        assert isinstance(fv, FeatureVector)
        assert fv.type == "FeatureVector"


# ═════════════════════════════════════════════════════════════════════════
# Indicator accuracy — sinusoidal data
# ═════════════════════════════════════════════════════════════════════════


class TestSinusoidal:
    """Different data shape → different indicator values."""

    @pytest.fixture
    def fv(self) -> FeatureVector:
        fabric = FeatureFabric(spread_max_points=0.00030)
        return fabric.compute(_snapshot(_sine_candles(200)))

    def test_atr_14(self, fv: FeatureVector):
        assert fv.atr_14 == pytest.approx(_SINE_ATR, abs=1e-8)

    def test_adx_14(self, fv: FeatureVector):
        assert fv.adx_14 == pytest.approx(_SINE_ADX, abs=1e-4)

    def test_ema_200(self, fv: FeatureVector):
        assert fv.ema_200 == pytest.approx(_SINE_EMA, abs=1e-6)

    def test_adx_differs_from_linear(self, fv: FeatureVector):
        """Sinusoidal ADX should NOT be 100 — validates data sensitivity."""
        assert fv.adx_14 != pytest.approx(100.0, abs=1.0)


# ═════════════════════════════════════════════════════════════════════════
# Indicator with > 200 candles (extra depth)
# ═════════════════════════════════════════════════════════════════════════


class TestExtraCandles:
    def test_250_candles_accepted(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(250)))
        # With 250 linear candles the last ATR should still be 0.002
        assert fv.atr_14 == pytest.approx(0.002, abs=1e-8)


# ═════════════════════════════════════════════════════════════════════════
# ValueError on insufficient candles
# ═════════════════════════════════════════════════════════════════════════


class TestInsufficientCandles:
    def test_199_candles_raises(self):
        """Exactly 199 H1 candles must raise ValueError."""
        fabric = FeatureFabric(spread_max_points=0.00030)
        # Build snapshot bypassing CandleMap validation by giving H1=200
        # then slicing — but CandleMap enforces min_length=200.
        # Instead, build a snapshot with 200 and then test with a modified
        # candle list via a helper that skips CandleMap.
        candles = _linear_candles(199)
        with pytest.raises(ValueError, match="Need at least 200 H1 candles"):
            # Can't build a valid MarketSnapshot with < 200 H1 candles
            # because CandleMap rejects it.  So we test the fabric directly
            # by constructing a snapshot with 200 candles, then calling
            # the compute path that validates again.
            #
            # Create a MarketSnapshot mock with short H1 list.
            snap = MagicMock()
            snap.candles.H1 = candles
            snap.pair = "EURUSD"
            snap.timestamp = int(time.time() * 1000)
            snap.spread_points = 0.00015
            snap.session = TradingSession.LONDON
            fabric.compute(snap)

    def test_0_candles_raises(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        snap = MagicMock()
        snap.candles.H1 = []
        with pytest.raises(ValueError, match="Need at least 200"):
            fabric.compute(snap)

    def test_exactly_200_accepted(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert fv is not None


# ═════════════════════════════════════════════════════════════════════════
# spread_ok
# ═════════════════════════════════════════════════════════════════════════


class TestSpreadOk:
    def test_spread_below_threshold(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200), spread=0.00015))
        assert fv.spread_ok is True

    def test_spread_at_threshold(self):
        """spread_ok requires strict less-than — equal is NOT ok."""
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200), spread=0.00030))
        assert fv.spread_ok is False

    def test_spread_above_threshold(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200), spread=0.00050))
        assert fv.spread_ok is False

    def test_custom_threshold(self):
        fabric = FeatureFabric(spread_max_points=0.00100)
        fv = fabric.compute(_snapshot(_linear_candles(200), spread=0.00050))
        assert fv.spread_ok is True


# ═════════════════════════════════════════════════════════════════════════
# news_blackout from Redis
# ═════════════════════════════════════════════════════════════════════════


class TestNewsBlackout:
    def test_no_redis_returns_false(self):
        fabric = FeatureFabric(spread_max_points=0.00030, redis_client=None)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert fv.news_blackout is False

    def test_redis_key_set_returns_true(self):
        redis = MagicMock()
        redis.get.return_value = b"1"
        fabric = FeatureFabric(spread_max_points=0.00030, redis_client=redis)
        fv = fabric.compute(_snapshot(_linear_candles(200), pair="EURUSD"))
        assert fv.news_blackout is True
        redis.get.assert_called_once_with("news_blackout_EURUSD")

    def test_redis_key_absent_returns_false(self):
        redis = MagicMock()
        redis.get.return_value = None
        fabric = FeatureFabric(spread_max_points=0.00030, redis_client=redis)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert fv.news_blackout is False

    def test_redis_error_returns_false(self):
        """Redis failure must not crash — defaults to False."""
        redis = MagicMock()
        redis.get.side_effect = ConnectionError("Redis down")
        fabric = FeatureFabric(spread_max_points=0.00030, redis_client=redis)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert fv.news_blackout is False


# ═════════════════════════════════════════════════════════════════════════
# Session passthrough
# ═════════════════════════════════════════════════════════════════════════


class TestSessionPassthrough:
    def test_session_from_snapshot(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        for session in TradingSession:
            fv = fabric.compute(_snapshot(_linear_candles(200), session=session))
            assert fv.session == session


# ═════════════════════════════════════════════════════════════════════════
# Pair + timestamp passthrough
# ═════════════════════════════════════════════════════════════════════════


class TestPassthrough:
    def test_pair_preserved(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200), pair="GBPUSD"))
        assert fv.pair == "GBPUSD"

    def test_timestamp_preserved(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        snap = _snapshot(_linear_candles(200))
        fv = fabric.compute(snap)
        assert fv.timestamp == snap.timestamp
