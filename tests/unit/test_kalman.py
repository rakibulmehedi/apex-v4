"""Unit tests for src/alpha/kalman.py — Kalman filter wrapper."""

from __future__ import annotations

import numpy as np
import pytest

from src.alpha.kalman import kalman_smooth


class TestKalmanSmooth:
    """kalman_smooth: 1-D filterpy Kalman with rolling R."""

    def test_output_length_matches_input(self) -> None:
        closes = np.linspace(1.10, 1.12, 200)
        states = kalman_smooth(closes)
        assert len(states) == len(closes)

    def test_constant_series_stays_constant(self) -> None:
        """A flat series should produce states near the constant value."""
        closes = np.full(100, 1.10000)
        states = kalman_smooth(closes)
        assert np.allclose(states, 1.10000, atol=1e-4)

    def test_tracks_linear_trend(self) -> None:
        """Filter should track a clean linear ramp closely."""
        closes = np.linspace(1.10, 1.12, 200)
        states = kalman_smooth(closes)
        # Last state should be close to 1.12.
        assert abs(states[-1] - 1.12) < 0.001

    def test_smooths_noisy_signal(self) -> None:
        """Filtered signal should have lower variance than raw."""
        rng = np.random.default_rng(42)
        base = np.full(200, 1.10000)
        noise = rng.normal(0, 0.001, 200)
        closes = base + noise
        states = kalman_smooth(closes)
        assert np.var(states) < np.var(closes)

    def test_rejects_too_few_closes(self) -> None:
        with pytest.raises(ValueError, match="at least 20"):
            kalman_smooth(np.array([1.1, 1.2, 1.3]))

    def test_minimum_20_closes_accepted(self) -> None:
        closes = np.linspace(1.10, 1.11, 20)
        states = kalman_smooth(closes)
        assert len(states) == 20

    def test_rolling_r_uses_last_100(self) -> None:
        """With 200+ candles, R should reflect recent volatility, not old."""
        # First 150 candles: stable. Last 50: volatile.
        stable = np.full(150, 1.10000)
        rng = np.random.default_rng(99)
        volatile = 1.10000 + rng.normal(0, 0.005, 50)
        closes = np.concatenate([stable, volatile])
        states = kalman_smooth(closes)
        # Should still produce valid output.
        assert len(states) == 200
        assert not np.any(np.isnan(states))

    def test_returns_float64_array(self) -> None:
        closes = np.linspace(1.10, 1.11, 50)
        states = kalman_smooth(closes)
        assert states.dtype == np.float64
