"""
src/alpha/kalman.py — filterpy Kalman filter wrapper for price smoothing.

Phase 2 (P2.4): Uses filterpy.kalman.KalmanFilter(dim_x=1, dim_z=1).
Measurement noise R is set from the rolling variance of the last 100
candle closes — not static.

Input:  H1 close prices (numpy array)
Output: Filtered state estimates (numpy array, same length)
"""
from __future__ import annotations

import numpy as np
import structlog
from filterpy.kalman import KalmanFilter

logger = structlog.get_logger(__name__)

# Rolling window for measurement noise estimation.
_R_WINDOW = 100

# Default process noise — small, lets the filter track smoothly.
_DEFAULT_Q = 1e-5


def kalman_smooth(closes: np.ndarray, q: float = _DEFAULT_Q) -> np.ndarray:
    """Run a 1-D Kalman filter over H1 close prices.

    Parameters
    ----------
    closes : np.ndarray
        Array of H1 close prices (length >= 20).
    q : float
        Process noise variance Q (default 1e-5).

    Returns
    -------
    np.ndarray
        Filtered state estimates, same length as *closes*.

    Raises
    ------
    ValueError
        If fewer than 20 closes are provided.
    """
    n = len(closes)
    if n < 20:
        raise ValueError(f"Need at least 20 closes for Kalman filter, got {n}")

    kf = KalmanFilter(dim_x=1, dim_z=1)

    # State transition: random walk (x_{k+1} = x_k + noise).
    kf.F = np.array([[1.0]])
    # Measurement model: observe state directly.
    kf.H = np.array([[1.0]])
    # Process noise.
    kf.Q = np.array([[q]])

    # Initial state: first close.
    kf.x = np.array([[closes[0]]])
    kf.P = np.array([[1.0]])

    # Initial measurement noise from first available window.
    initial_var = np.var(closes[:min(_R_WINDOW, n)])
    kf.R = np.array([[max(initial_var, 1e-10)]])

    states = np.empty(n, dtype=np.float64)

    for i in range(n):
        # Update R from rolling variance of last 100 closes.
        if i >= _R_WINDOW:
            window = closes[i - _R_WINDOW + 1 : i + 1]
        else:
            window = closes[: i + 1]

        rolling_var = np.var(window)
        kf.R = np.array([[max(rolling_var, 1e-10)]])

        # Predict → update cycle.
        kf.predict()
        kf.update(np.array([[closes[i]]]))

        states[i] = float(kf.x[0, 0])

    return states
