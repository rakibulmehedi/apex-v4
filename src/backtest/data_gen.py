"""
src/backtest/data_gen.py — Synthetic EURUSD H1 data generator.

Generates realistic price data with regime-switching dynamics:
  - Trending regimes: directional moves with high ADX
  - Ranging regimes: mean-reverting oscillation with low ADX
  - Transition zones: ADX in dead zone (20-25)

Used for Phase 2 backtest validation (P2.8).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

# 6 months of H1 candles (approx 26 weeks × 5 days × 24 hours).
_DEFAULT_CANDLE_COUNT = 26 * 5 * 24  # 3120 trading hours


@dataclass(frozen=True)
class SyntheticCandle:
    """Single H1 candle with timestamp."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


def generate_eurusd_h1(
    n_candles: int = _DEFAULT_CANDLE_COUNT,
    seed: int = 42,
    start_price: float = 1.08000,
    start_date: datetime | None = None,
) -> list[SyntheticCandle]:
    """Generate *n_candles* synthetic EURUSD H1 candles.

    Regime dynamics:
      - Regime switches every ~80-200 candles (random)
      - Trending: drift ± 0.00015/bar, vol 0.0008
      - Ranging: mean-reverting, theta=0.06, vol 0.0005
      - Transition: low drift, moderate vol
    """
    rng = np.random.default_rng(seed)

    if start_date is None:
        start_date = datetime(2025, 9, 1, 0, 0, tzinfo=timezone.utc)

    prices = np.empty(n_candles + 1, dtype=np.float64)
    prices[0] = start_price

    # Regime schedule: pre-compute switching points.
    regimes: list[str] = []  # "trend_up", "trend_down", "range", "transition"
    i = 0
    while i < n_candles:
        regime = rng.choice(
            ["trend_up", "trend_down", "range", "transition"],
            p=[0.15, 0.15, 0.45, 0.25],
        )
        duration = int(rng.integers(80, 200))
        regimes.extend([regime] * min(duration, n_candles - i))
        i += duration

    regimes = regimes[:n_candles]

    # Range mean tracks slowly to avoid unrealistic anchoring.
    range_mean = start_price

    for i in range(n_candles):
        regime = regimes[i]
        p = prices[i]

        if regime == "trend_up":
            drift = rng.normal(0.00015, 0.00005)
            noise = rng.normal(0, 0.00080)
            prices[i + 1] = p + drift + noise
        elif regime == "trend_down":
            drift = rng.normal(-0.00015, 0.00005)
            noise = rng.normal(0, 0.00080)
            prices[i + 1] = p + drift + noise
        elif regime == "range":
            range_mean = 0.999 * range_mean + 0.001 * p
            reversion = 0.06 * (range_mean - p)
            noise = rng.normal(0, 0.00050)
            prices[i + 1] = p + reversion + noise
        else:  # transition
            noise = rng.normal(0, 0.00065)
            prices[i + 1] = p + noise

    # Build candles from close prices.
    candles: list[SyntheticCandle] = []
    ts = start_date

    for i in range(n_candles):
        c = prices[i + 1]
        o = prices[i]
        # Intra-bar volatility for high/low.
        bar_range = abs(c - o) + rng.exponential(0.00030)
        h = max(o, c) + rng.uniform(0, bar_range * 0.5)
        l = min(o, c) - rng.uniform(0, bar_range * 0.5)
        vol = int(rng.integers(80, 400))

        candles.append(SyntheticCandle(
            timestamp=ts,
            open=round(o, 5),
            high=round(h, 5),
            low=round(l, 5),
            close=round(c, 5),
            volume=vol,
        ))
        ts += timedelta(hours=1)

    return candles
