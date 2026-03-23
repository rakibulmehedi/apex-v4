"""Unit tests for src/alpha/momentum.py — MomentumEngine.

Covers:
  - Regime gate (only TRENDING_UP / TRENDING_DOWN)
  - Multi-TF confirmation (H4 + H1 EMA20)
  - Entry zone, stop loss, take profit calculations
  - Setup score components (+10 H4, +10 ADX>30, +5 session, +5 spread)
  - Rejection conditions (regime, TF disagreement, R:R)
"""
from __future__ import annotations

import pytest

from src.market.schemas import (
    AlphaHypothesis,
    CandleMap,
    Direction,
    MarketSnapshot,
    FeatureVector,
    OHLCV,
    Regime,
    Strategy,
    TradingSession,
)
from src.alpha.momentum import MomentumEngine


# ---------------------------------------------------------------------------
# Helpers — build synthetic data
# ---------------------------------------------------------------------------

def _make_candles(count: int, close: float, trend: float = 0.0) -> list[OHLCV]:
    """Generate `count` candles around `close` with optional trend slope."""
    candles = []
    for i in range(count):
        c = close + trend * (i - count + 1)
        candles.append(OHLCV(open=c - 0.0001, high=c + 0.0005, low=c - 0.0005, close=c, volume=100))
    return candles


def _make_snapshot(
    pair: str = "EURUSD",
    h4_close: float = 1.10000,
    h1_close: float = 1.10000,
    m15_close: float = 1.10000,
    h4_trend: float = 0.0,
    h1_trend: float = 0.0,
    m15_trend: float = 0.0,
    spread: float = 0.00008,
    session: TradingSession = TradingSession.LONDON,
) -> MarketSnapshot:
    """Create a MarketSnapshot with controllable close prices and trends.

    A positive trend makes later candles higher (uptrend), so the last
    close will be above EMA20 (which averages the last 20 closes).
    """
    return MarketSnapshot(
        pair=pair,
        timestamp=1_700_000_000_000,
        candles=CandleMap(
            M5=_make_candles(50, m15_close),
            M15=_make_candles(50, m15_close, trend=m15_trend),
            H1=_make_candles(200, h1_close, trend=h1_trend),
            H4=_make_candles(50, h4_close, trend=h4_trend),
        ),
        spread_points=spread,
        session=session,
    )


def _make_fv(
    adx: float = 30.0,
    atr: float = 0.00120,
    ema_200: float = 1.09000,
    session: TradingSession = TradingSession.LONDON,
    spread_ok: bool = True,
    news_blackout: bool = False,
    pair: str = "EURUSD",
) -> FeatureVector:
    return FeatureVector(
        pair=pair,
        timestamp=1_700_000_000_000,
        atr_14=atr,
        adx_14=adx,
        ema_200=ema_200,
        bb_upper=1.10200,
        bb_mid=1.10100,
        bb_lower=1.10000,
        session=session,
        spread_ok=spread_ok,
        news_blackout=news_blackout,
    )


def _uptrend_snapshot(**kwargs) -> MarketSnapshot:
    """Snapshot where all TFs show uptrend (close > EMA20)."""
    defaults = dict(
        h4_close=1.10000, h4_trend=0.00010,
        h1_close=1.10000, h1_trend=0.00010,
        m15_close=1.10000, m15_trend=0.00005,
    )
    defaults.update(kwargs)
    return _make_snapshot(**defaults)


def _downtrend_snapshot(**kwargs) -> MarketSnapshot:
    """Snapshot where all TFs show downtrend (close < EMA20)."""
    defaults = dict(
        h4_close=1.10000, h4_trend=-0.00010,
        h1_close=1.10000, h1_trend=-0.00010,
        m15_close=1.10000, m15_trend=-0.00005,
    )
    defaults.update(kwargs)
    return _make_snapshot(**defaults)


# ---------------------------------------------------------------------------
# Regime gate
# ---------------------------------------------------------------------------

class TestRegimeGate:
    """Engine only fires on TRENDING_UP / TRENDING_DOWN."""

    def test_ranging_rejected(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _uptrend_snapshot()
        assert eng.generate(fv, Regime.RANGING, snap) is None

    def test_undefined_rejected(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _uptrend_snapshot()
        assert eng.generate(fv, Regime.UNDEFINED, snap) is None

    def test_trending_up_accepted(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _uptrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        assert result.direction == Direction.LONG

    def test_trending_down_accepted(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _downtrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_DOWN, snap)
        assert result is not None
        assert result.direction == Direction.SHORT


# ---------------------------------------------------------------------------
# Multi-TF confirmation
# ---------------------------------------------------------------------------

class TestMultiTFConfirmation:
    """H4 EMA20 and H1 EMA20 must agree with direction."""

    def test_h4_disagrees_long_rejected(self) -> None:
        """H4 downtrend + TRENDING_UP → rejected."""
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _uptrend_snapshot(h4_trend=-0.00010)
        assert eng.generate(fv, Regime.TRENDING_UP, snap) is None

    def test_h1_disagrees_long_rejected(self) -> None:
        """H1 downtrend + TRENDING_UP → rejected."""
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _uptrend_snapshot(h1_trend=-0.00010)
        assert eng.generate(fv, Regime.TRENDING_UP, snap) is None

    def test_h4_disagrees_short_rejected(self) -> None:
        """H4 uptrend + TRENDING_DOWN → rejected."""
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _downtrend_snapshot(h4_trend=0.00010)
        assert eng.generate(fv, Regime.TRENDING_DOWN, snap) is None

    def test_h1_disagrees_short_rejected(self) -> None:
        """H1 uptrend + TRENDING_DOWN → rejected."""
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _downtrend_snapshot(h1_trend=0.00010)
        assert eng.generate(fv, Regime.TRENDING_DOWN, snap) is None

    def test_both_agree_long_passes(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _uptrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None

    def test_both_agree_short_passes(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _downtrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_DOWN, snap)
        assert result is not None


# ---------------------------------------------------------------------------
# Entry zone, SL, TP
# ---------------------------------------------------------------------------

class TestEntryStopTP:
    """Verify ATR-based entry zone, stop loss, and take profit."""

    def test_long_entry_zone(self) -> None:
        eng = MomentumEngine()
        atr = 0.00100
        fv = _make_fv(atr=atr)
        snap = _uptrend_snapshot(m15_close=1.10000)
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        lo, hi = result.entry_zone
        # Band = 0.2 * ATR = 0.00020
        # Entry zone should be centered around M15 EMA20
        assert hi > lo
        assert abs(hi - lo - 2 * 0.2 * atr) < 1e-5

    def test_long_stop_below_entry(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _uptrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        entry_mid = (result.entry_zone[0] + result.entry_zone[1]) / 2
        assert result.stop_loss < entry_mid

    def test_long_tp_above_entry(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _uptrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        entry_mid = (result.entry_zone[0] + result.entry_zone[1]) / 2
        assert result.take_profit > entry_mid

    def test_short_stop_above_entry(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _downtrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_DOWN, snap)
        assert result is not None
        entry_mid = (result.entry_zone[0] + result.entry_zone[1]) / 2
        assert result.stop_loss > entry_mid

    def test_short_tp_below_entry(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _downtrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_DOWN, snap)
        assert result is not None
        entry_mid = (result.entry_zone[0] + result.entry_zone[1]) / 2
        assert result.take_profit < entry_mid

    def test_sl_distance_is_1_5_atr(self) -> None:
        eng = MomentumEngine()
        atr = 0.00200
        fv = _make_fv(atr=atr)
        snap = _uptrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        entry_mid = (result.entry_zone[0] + result.entry_zone[1]) / 2
        sl_dist = abs(entry_mid - result.stop_loss)
        assert abs(sl_dist - 1.5 * atr) < 1e-5

    def test_tp_distance_is_4_0_atr(self) -> None:
        eng = MomentumEngine()
        atr = 0.00200
        fv = _make_fv(atr=atr)
        snap = _uptrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        entry_mid = (result.entry_zone[0] + result.entry_zone[1]) / 2
        tp_dist = abs(result.take_profit - entry_mid)
        assert abs(tp_dist - 4.0 * atr) < 1e-5


# ---------------------------------------------------------------------------
# Expected R
# ---------------------------------------------------------------------------

class TestExpectedR:
    """expected_R = TP distance / SL distance; reject if < 1.8."""

    def test_default_rr_above_minimum(self) -> None:
        """With standard multipliers (4.0 / 1.5 ≈ 2.67), R should pass."""
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _uptrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        assert result.expected_R >= 1.8

    def test_rr_value_is_correct(self) -> None:
        """4.0 / 1.5 = 2.6667."""
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _uptrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        assert abs(result.expected_R - 4.0 / 1.5) < 0.02

    def test_custom_min_rr_rejects(self) -> None:
        """With min_rr=3.0, the default 2.67 should be rejected."""
        eng = MomentumEngine(min_rr=3.0)
        fv = _make_fv()
        snap = _uptrend_snapshot()
        assert eng.generate(fv, Regime.TRENDING_UP, snap) is None


# ---------------------------------------------------------------------------
# Setup score components
# ---------------------------------------------------------------------------

class TestSetupScore:
    """Score: +10 H4 confirms, +10 ADX>30, +5 LONDON/OVERLAP, +5 spread<1pip."""

    def test_max_score_30(self) -> None:
        """All conditions met → 30."""
        eng = MomentumEngine()
        fv = _make_fv(adx=35.0, session=TradingSession.LONDON)
        snap = _uptrend_snapshot(spread=0.00005)  # < 1 pip
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        assert result.setup_score == 30

    def test_h4_confirms_gives_10(self) -> None:
        """H4 always confirms in uptrend snapshots → +10."""
        eng = MomentumEngine()
        fv = _make_fv(adx=28.0, session=TradingSession.ASIA)  # no ADX>30, no session bonus
        snap = _uptrend_snapshot(spread=0.00020)  # > 1 pip, no spread bonus
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        # H4 confirms (+10), ADX 28 (no +10), ASIA (no +5), spread>1pip (no +5)
        assert result.setup_score == 10

    def test_adx_above_30_gives_10(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv(adx=35.0, session=TradingSession.ASIA)
        snap = _uptrend_snapshot(spread=0.00020)
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        # H4 (+10) + ADX>30 (+10) = 20
        assert result.setup_score == 20

    def test_adx_exactly_30_no_bonus(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv(adx=30.0, session=TradingSession.ASIA)
        snap = _uptrend_snapshot(spread=0.00020)
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        # H4 (+10) only; ADX == 30 is not > 30
        assert result.setup_score == 10

    def test_london_session_gives_5(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv(adx=28.0, session=TradingSession.LONDON)
        snap = _uptrend_snapshot(spread=0.00020)
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        # H4 (+10) + LONDON (+5) = 15
        assert result.setup_score == 15

    def test_overlap_session_gives_5(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv(adx=28.0, session=TradingSession.OVERLAP)
        snap = _uptrend_snapshot(spread=0.00020)
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        # H4 (+10) + OVERLAP (+5) = 15
        assert result.setup_score == 15

    def test_ny_session_no_bonus(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv(adx=28.0, session=TradingSession.NY)
        snap = _uptrend_snapshot(spread=0.00020)
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        # H4 (+10) only
        assert result.setup_score == 10

    def test_tight_spread_gives_5(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv(adx=28.0, session=TradingSession.ASIA)
        snap = _uptrend_snapshot(spread=0.00005)  # 0.5 pips < 1 pip
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        # H4 (+10) + spread (+5) = 15
        assert result.setup_score == 15

    def test_spread_exactly_1pip_no_bonus(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv(adx=28.0, session=TradingSession.ASIA)
        snap = _uptrend_snapshot(spread=0.00010)  # exactly 1 pip, not < 1 pip
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        # H4 (+10) only
        assert result.setup_score == 10


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------

class TestOutputShape:
    """Verify AlphaHypothesis fields are correctly populated."""

    def test_strategy_is_momentum(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _uptrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        assert result.strategy == Strategy.MOMENTUM

    def test_conviction_is_none(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _uptrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        assert result.conviction is None

    def test_regime_matches_input(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv()
        snap = _uptrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        assert result.regime == Regime.TRENDING_UP

    def test_pair_matches_input(self) -> None:
        eng = MomentumEngine()
        fv = _make_fv(pair="GBPUSD")
        snap = _uptrend_snapshot()
        result = eng.generate(fv, Regime.TRENDING_UP, snap)
        assert result is not None
        assert result.pair == "GBPUSD"
