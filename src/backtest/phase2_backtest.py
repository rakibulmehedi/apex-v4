"""
src/backtest/phase2_backtest.py — Phase 2 backtest validation.

Runs 6 months of synthetic EURUSD H1 data through:
  FeatureFabric → RegimeClassifier → MomentumEngine / MeanReversionEngine

Collects regime distribution, signal counts, and key metrics.
Does NOT execute trades — this validates signal generation only.
"""
from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass, field

import backtrader as bt
import numpy as np
import structlog
import talib

from src.backtest.bt_feed import candles_to_bt_feed
from src.backtest.data_gen import generate_eurusd_h1
from src.market.schemas import (
    CandleMap,
    FeatureVector,
    MarketSnapshot,
    OHLCV,
    Regime,
    TradingSession,
)
from src.regime.classifier import RegimeClassifier
from src.alpha.momentum import MomentumEngine
from src.alpha.mean_reversion import MeanReversionEngine
from src.market.feed import classify_session

logger = structlog.get_logger(__name__)

# Minimum bars before we can compute indicators (EMA-200 needs 200).
_WARMUP_BARS = 200

# Spread for synthetic data (realistic EURUSD).
_SPREAD = 0.00012  # 1.2 pips


@dataclass
class BacktestStats:
    """Collected statistics from a backtest run."""

    total_candles: int = 0
    candles_classified: int = 0
    regime_counts: Counter = field(default_factory=Counter)
    momentum_signals: int = 0
    mr_signals: int = 0
    momentum_expected_rs: list[float] = field(default_factory=list)
    mr_convictions: list[float] = field(default_factory=list)
    adx_trend_threshold: float = 25.0
    adx_range_threshold: float = 20.0

    @property
    def regime_pcts(self) -> dict[str, float]:
        total = self.candles_classified or 1
        return {
            regime: round(count / total * 100, 1)
            for regime, count in self.regime_counts.items()
        }

    @property
    def trending_pct(self) -> float:
        total = self.candles_classified or 1
        trending = (
            self.regime_counts.get("TRENDING_UP", 0)
            + self.regime_counts.get("TRENDING_DOWN", 0)
        )
        return round(trending / total * 100, 1)

    @property
    def ranging_pct(self) -> float:
        total = self.candles_classified or 1
        return round(
            self.regime_counts.get("RANGING", 0) / total * 100, 1
        )

    @property
    def avg_momentum_r(self) -> float | None:
        if not self.momentum_expected_rs:
            return None
        return round(float(np.mean(self.momentum_expected_rs)), 4)

    @property
    def avg_mr_conviction(self) -> float | None:
        if not self.mr_convictions:
            return None
        return round(float(np.mean(self.mr_convictions)), 4)


class Phase2Strategy(bt.Strategy):
    """Backtrader strategy that runs the Phase 2 signal pipeline.

    Does NOT place orders — only classifies regimes and generates signals.
    """

    params = (
        ("adx_trend", 25.0),
        ("adx_range", 20.0),
        ("spread_max", 0.00030),
    )

    def __init__(self) -> None:
        self.bt_stats = BacktestStats(
            adx_trend_threshold=self.p.adx_trend,
            adx_range_threshold=self.p.adx_range,
        )
        self.classifier = RegimeClassifier(
            adx_trend_threshold=self.p.adx_trend,
            adx_range_threshold=self.p.adx_range,
        )
        self.momentum = MomentumEngine()
        self.mr_engine = MeanReversionEngine()

        # Accumulate bars for indicator computation.
        self._h1_opens: list[float] = []
        self._h1_highs: list[float] = []
        self._h1_lows: list[float] = []
        self._h1_closes: list[float] = []
        self._h1_volumes: list[float] = []

    def next(self) -> None:
        self.bt_stats.total_candles += 1

        # Accumulate bar data.
        self._h1_opens.append(self.data.open[0])
        self._h1_highs.append(self.data.high[0])
        self._h1_lows.append(self.data.low[0])
        self._h1_closes.append(self.data.close[0])
        self._h1_volumes.append(self.data.volume[0])

        # Need at least 200 bars for EMA-200 + indicator warmup.
        if len(self._h1_closes) < _WARMUP_BARS:
            return

        # ── Compute indicators via TA-Lib ─────────────────────────
        high = np.array(self._h1_highs, dtype=np.float64)
        low = np.array(self._h1_lows, dtype=np.float64)
        close = np.array(self._h1_closes, dtype=np.float64)

        atr_arr = talib.ATR(high, low, close, timeperiod=14)
        adx_arr = talib.ADX(high, low, close, timeperiod=14)
        ema_arr = talib.EMA(close, timeperiod=200)
        bb_upper, bb_mid, bb_lower = talib.BBANDS(
            close, timeperiod=20, nbdevup=2, nbdevdn=2,
        )

        atr_14 = float(atr_arr[-1])
        adx_14 = float(adx_arr[-1])
        ema_200 = float(ema_arr[-1])

        if np.isnan(adx_14) or np.isnan(ema_200) or np.isnan(atr_14):
            return

        # ── Session classification ────────────────────────────────
        dt = self.data.datetime.datetime(0)
        session = classify_session(dt.hour)

        # ── Build FeatureVector ───────────────────────────────────
        fv = FeatureVector(
            pair="EURUSD",
            timestamp=int(dt.timestamp() * 1000),
            atr_14=atr_14,
            adx_14=adx_14,
            ema_200=ema_200,
            bb_upper=float(bb_upper[-1]),
            bb_mid=float(bb_mid[-1]),
            bb_lower=float(bb_lower[-1]),
            session=session,
            spread_ok=_SPREAD < self.p.spread_max,
            news_blackout=False,
        )

        # ── Regime classification ─────────────────────────────────
        close_price = float(close[-1])
        regime = self.classifier.classify(fv, close_price)
        self.bt_stats.candles_classified += 1
        self.bt_stats.regime_counts[regime.value] += 1

        # ── Build MarketSnapshot for alpha engines ────────────────
        # Need min candles: M5=50, M15=50, H1=200, H4=50.
        n = len(self._h1_closes)
        if n < 200:
            return

        # Build OHLCV lists — use H1 data for all timeframes
        # (synthetic backtest: H1 is our primary timeframe).
        h1_candles = [
            OHLCV(
                open=self._h1_opens[i],
                high=self._h1_highs[i],
                low=self._h1_lows[i],
                close=self._h1_closes[i],
                volume=self._h1_volumes[i],
            )
            for i in range(n)
        ]

        # For M5/M15/H4 we reuse subsets of H1 to satisfy CandleMap minimums.
        # This is a synthetic backtest — multi-TF data is approximate.
        snapshot = MarketSnapshot(
            pair="EURUSD",
            timestamp=int(dt.timestamp() * 1000),
            candles=CandleMap(
                M5=h1_candles[-50:],    # last 50 bars
                M15=h1_candles[-50:],   # last 50 bars
                H1=h1_candles,          # all H1 bars
                H4=h1_candles[-50:],    # last 50 bars
            ),
            spread_points=_SPREAD,
            session=session,
        )

        # ── Momentum engine (TRENDING regimes) ────────────────────
        if regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
            signal = self.momentum.generate(fv, regime, snapshot)
            if signal is not None:
                self.bt_stats.momentum_signals += 1
                self.bt_stats.momentum_expected_rs.append(signal.expected_R)

        # ── Mean reversion engine (RANGING regime) ────────────────
        elif regime == Regime.RANGING:
            signal = self.mr_engine.generate(fv, regime, snapshot)
            if signal is not None:
                self.bt_stats.mr_signals += 1
                if signal.conviction is not None:
                    self.bt_stats.mr_convictions.append(signal.conviction)


def run_backtest(
    adx_trend: float = 25.0,
    adx_range: float = 20.0,
    seed: int = 42,
) -> BacktestStats:
    """Run the Phase 2 backtest and return statistics."""
    # Suppress backtrader/structlog noise during backtest.
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(40),  # ERROR only
    )

    candles = generate_eurusd_h1(seed=seed)
    feed = candles_to_bt_feed(candles)

    cerebro = bt.Cerebro()
    cerebro.adddata(feed)
    cerebro.addstrategy(
        Phase2Strategy,
        adx_trend=adx_trend,
        adx_range=adx_range,
    )

    results = cerebro.run()
    stats = results[0].bt_stats

    # Restore structlog default.
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
    )

    return stats


def print_report(stats: BacktestStats) -> None:
    """Print the backtest validation report."""
    print("\n" + "=" * 60)
    print("  APEX V4 — PHASE 2 BACKTEST VALIDATION")
    print("=" * 60)
    print(f"\nADX thresholds: trend={stats.adx_trend_threshold}, "
          f"range={stats.adx_range_threshold}")
    print(f"\nTotal candles:      {stats.total_candles}")
    print(f"Candles classified: {stats.candles_classified}")
    print(f"Warmup skipped:     {stats.total_candles - stats.candles_classified}")

    print("\n── Regime Distribution ──────────────────────────")
    for regime, pct in sorted(stats.regime_pcts.items()):
        count = stats.regime_counts[regime]
        print(f"  {regime:<16s} {pct:5.1f}%  ({count} candles)")

    trending = stats.trending_pct
    ranging = stats.ranging_pct
    print(f"\n  Trending (UP+DOWN): {trending:.1f}%  "
          f"{'✓' if 25 <= trending <= 35 else '✗'} target 25-35%")
    print(f"  Ranging:            {ranging:.1f}%  "
          f"{'✓' if 35 <= ranging <= 45 else '✗'} target 35-45%")

    print("\n── Signal Statistics ────────────────────────────")
    print(f"  Momentum signals:  {stats.momentum_signals}")
    print(f"  MR signals:        {stats.mr_signals}")

    if stats.avg_momentum_r is not None:
        print(f"  Avg expected R (MOM):  {stats.avg_momentum_r}")
    if stats.avg_mr_conviction is not None:
        print(f"  Avg conviction (MR):   {stats.avg_mr_conviction}")

    in_range = 25 <= trending <= 35 and 35 <= ranging <= 45
    print(f"\nOVERALL: {'PASS ✓' if in_range else 'FAIL — adjust thresholds'}")
    print("=" * 60 + "\n")

    return in_range


def main() -> None:
    """Run backtest, adjust thresholds if needed."""
    adx_trend = 25.0
    adx_range = 20.0
    max_attempts = 5

    for attempt in range(1, max_attempts + 1):
        print(f"\n>>> Backtest attempt {attempt}/{max_attempts} "
              f"(trend={adx_trend}, range={adx_range})")

        stats = run_backtest(adx_trend=adx_trend, adx_range=adx_range)
        in_range = print_report(stats)

        if in_range:
            print("Regime distribution within target range. Backtest validated.")
            break

        # Adjust thresholds.
        trending = stats.trending_pct
        ranging = stats.ranging_pct

        if trending < 25:
            adx_trend -= 2
            print(f"  → trending {trending:.1f}% < 25%: "
                  f"lowering adx_trend to {adx_trend}")
        elif trending > 35:
            adx_trend += 2
            print(f"  → trending {trending:.1f}% > 35%: "
                  f"raising adx_trend to {adx_trend}")

        if ranging < 35:
            adx_range += 2
            print(f"  → ranging {ranging:.1f}% < 35%: "
                  f"raising adx_range to {adx_range}")
        elif ranging > 45:
            adx_range -= 2
            print(f"  → ranging {ranging:.1f}% > 45%: "
                  f"lowering adx_range to {adx_range}")
    else:
        print("WARNING: Could not reach target distribution "
              f"in {max_attempts} attempts.")
        print(f"Final thresholds: trend={adx_trend}, range={adx_range}")


if __name__ == "__main__":
    main()
